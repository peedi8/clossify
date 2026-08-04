# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Clossify MCP 서버 — 네이버 스마트스토어 등록 능력을 MCP 클라이언트 LLM에 부여.

이 모듈은 MCP Python SDK(v2, PyPI `mcp`)의 `MCPServer`(FastMCP 후속)를 사용해
로컬 stdio MCP 서버를 노출한다. 서버는 6개의 도구를 제공한다:

- ``check_config``: 자격증명/설정 파일 존재 및 형식 검사 (외부 API 호출 없음).
- ``upload_images``: 로컬 이미지 경로 리스트를 네이버 이미지서버에 업로드.
- ``register_product``: 상품 정보를 받아 등록 페이로드를 구성하고 커머스 API로 등록.
- ``get_product``: 등록된 상품(origin product)을 조회.
- ``prepare_listing``: 상품 정보 + 이미지 소스로 prepared payload 를 만든다.
- ``submit_reviews``: 클라이언트 LLM 의 검수 회신을 prepared payload 에 병합.

모든 자격증명은 프로젝트 루트의 ``.local/config.json`` 에만 존재한다
(ADR-0002 로컬 MCP + BYO-key). 이 서버 자체는 자격증명을 수탁/저장하지 않는다.

인증 토큰, API 호출, 페이로드 빌딩의 모든 복잡도는 ``naver_client`` 에 캡슐화되어
있으며, 본 모듈은 그것을 MCP 도구로 얇게 감쌀 뿐이다.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from mcp.server import MCPServer

from . import naver_client, qa_agents
from . import register as _register_mod

# 서버 인스턴스 — 클라이언트 LLM이 discover 하는 도구들의 컨테이너.
mcp = MCPServer("clossify")

# 설정 파일 경로 — naver_client.config_path() 의 단일 진실 공급원을 따른다.
# (CLOSSIFY_CONFIG 환경변수 오버라이드 포함)
_CONFIG_PATH = naver_client.config_path()


# --------------------------------------------------------------------------- #
# Fix 1 — 이미지 업로드 검증 상수
# --------------------------------------------------------------------------- #
# 허용 이미지 확장자 화이트리스트 (네이버 이미지서버 호환).
_ALLOWED_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
# 단일 이미지 파일 크기 상한 (10MB, 네이버 정책 기준 여유치).
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Fix 7 — 에러 sanitization
# --------------------------------------------------------------------------- #
# traceback/에러 메시지에서 제거해야 할 민감 패턴.
_SENSITIVE_PATTERNS = [
    # 시크릿/토큰류
    re.compile(r"(sk-[A-Za-z0-9_\-]{8,})", re.IGNORECASE),
    re.compile(r"(AIza[A-Za-z0-9_\-]{8,})", re.IGNORECASE),
    re.compile(r"(Bearer\s+[A-Za-z0-9_\-\.]+)", re.IGNORECASE),
    # key=value 형태의 시크릿 (api_key=..., client_secret: ..., token=..., 등)
    # 콜론(:) 또는 등호(=) 구분자 모두 매칭. 값 부분은 4자까지만 노출(표식용).
    re.compile(
        r"((?:api[_\-]?key|client[_\-]?secret|access[_\-]?token|auth[_\-]?token|"
        r"secret[_\-]?key|password|passwd|pwd|credential|private[_\-]?key|"
        r"token|secret|apikey)"
        r"\s*[:=]\s*)([^\s\"'<>,;]{5,})",
        re.IGNORECASE,
    ),
    # Windows 파일시스템 경로 전체 (드라이브 문자 포함, 사용자명/비사용자명 무관)
    re.compile(
        r"([A-Za-z]:[\\/](?:Users|home|private|secret|config|\.local|Desktop|Documents)[\\/])[^\"'<>\s]+",
        re.IGNORECASE,
    ),
    # POSIX 시스템/사용자 디렉토리 경로
    re.compile(
        r"(/(?:home|Users|etc|var|root|tmp|opt|srv|private|secret)/[^\"'<>\s]+)", re.IGNORECASE
    ),
    # traceback 헤더 및 File 프레임 (독립적으로도 매칭)
    re.compile(r"Traceback\s*\(most\s+recent\s+call\s+last\)", re.IGNORECASE),
    re.compile(r'(File\s+"[^"]+",\s*line\s+\d+[^\n]*)', re.IGNORECASE),
]


def _sanitize_text(text: str) -> str:
    """traceback/메시지에서 민감 정보(시크릿, 사용자 경로 등)를 마스킹한다."""
    if not isinstance(text, str):
        text = str(text)
    for pat in _SENSITIVE_PATTERNS:
        if pat.groups >= 2:
            # key=value 패턴: 키 이름은 유지, 값만 [REDACTED].
            text = pat.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


# raw API 응답에서 LLM에게 노출해도 안전한 키만 남기고 나머지는 제거.
# 네이버 커머스 API 에러 응답에서 디버그에 유용한 최소 필드만 허용.
_SAFE_BODY_KEYS = frozenset(
    {
        "code",
        "message",
        "status",
        "detail",
        "invalidInputs",
        "name",
        "type",
        "reason",
        "invalidReason",
        "originProductNo",
        "channelProductNo",
        naver_client.SELLER_TAG_AUTOSTRIP_KEY,
    }
)


def _sanitize_body(body: Any, _depth: int = 0) -> Any:
    """API 응답 본문에서 민감하지 않은 최소 필드만 남긴다.

    네이버 커머스 API 에러 응답의 경우 전체 본문을 LLM에게 노출하면
    내부 필드가 누출될 수 있다. 화이트리스트 방식으로 ``code``,
    ``message``, ``invalidInputs[].name`` 등 디버그에 필요한 최소
    정보만 보존한다. 문자열 값은 추가로 ``_sanitize_text`` 로 위생화.

    200 OK 응답의 본문은 그대로 통과시킨다(호출자가 이미 ok 플래그로
    제어하므로 에러 케이스만 가지치기).
    """
    if _depth == 0 and isinstance(body, dict):
        # 최상위 에러 응답: 화이트리스트 키만 추출.
        if "code" in body or "invalidInputs" in body or "status" in body:
            pruned: dict[str, Any] = {}
            for key in body:
                if key in _SAFE_BODY_KEYS:
                    pruned[key] = _sanitize_body(body[key], _depth + 1)
            return pruned if pruned else body
    if isinstance(body, dict):
        return {k: _sanitize_body(v, _depth + 1) for k, v in body.items()}
    if isinstance(body, list):
        return [_sanitize_body(v, _depth + 1) for v in body]
    if isinstance(body, str):
        return _sanitize_text(body)
    return body


def _sanitize_error(exc: BaseException) -> str:
    """예외 객체로부터 타입+메시지를 추출해 sanitized 문자열을 반환한다.

    traceback 전체를 LLM 에게 노출하면 민감한 경로/키가 노출될 수 있으므로
    예외 타입명과 메시지만 간결하게.
    """
    type_name = type(exc).__name__
    msg = _sanitize_text(str(exc))
    return f"{type_name}: {msg}"


# check_config 가 "아직 설정되지 않음" 으로 간주하는 플레이스홀더 값들.
_PLACEHOLDER_TOKENS = (
    "REPLACE_WITH_",
    "{STORE_SLUG}",
    "{STORE_NAME}",
)


def _is_placeholder(value: Any) -> bool:
    """값이 config.example.json 의 치환 전 플레이스홀더인지 판별."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return True
    return any(token in text for token in _PLACEHOLDER_TOKENS)


def _required_naver_keys() -> tuple[str, ...]:
    return ("client_id", "client_secret", "store_url_slug")


def _resolve_upload_root() -> str:
    """업로드 루트 디렉토리 결정.

    우선순위:
      1. 환경변수 ``CLOSSIFY_UPLOAD_ROOT``
      2. 현재 작업 디렉토리(cwd)
    """
    env_root = os.environ.get("CLOSSIFY_UPLOAD_ROOT")
    if env_root and env_root.strip():
        return os.path.normpath(os.path.expandvars(os.path.expanduser(env_root.strip())))
    return os.getcwd()


def _resolve_upload_path(raw_path: str) -> str:
    """사용자가 준 경로를 절대경로로 정규화.

    상대경로인 경우 ``CLOSSIFY_UPLOAD_ROOT`` 기준으로 해석한다.
    이미 절대경로면 그대로 사용한다.
    """
    if os.path.isabs(raw_path):
        return os.path.normpath(raw_path)
    return os.path.normpath(os.path.join(_resolve_upload_root(), raw_path))


def _config_require_preview_confirmation() -> bool:
    """config 의 ``require_preview_confirmation`` 설정을 읽는다.

    기본값은 ``True`` (켬). 위험한 쪽이 기본이 되면 안 된다. config 에 키가
    없거나 값이 bool 이 아니면 기본값(True) 을 반환한다 — 조용히 끄지 않는다.
    """
    cfg_path = naver_client.config_path()
    try:
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return True
    value = cfg.get("require_preview_confirmation")
    if isinstance(value, bool):
        return value
    return True


# --------------------------------------------------------------------------- #
# 결정론 컴플라이언스 게이트 (fail-closed).
#
# register_product 가 네이버 API 를 호출하기 직전에 결정론 검사를 실행한다.
# LLM 판단이 필요한 항목(카피/이미지 QA)은 위임 왕복이 붙기 전까지
# 항상 미회신(PENDING) 상태이므로, 본 게이트는 **결정론 위반(FAIL)만 차단**하고
# LLM 판단 미회신은 차단하지 않되 응답에 ``pending_reviews`` 로 표기한다.
# --------------------------------------------------------------------------- #

# 고시 필드 이름 → 한국어 라벨/사유 매핑.
# data/notice_types.json 의 필드명은 camelCase 영어라서 사용자가 바로 이해하기
# 어렵다. 이 매핑은 거부 응답의 needs_user 항목에 사람이 읽을 수 있는 안내를
# 제공하기 위해 사용한다. 정책: "필드명만 던지지 말고 사람이 이해할 라벨·
# 사유를 함께 준다."
#
# 라벨 데이터는 data/notice_field_labels.json 에서 읽는다(단일 진실 공급원).
# 이전에는 본 모듈에 dict 로 하드코딩되어 있었으나, 문서와 코드가 갈라지는
# 문제로 데이터 파일로 분리했다. 라벨을 새로 창작하지 않는다 — 출처 기반
# 수집만 data/notice_field_labels.json 의 labels 에 추가한다.
_NOTICE_LABELS_PATH = os.path.join(
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..")),
    "data",
    "notice_field_labels.json",
)

# 1회 캐시 — 매 호출마다 디스크를 읽지 않는다. None 은 "로드 시도 전",
# dict 는 "로드 결과(성공 시 항목, 실패 시 빈 dict → 폴백)" 을 뜻한다.
_notice_labels_cache: dict[str, tuple[str, str]] | None = None


def _load_notice_field_labels() -> dict[str, tuple[str, str]]:
    """data/notice_field_labels.json 을 1회 로드해 캐싱한다.

    파일이 없거나 깨졌을 때: 조용히 죽지 않고 기존 폴백(필드명 그대로 +
    "이 카테고리 고시 필수 항목입니다")으로 떨어지되, 그 사실이 stderr 에
    드러난다(조용한 축소 금지). 호출자에게는 빈 dict 를 반환해 _notice_field_label
    이 필드명 폴백을 적용하도록 한다. 예외를 밖으로 던지지 않는다 —
    needs_user 조립 경로가 컴플라이언스 게이트 안에서 호출되므로, 예외 전파는
    등록 차단(fail-closed 게이트의 오작동)으로 이어진다.
    """
    global _notice_labels_cache
    if _notice_labels_cache is not None:
        return _notice_labels_cache
    loaded: dict[str, tuple[str, str]] = {}
    try:
        with open(_NOTICE_LABELS_PATH, encoding="utf-8") as f:
            doc = json.load(f)
        labels = doc.get("labels") if isinstance(doc, dict) else None
        if isinstance(labels, dict):
            for name, entry in labels.items():
                if not isinstance(entry, dict):
                    continue
                label = entry.get("label")
                hint = entry.get("hint")
                if isinstance(label, str) and isinstance(hint, str):
                    loaded[name] = (label, hint)
    except (OSError, ValueError) as exc:
        # 폴백으로 떨어지되 사실을 stderr 에 남긴다 (조용한 축소 금지).
        # 빈 dict 를 캐싱해 이후 호출이 디스크를 반복해 읽지 않게 한다.
        import sys

        print(
            f"[clossify] notice_field_labels.json 로드 실패 — 필드명 폴백 적용: {exc}",
            file=sys.stderr,
        )
    _notice_labels_cache = loaded
    return loaded


def _notice_field_label(field: str) -> tuple[str, str]:
    """고시 필드명에 대한 (라벨, 사유) 반환. 매핑 없으면 필드명 그대로.

    반환 형태 ``(라벨, 사유)`` 는 호출자가 의존하므로 변경하지 않는다.
    """
    labels = _load_notice_field_labels()
    return labels.get(field, (field, "이 카테고리 고시 필수 항목입니다"))


def _notice_field_answer_shape(field: str) -> str:
    """고시 필드의 타입에서 산출된 답변 형태 안내 문자열.

    ``data/notice_field_types.json`` 의 타입 정보를 읽어 사용자가 어떤 형태로
    답해야 하는지 안내한다. 이 안내는 needs_user 항목의 answer_shape 키에 실려
    클라이언트 LLM 에게 전달된다 — boolean 필드를 자유 텍스트로 물으면 사용자가
    문장을 쓰고, 그것을 조용히 true/false 로 변환하면 잘못 신고된다.

    라벨을 새로 창작하지 않는다 — 데이터에 기록된 타입에서 기계적으로 산출한다.
    미기재 필드(문자열)는 빈 문자열을 반환해 기존 동작을 보존한다.
    """
    ftype = qa_agents._notice_field_type(field)
    if ftype == "boolean":
        return "예/아니오 질문입니다. true 또는 false(Python bool) 로 답해주세요."
    if ftype == "date":
        return "날짜 항목입니다. 정확한 형식은 네이버 고시 스펙을 확인해주세요."
    # string/미기재 — 기존 동작(자유 텍스트). 빈 문자열로 둬 기존 필드와 회귀 없이.
    return ""


def _category_path_for(category_id: str) -> str:
    """``category_id`` 의 카테고리 경로를 반환 (알 수 없으면 빈 문자열).

    ``qa_agents._infer_notice_type`` 이 카테고리 경로에서 고시 타입을
    추론할 수 있도록 돕는다. 데이터 파일 부재/알 수 없는 ID 는 조용히
    빈 문자열로 떨어진다 (이 경우 ETC 기본값 사용).
    """
    try:
        from . import category_meta

        return category_meta.category_path(category_id, raise_if_unknown=False)
    except Exception:
        return ""


def _build_compliance_context(
    name: str,
    category_id: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """컴플라이언스 검사용 context 와 검사 대상 notice dict 를 구성.

    ``naver_client.build_payload`` 는 FURNITURE 가 아닌 모든 카테고리에 대해
    ``productInfoProvidedNoticeType: "ETC"`` 를 하드코딩한다. 하지만 실제로는
    의류(WEAR), 신발(SHOES) 등 카테고리에 따라 전혀 다른 필수 필드가 적용된다.
    본 함수는 카테고리 경로에서 올바른 고시 타입을 추론해 notice dict 에
    반영한 뒤, ``_compliance_code_check`` 가 올바른 필수 필드 목록으로
    검사하도록 돕는다.

    Returns:
        ``(context, effective_notice)`` — context 는 ``_compliance_code_check`` 에
        전달할 dict, effective_notice 는 타입이 보정된 notice dict.
    """
    origin_product = payload.get("originProduct") if isinstance(payload, dict) else {}
    detail_attr = origin_product.get("detailAttribute") if isinstance(origin_product, dict) else {}
    notice = detail_attr.get("productInfoProvidedNotice") if isinstance(detail_attr, dict) else {}
    if not isinstance(notice, dict):
        notice = {}

    category_path = _category_path_for(category_id)
    # 카테고리 경로 기반으로 올바른 고시 타입 추론.
    inferred_type = qa_agents._infer_notice_type(
        {
            "category_path": category_path,
            "category_name": category_path,
        }
    )
    effective_notice = dict(notice)
    # naver_client 가 ETC 로 설정했더라도, 카테고리가 더 구체적인 타입을
    # 요구하면 그것으로 보정한다 (ETC 는 catch-all 기본값일 뿐).
    current_type = str(effective_notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    if inferred_type != "ETC" and (current_type == "ETC" or not current_type):
        effective_notice["productInfoProvidedNoticeType"] = inferred_type
        # notice node 키가 추론된 타입의 node 와 다르면 올바른 node 를 추가.
        spec = qa_agents._notice_type_spec(inferred_type)
        expected_node = (spec or {}).get("node") if spec else None
        if expected_node and expected_node not in effective_notice:
            # 기존 etc/furniture 노드의 필드를 올바른 노드로 복사(최선 노력).
            for fallback_key in ("etc", "furniture"):
                fb = effective_notice.get(fallback_key)
                if isinstance(fb, dict):
                    effective_notice[expected_node] = dict(fb)
                    break
            else:
                effective_notice[expected_node] = {}

    context: dict[str, Any] = {
        "category_id": category_id,
        "category_path": category_path,
        "category_name": category_path,
    }
    return context, effective_notice


def _payload_notice_type(payload: dict[str, Any]) -> str:
    """빌드된 페이로드에서 실제 신고되는 고시 타입을 추출한다.

    페이로드의 ``originProduct.detailAttribute.productInfoProvidedNotice
    .productInfoProvidedNoticeType`` 에서 읽는다. 이것이 네이버 API 로
    실제 송신되는 값이다. 게이트의 판정 타입과 이 값이 다르면,
    판매자가 게이트 질문에 답하고 통과했는데 실제 신고는 다른 타입으로
    나가는 조용한 잘못 신고가 된다.
    """
    try:
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        return str(notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    except (AttributeError, TypeError):
        return ""


def _gate_notice_type(category_id: str, product: dict[str, Any] | None = None) -> str:
    """게이트(컴플라이언스 컨텍스트)가 판정하는 고시 타입을 반환한다.

    ``_build_compliance_context`` 와 동일한 조회 경로(``_category_path_for``
    → ``qa_agents._infer_notice_type``)를 쓴다. 명시 타입이 product 에 있으면
    그것이 최우선이다(기존 규칙 회귀 방지).
    """
    ctx_product = product or {}
    explicit = ""
    user_notice = ctx_product.get("notice") if isinstance(ctx_product, dict) else None
    if isinstance(user_notice, dict):
        explicit = (
            user_notice.get("productInfoProvidedNoticeType") or user_notice.get("notice_type") or ""
        )
    if not explicit and isinstance(ctx_product, dict):
        explicit = (
            ctx_product.get("notice_type") or ctx_product.get("productInfoProvidedNoticeType") or ""
        )
    if explicit:
        return str(explicit).strip().upper()
    category_path = _category_path_for(category_id)
    return qa_agents._infer_notice_type(
        {"category_path": category_path, "category_name": category_path}
    )


def _extract_status_type(body: Any) -> str:
    """API 응답 본문에서 ``statusType`` 값을 추출한다 (대문자 정규화).

    생성 응답과 조회 응답 모두에서 시도한다:
      - ``body.originProduct.statusType``
      - ``body.statusType``
      - ``body.originProduct.originProduct.statusType`` (중첩 케이스)
    """
    if not isinstance(body, dict):
        return ""
    candidates = []
    op = body.get("originProduct")
    if isinstance(op, dict):
        candidates.append(op.get("statusType"))
        inner = op.get("originProduct")
        if isinstance(inner, dict):
            candidates.append(inner.get("statusType"))
    candidates.append(body.get("statusType"))
    for val in candidates:
        text = str(val or "").strip().upper() if val else ""
        if text:
            return text
    return ""


def _run_compliance_gate(
    name: str,
    category_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """결정론 컴플라이언스 검사를 실행하고 정형화된 결과를 반환.

    Returns:
        ``{"blocked": bool, "violations": [...], "needs_user": [...],
        "pending_reviews": [...]}``

        - ``blocked``: FAIL 심각도 위반이 하나라도 있으면 True.
        - ``violations``: FAIL 심각도 위반 항목(사용자 가독 형태).
        - ``needs_user``: 비어 있는 고시 필수 필드에서 산출한 사용자 입력 요청.
        - ``pending_reviews``: LLM 판단이 필요해 대기 중인 검사 항목.
    """
    context, effective_notice = _build_compliance_context(name, category_id, payload)

    # _compliance_code_check 는 api_payload 의 notice 를 우선 읽는다.
    # 여기서는 effective_notice(타입 보정됨)를 context.notice 로 넣고,
    # api_payload 에서는 detailAttribute 를 제외한 채로 넘겨 notice 가
    # context 경로로 읽히도록 유도한다. 단 originAreaInfo/KC/A-S 정보는
    # api_payload 에서 읽어야 하므로, effective_notice 를 payload 에도 반영한다.
    effective_payload = dict(payload) if isinstance(payload, dict) else {}
    if isinstance(effective_payload.get("originProduct"), dict):
        op = dict(effective_payload["originProduct"])
        da = dict(op.get("detailAttribute")) if isinstance(op.get("detailAttribute"), dict) else {}
        da["productInfoProvidedNotice"] = effective_notice
        op["detailAttribute"] = da
        effective_payload["originProduct"] = op
    context["notice"] = effective_notice

    check_result = qa_agents._compliance_code_check(name, context, api_payload=effective_payload)

    fail_violations = []
    for row in check_result.get("violations") or []:
        if isinstance(row, dict) and str(row.get("severity") or "").upper() == qa_agents.FAIL:
            fail_violations.append(
                {
                    "rule": str(row.get("rule") or "컴플라이언스"),
                    "detail": str(row.get("detail") or ""),
                }
            )

    # needs_user: 결정론 위반 중 "고시 필수필드" / "고시 필드 상호배제" 위반에서
    # 사용자 입력 요청을 조립. 요구되는 구조:
    #   {"field": ..., "label": ..., "why": ..., "answer_shape": ...}
    # answer_shape 는 해당 필드의 타입에서 산출된 답변 형태 안내다:
    #   - boolean → "예/아니오 질문입니다. true 또는 false 로 답해주세요."
    #   - date → "날짜 항목입니다. 형식은 네이버 고시 스펙을 확인해주세요."
    #   - string/미기재 → 빈 문자열(기존 동작 — 자유 텍스트).
    # boolean 필드를 자유 텍스트로 물으면 사용자가 문장을 쓰고, 그 문자열을
    # 조용히 true/false 로 변환하면 잘못 신고된다. answer_shape 가 예/아니오
    # 질문임을 드러내면 클라이언트 LLM 이 올바른 형태의 질문을 만든다.
    #
    # "고시 필드 상호배제"(XOR) 위반은 누락과 반대 방향이다 — 사용자가 *너무
    # 많이* 제공했다. needs_user 항목의 why 에 "둘 중 하나만" 임을 드러내고
    # field 에는 그룹 전체를 ", " 로.join 해 올려 클라이언트 LLM 이 어떤 필드들이
    # 충돌하는지 알 수 있게 한다 (조용한 선택 금지 — 어느 하나를 버리지 않는다).
    needs_user: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for row in check_result.get("violations") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("severity") or "").upper() != qa_agents.FAIL:
            continue
        rule_name = str(row.get("rule") or "")
        detail_text = str(row.get("detail") or "")
        if rule_name == "고시 필수필드":
            # detail 형태: "고시 타입 WEAR 필수 필드 누락: material, size, color"
            if "누락:" in detail_text:
                after = detail_text.split("누락:", 1)[1]
                for field in after.split(","):
                    field = field.strip()
                    if field and field not in seen_fields:
                        seen_fields.add(field)
                        label, why = _notice_field_label(field)
                        answer_shape = _notice_field_answer_shape(field)
                        needs_user.append(
                            {
                                "field": field,
                                "label": label,
                                "why": why,
                                "answer_shape": answer_shape,
                            }
                        )
        elif rule_name == "고시 필드 상호배제":
            # XOR "둘 다 채워짐" 위반. detail 에 전체 그룹 필드명이 들어있다.
            # field 자리에 그룹 전체를 올려 클라이언트가 충돌을 인식하게 한다.
            # 중복 보고 방지용 키는 detail 전체로 잡는다(같은 그룹이 여러 번
            # 보고될 일은 없지만 방어적으로).
            dedup_key = "xor:" + detail_text
            if dedup_key in seen_fields:
                continue
            seen_fields.add(dedup_key)
            needs_user.append(
                {
                    "field": "(상호배제 그룹)",
                    "label": "고시 필드 상호배제",
                    "why": detail_text,
                    "answer_shape": (
                        "네이버가 이 필드들을 상호배제(XOR) 로 다룹니다. "
                        "둘 중 하나만 남기고 나머지를 비워주세요."
                    ),
                }
            )

    pending_reviews: list[str] = []
    # 카피/이미지 QA 는 위임 왕복이 붙기 전까지 항상 미회신이다.
    # 결정론 게이트 통과 시 이 사실을 응답에 표기한다 (조용한 생략 금지).
    # 단, 결정론 FAIL 로 차단된 경우에는 pending_reviews 가 무의미하므로 빈 리스트.
    if not fail_violations:
        pending_reviews = [
            "copy_qa: 카피 품질 LLM 판단 대기 중",
            "image_qa: 이미지 적합성 LLM 판단 대기 중",
        ]

    return {
        "blocked": bool(fail_violations),
        "violations": fail_violations,
        "needs_user": needs_user,
        "pending_reviews": pending_reviews,
    }


@mcp.tool()
def check_config() -> dict[str, Any]:
    """네이버 커머스 API 자격증명/설정 상태를 검사한다 (외부 API 호출 없음).

    ``.local/config.json`` 파일의 존재, JSON 파싱 가능 여부, 그리고
    ``naver.client_id`` / ``naver.client_secret`` / ``naver.store_url_slug``
    세 키의 존재 및 플레이스홀더 미사용 여부를 확인한다.
    LLM은 이 도구로 "설정이 완료되었는가?" 를 분기 없이 확인할 수 있다.

    Returns:
        ``{"ok": bool, "config_path": str, "present": {...}, "missing": [...],
        "placeholders": [...], "origin_configured": bool, "origin_hint": str,
        "as_tel_configured": bool, "as_tel_hint": str, "error": str | None}``
        - ``ok``: 모든 필수 키가 존재하고 플레이스홀더가 아님.
        - ``present``: 필수 키별 현재 값의 *존재 여부* (값 자체는 노출 안 함).
        - ``missing``: 누락된 필수 키 이름 목록.
        - ``placeholders``: 플레이스홀더로 남아있는 필수 키 이름 목록.
        - ``origin_configured``: 원산지 정본 설정 여부(값 미노출).
        - ``as_tel_configured``: AS 전화번호 정본 설정 여부(값 미노출).
        - ``error``: 파일이 없거나 JSON 파싱에 실패한 경우의 메시지.

    안내: 실제 값은 반환하지 않는다. 이 도구는 가시성이 아니라 게이트(gate)다.
    """
    # 매 호출마다 최신 경로를 사용 (CLOSSIFY_CONFIG 오버라이드 반영).
    cfg_path = naver_client.config_path()
    result: dict[str, Any] = {
        "ok": False,
        "config_path": cfg_path,
        "present": {},
        "missing": [],
        "placeholders": [],
        "error": None,
    }

    if not os.path.isfile(cfg_path):
        result["error"] = (
            f"config 파일이 없습니다: {cfg_path}. "
            "config.example.json 을 .local/config.json 으로 복사한 뒤 실제 값으로 채우세요."
        )
        return result

    try:
        with open(cfg_path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        result["error"] = _sanitize_text(f"config 파일을 읽거나 파싱할 수 없습니다: {exc}")
        return result

    naver = cfg.get("naver")
    if not isinstance(naver, dict):
        result["error"] = "config 의 'naver' 섹션이 객체가 아닙니다."
        return result

    missing: list[str] = []
    placeholders: list[str] = []
    present: dict[str, bool] = {}

    for key in _required_naver_keys():
        value = naver.get(key)
        exists = key in naver and not _is_placeholder(value)
        present[key] = exists
        if key not in naver or value is None:
            missing.append(key)
        elif _is_placeholder(value):
            placeholders.append(key)

    result["present"] = present
    result["missing"] = missing
    result["placeholders"] = placeholders

    # 원산지 설정 여부 점검 항목 추가.
    # 값 자체는 반환하지 않고 채워짐/비어있음만 보고한다.
    # 원산지가 설정되어 있지 않으면 register_product 의 컴플라이언스 게이트가
    # 등록을 거부한다 — 사용자에게 이 사실을 안내한다.
    origin_set = False
    notice_defaults = cfg.get("smartstore_notice_defaults")
    if isinstance(notice_defaults, dict):
        origin_area_code = notice_defaults.get("origin_area_code")
        origin_content = notice_defaults.get("origin_content")
        origin_set = (
            bool(origin_area_code)
            and not _is_placeholder(origin_area_code)
            and bool(origin_content)
            and not _is_placeholder(origin_content)
        )
    result["origin_configured"] = origin_set
    if not origin_set:
        result["origin_hint"] = (
            "원산지(smartstore_notice_defaults.origin_area_code 및 origin_content)가 "
            "설정되지 않았습니다. register_product 가 컴플라이언스 검사에서 "
            "등록을 거부합니다."
        )

    # AS 전화번호 정본 위치 점검 항목 추가.
    # 정본은 smartstore_notice_defaults.as_tel 이다 (naver_client._notice_defaults 가
    # cfg_notice.get("as_tel") 로 읽는 자리). 값 자체는 반환하지 않고
    # 채워짐/비어있음만 보고한다. 미설정 시 등록이 거부된다는
    # 안내를 포함한다.
    as_tel_set = False
    if isinstance(notice_defaults, dict):
        as_tel_value = notice_defaults.get("as_tel")
        as_tel_set = bool(as_tel_value) and not _is_placeholder(as_tel_value)
    result["as_tel_configured"] = as_tel_set
    if not as_tel_set:
        result["as_tel_hint"] = (
            "AS 전화번호(smartstore_notice_defaults.as_tel)가 설정되지 않았습니다. "
            "register_product 가 컴플라이언스 검사에서 등록을 거부합니다. "
            "안내문구/플레이스홀더를 넣으면 거부됩니다 (fail-closed)."
        )

    result["ok"] = not missing and not placeholders
    return result


@mcp.tool()
def upload_images(paths: list[str]) -> dict[str, Any]:
    """로컬 이미지 파일 경로 리스트를 네이버 이미지서버에 업로드한다.

    업로드된 이미지들은 네이버 CDN의 secure URL 로 반환되며, 이 URL 들은
    ``register_product`` 의 ``image_urls`` 인자로 그대로 전달된다.

    Args:
        paths: 업로드할 로컬 이미지 파일의 절대/상대 경로 리스트.
            첫 번째 이미지가 상품 대표 이미지가 된다.
            상대경로는 ``CLOSSIFY_UPLOAD_ROOT`` 환경변수(기본: cwd) 기준으로 해석.

    Returns:
        ``{"ok": bool, "image_urls": [str, ...], "count": int, "error": str | None}``
        성공 시 ``image_urls`` 는 업로드 순서와 동일한 URL 리스트.

    주의:
        - 허용 확장자: ``.jpg``, ``.jpeg``, ``.png``, ``.webp``.
        - 단일 파일 크기 상한: 10MB.
        - 검증은 정본 가드 ``images.validate_local_image`` 에 위임한다.
          확장자 위장·심링크·디렉터리·루트 밖 절대경로 등이 여기서 차단된다.
        - 인증 토큰은 ``naver_client.get_token()`` 이 내부에서 발급·사용한다.
        - 설정이 완료되지 않았다면 ``check_config`` 를 먼저 호출하라.
    """
    if not isinstance(paths, list) or not paths:
        return {
            "ok": False,
            "image_urls": [],
            "count": 0,
            "error": "paths 는 최소 1개 이상의 이미지 경로 리스트여야 합니다.",
        }

    bad_type = [p for p in paths if not isinstance(p, str)]
    if bad_type:
        return {
            "ok": False,
            "image_urls": [],
            "count": 0,
            "error": "paths 의 각 원소는 문자열이어야 합니다.",
        }

    # 정본 로컬 이미지 가드로 교체. 도구 시그니처/반환 계약 유지.
    # mcp_server._MAX_IMAGE_BYTES 를 max_bytes 오버라이드로 넘겨 기존 테스트가
    # monkeypatch 한 상한을 존중한다.
    from . import images as _images_mod  # 방향: mcp_server -> images (허용)

    resolved: list[str] = []
    for raw in paths:
        v = _images_mod.validate_local_image(raw, max_bytes=_MAX_IMAGE_BYTES)
        if not v["ok"]:
            reason = "; ".join(v["errors"]) if v["errors"] else "검증 실패"
            return {
                "ok": False,
                "image_urls": [],
                "count": 0,
                "error": f"이미지 검증 실패 ({raw}): {reason}",
            }
        resolved.append(v["path"])

    try:
        urls = naver_client.upload_images(resolved)
    except Exception as exc:  # 네트워크/인증/서버 에러 — sanitized (Fix 7)
        return {
            "ok": False,
            "image_urls": [],
            "count": 0,
            "error": f"이미지 업로드 실패: {_sanitize_error(exc)}",
        }

    return {
        "ok": True,
        "image_urls": urls,
        "count": len(urls),
        "error": None,
    }


@mcp.tool()
def register_product(
    name: str,
    price: int,
    *,
    image_urls: list[str] | None = None,
    category_id: str,
    detail_html: str | None = None,
    product_key: str | None = None,
    options: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    status: str = "SALE",
    stock: int = 1,
    delivery_fee: int = 3000,
    courier: str = "CJGLS",
    notice: dict[str, Any] | None = None,
    preview_confirmed: bool = False,
) -> dict[str, Any]:
    """상품 정보를 받아 등록 페이로드를 빌드하고 네이버 커머스 API 로 등록한다.

    본 도구는 naver_client.build_payload() + register_product() 를 순차 호출한다.
    페이로드 빌딩·고시 정보 자동 완성·판매자태그 제한어 자동 제거 등의 복잡도는
    naver_client 가 처리한다.

    ``image_urls`` 와 ``detail_html`` 은 생략 가능하다(기본값 ``None``). 둘 중
    하나라도 비어 있으면, ``name``+``price`` 에서 product_key 를 유도해
    prepared payload(``prepare_listing`` 이 저장한 결과)를 조회하고, **거기에
    저장된 값으로 채운다**. 명시적으로 준 값이 항상 우선하며, prepared 가
    덮어쓰지 않는다. 채운 뒤에도 기존 검증(원본 이미지 진입 게이트, prepared
    QA 게이트)은 전부 그대로 통과해야 한다 — 이 경로가 검증을 우회하는 뒷문이
    되면 안 된다. 둘 다 비어 있고 prepared payload 도 없으면 거부한다(무동작·
    빈 등록 금지).

    Args:
        name: 상품명 (네이버 정책상 길이 제한이 있음, naver_client 가 50자 컷).
        price: 판매가 (KRW, 양의 정수).
        category_id: 네이버 상품 카테고리 트리의 리프 카테고리 ID.
        image_urls: ``upload_images`` 가 반환한 CDN URL 리스트. 생략 시
            prepared payload 의 정규화된 이미지 URL 사용. 첫 번째 URL 이
            대표 이미지가 된다.
        detail_html: 상세페이지 HTML (``<html>``... 또는 조각 HTML). 생략 시
            prepared payload 의 상세 HTML 사용.
        product_key: ``prepare_listing`` 이 반환한 prepared payload 키. 같은
            이름·가격의 SKU(색상만 다른 옵션 상품 등) 가 여러 개일 때
            ``name``+``price`` 유도 키로는 어느 prepared 가 이 등록의 것인지
            구분할 수 없다 — 준비 단계가 반환한 키를 그대로 넘기면 모호성이
            사라진다. 생략 시 ``name``+``price`` 유도 키로 동작한다(하위호환).
        options: 옵션 조합 목록. 각 원소는 ``{"name": str, "stock": int,
            "price": int}`` 또는 ``optionName1..3`` 형태. 단일 옵션 상품은
            생략 가능.
        tags: 판매자태그(SEO) 문자열 리스트. 제한어는 자동 제거/재시도된다.
        status: ``"SALE"`` (판매중) 또는 ``"SUSPENSION"`` (판매중지).
            기본값 ``"SALE"``.
        stock: 단일 품목(옵션 없음)일 때의 재고. ``options`` 제공 시 무시되고
            옵션별 재고 합으로 계산된다.
        delivery_fee: 기본 배송비 (KRW). 기본 3000.
        courier: 택배사 코드. 기본 ``"CJGLS"``.
        notice: 상품정보제공고시 오버라이드. ``{"productInfoProvidedNoticeType":
            "ETC"|"FURNITURE", ...}`` 형태. 미제공 시 naver_client 가
            카테고리/기본값으로 자동 완성.
        preview_confirmed: 미리보기 승인 게이트. 설정의
            ``require_preview_confirmation`` 이 켜져 있을 때(기본 켬),
            ``preview_confirmed`` 가 ``True`` 가 아니면 등록을 거부하고
            미리보기 파일 경로를 사유에 포함한다 (네이버 호출 0회). 이것은
            **선언 게이트**다 — 사용자가 실제로 미리보기를 봤는지 기계적으로
            알 수 없으므로, "``True`` 로 회신했다" 는 선언일 뿐이다.

    Returns:
        ``{"ok": bool, "status_code": int | None, "origin_product_no": str | None,
        "raw": Any, "seller_tags": {...} | None, "error": str | None}``
        - ``ok``: HTTP 상태가 2xx(성공)인지.
        - ``raw``: API 응답 본문 (에러 메시지 포함 가능).
        - ``seller_tags``: 제한어 자동 제거 메타가 있을 때만 존재.
        - ``filled_from_prepared``: prepared payload 에서 채운 항목 리스트
          (``"detail_html"``/``"image_urls"``). 직접 준 값은 포함되지 않는다.
        - ``prepared_key_used``: 실제로 prepared 조회에 쓴 product_key.
          유도한 키를 썼을 때와 달리 어디서 가져왔는지 드러낸다 (조용한
          치환 방지).
        - ``notice_filled_from_config``: 설정에서 자동으로 채워진 규제값 필드
          이름 리스트. 성공·차단·실패 모든 경로에서 나타난다 (조용한 자동
          채움 금지). 비었으면 빈 리스트.

    Note:
        환경변수 ``COMMERCE_DRY_RUN=1`` 시 실제 등록 없이 페이로드를
        ``.local/dry_run_payload.json`` 에 덤프한다 (naver_client 동작).
        리허설 모드에서도 컴플라이언스 게이트는 동일하게 실행되며, 반환에
        ``dry_run: true`` 가 표시된다.
    """
    _dry_run = os.environ.get("COMMERCE_DRY_RUN") == "1"
    if not isinstance(name, str) or not name.strip():
        return _fail("name 은 비어있지 않은 문자열이어야 합니다.", dry_run=_dry_run)
    if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        return _fail("price 는 0보다 큰 정수(KRW)여야 합니다.", dry_run=_dry_run)
    if not isinstance(category_id, str) or not category_id.strip():
        return _fail("category_id 는 비어있지 않은 문자열이어야 합니다.", dry_run=_dry_run)
    if status not in {"SALE", "SUSPENSION"}:
        return _fail("status 는 'SALE' 또는 'SUSPENSION' 이어야 합니다.", dry_run=_dry_run)

    # product_key 결정 — 명시 인자가 있으면 그것을, 없으면 이름+가격으로
    # 후보를 찾는다. 같은 이름·가격의 SKU 가 여러 개일 때(색상만 다른 옵션
    # 상품 등) 조용히 하나를 고르면 다른 상품이 전송되는 조용한 오등록이
    # 된다 — 모호하면 거부다(네이버 호출 0회). ``resolve_prepared_for_register``
    # 가 이 판정을 담당한다.
    _explicit_key = product_key if isinstance(product_key, str) and product_key.strip() else None
    try:
        _resolved_payload, prepared_lookup = _register_mod.resolve_prepared_for_register(
            name, int(price), product_key=_explicit_key
        )
    except ValueError as _amb_exc:
        # 후보가 2개 이상 — 모호성으로 거부. 네이버 호출 0회.
        return {
            "ok": False,
            "status_code": None,
            "origin_product_no": None,
            "channel_product_no": None,
            "missing_channel_no": True,
            "name_truncated": False,
            "raw": None,
            "seller_tags": None,
            "blocked_by": "ambiguous_prepared",
            "filled_from_prepared": [],
            "prepared_lookup": {},
            "notice_filled_from_config": [],
            "dry_run": _dry_run,
            "message": (
                f"{_amb_exc}. name+price 로는 어느 prepared 가 이 등록의 것인지 " "결정할 수 없다."
            ),
            "error": _sanitize_text(str(_amb_exc)),
        }
    # 호환성: 기존 코드가 _product_key 를 사용한다. resolved 가 있으면 그 키를,
    # 없으면 명시 키(비어있을 수 있다)를 쓴다. 둘 다 없으면 이름+가격 유도.
    _product_key_source = prepared_lookup.get("source") or "none"
    if _resolved_payload is not None:
        _product_key = str(_resolved_payload.get("product_key") or "").strip()
    elif _explicit_key:
        _product_key = _register_mod._sanitize_product_key(_explicit_key)
        _product_key_source = "explicit"
    else:
        try:
            _product_key = _register_mod.make_product_key(name, int(price))
            _product_key_source = "derived"
        except Exception:
            _product_key = None
    # prepared_lookup.source 가 "none" 이지만 _product_key 가 유도된 경우 보정.
    if _product_key_source == "none" and _product_key:
        _product_key_source = "derived"
        prepared_lookup = {
            "key": _product_key,
            "source": "derived",
            "name": "",
            "salePrice": None,
        }

    # ------------------------------------------------------------------ #
    # 미리보기 승인 게이트 (선언 게이트).
    #
    # 설정의 require_preview_confirmation 이 켜져 있을 때(기본 켬),
    # preview_confirmed 가 True 가 아니면 등록을 거부하고 미리보기 파일
    # 경로를 사유에 포함한다 (네이버 호출 0회). 이것은 선언 게이트다 —
    # 사용자가 실제로 미리보기를 봤는지 기계적으로 알 수 없으므로,
    # "True 로 회신했다" 는 선언일 뿐이다. 원본 사진 게이트와 같은 성격이며,
    # 문서에 그렇게 정직하게 적는다. 과장 금지.
    # ------------------------------------------------------------------ #
    _preview_path_for_gate: str | None = None
    if isinstance(_resolved_payload, dict):
        _preview_path_for_gate = _resolved_payload.get("preview_path") or None
    _require_preview = _config_require_preview_confirmation()
    if _require_preview and not preview_confirmed:
        _msg_parts = [
            "미리보기 승인 없이 등록을 거부했습니다 (require_preview_confirmation 켜짐).",
            "브라우저로 미리보기 파일을 열어 내용을 확인한 뒤 preview_confirmed=True 로 다시 호출하세요.",
        ]
        if _preview_path_for_gate:
            _msg_parts.append(f"미리보기 파일: {_preview_path_for_gate}")
        else:
            _msg_parts.append(
                "미리보기 파일 경로를 찾을 수 없습니다 — prepare_listing 을 먼저 호출했는지 확인하세요."
            )
        return {
            "ok": False,
            "status_code": None,
            "origin_product_no": None,
            "channel_product_no": None,
            "missing_channel_no": True,
            "name_truncated": False,
            "raw": None,
            "seller_tags": None,
            "blocked_by": "preview_confirmation",
            "preview_path": _preview_path_for_gate,
            "require_preview_confirmation": True,
            "filled_from_prepared": [],
            "prepared_lookup": prepared_lookup,
            "notice_filled_from_config": [],
            "dry_run": _dry_run,
            "message": " ".join(_msg_parts),
            "error": None,
        }

    # ------------------------------------------------------------------ #
    # prepared payload 로부터 image_urls / detail_html 채우기.
    #
    # 명시적으로 준 값이 항상 우선한다. prepared 가 덮어쓰지 않는다. 채운 뒤에도
    # 아래 진입 게이트(원본 이미지 검사)와 prepared QA 게이트가 그대로 실행된다
    # — 이 경로가 검증을 우회하는 뒷문이 되면 안 된다.
    # ------------------------------------------------------------------ #
    _need_images = image_urls is None
    _need_detail = detail_html is None
    filled_from_prepared: list[str] = []

    if (_need_images or _need_detail) and _resolved_payload is not None:
        _fill_pkey = _product_key
        _fill_prepared = _resolved_payload

        # 어느 키에서 무엇을 가져왔는지 기록한다 (조용한 치환 방지).
        if not prepared_lookup:
            _retrieved_name = ""
            _retrieved_price = None
            _fp = (
                _fill_prepared.get("product")
                if isinstance(_fill_prepared.get("product"), dict)
                else {}
            )
            if isinstance(_fp, dict):
                _retrieved_name = str(_fp.get("name") or "")
                _retrieved_price = _fp.get("salePrice")
            prepared_lookup = {
                "key": _fill_pkey,
                "source": _product_key_source,
                "name": _retrieved_name,
                "salePrice": _retrieved_price,
            }
        if _need_detail:
            _prepared_html = _fill_prepared.get("detail_html")
            if isinstance(_prepared_html, str) and _prepared_html.strip():
                detail_html = _prepared_html
                filled_from_prepared.append("detail_html")
        if _need_images:
            _images_block = (
                _fill_prepared.get("images")
                if isinstance(_fill_prepared.get("images"), dict)
                else {}
            )
            # prepared 에서 가져온 이미지도 명시 입력과 *동일한 검증* 을
            # 통과해야 한다. 무효 항목을 조용히 걸러내면 2번 이미지가 대표
            # 이미지로 승격되는 조용한 치환이 일어난다. 원본 리스트를
            # 정규화 없이 정본 검증기에 그대로 넘겨 무효 항목이 하나라도
            # 섞이면 거부한다 (filter-not-fix). 새 검증 함수를 만들지 않고
            # 기존 정본을 재사용한다.
            _raw_prepared_urls = list(_images_block.get("listing_urls") or [])
            if _raw_prepared_urls:
                try:
                    naver_client._require_original_images(_raw_prepared_urls)
                except ValueError as _img_exc:
                    return _fail(
                        f"prepared payload 의 이미지에 무효 항목이 섞여 있어 "
                        f"등록을 거부한다 (filter-not-fix). "
                        f"product_key={_fill_pkey}, 사유={_img_exc}",
                        filled_from_prepared=filled_from_prepared,
                        prepared_lookup=prepared_lookup,
                        dry_run=_dry_run,
                    )
                _prepared_urls = [
                    str(u).strip() for u in _raw_prepared_urls if isinstance(u, str) and u.strip()
                ]
                if _prepared_urls:
                    image_urls = _prepared_urls
                    filled_from_prepared.append("image_urls")

    # 진입 게이트: 단순 길이검사가 아니라 내용검사로 교체.
    # 빈 문자열·공백·None·비문자열 항목이 섞이면 거부한다 (조용한 필터링 금지).
    # prepared 로 채운 값을 포함해 어떤 경로로든 여기를 통과해야 한다.
    try:
        naver_client._require_original_images(image_urls)
    except ValueError as exc:
        return _fail(
            str(exc),
            filled_from_prepared=filled_from_prepared,
            prepared_lookup=prepared_lookup,
            dry_run=_dry_run,
        )
    if not isinstance(detail_html, str) or not detail_html.strip():
        return _fail(
            "detail_html 은 비어있지 않은 HTML 문자열이어야 합니다.",
            filled_from_prepared=filled_from_prepared,
            prepared_lookup=prepared_lookup,
            dry_run=_dry_run,
        )

    # Fix 5 — 상품명 50자 정책 컷. naver_client 도 내부에서 자르지만,
    # 호출자에게 truncation 여부를 명시적으로 알리기 위해 여기서도 자르고 플래그 노출.
    original_name = name
    if len(name) > naver_client.MAX_PRODUCT_NAME_LEN:
        name = name[: naver_client.MAX_PRODUCT_NAME_LEN]
    name_truncated = name != original_name

    product = {
        "name": name,
        "categoryId": category_id,
        "salePrice": int(price),
        "tags": list(tags) if tags else [],
        "stock": int(stock),
        "delivery_fee": int(delivery_fee),
        "courier": courier,
    }
    if options:
        product["options"] = options
    if notice is not None:
        product["notice"] = notice

    try:
        payload = naver_client.build_payload(product, detail_html, image_urls, status=status)
    except Exception as exc:  # Fix 7 — sanitized
        return _fail(
            f"등록 중 오류(페이로드 빌드): {_sanitize_error(exc)}",
            filled_from_prepared=filled_from_prepared,
            prepared_lookup=prepared_lookup,
            dry_run=_dry_run,
        )

    # build_payload 가 설정에서 자동으로 채운 규제값 필드 목록을 페이로드에서
    # 추출한다. 이 값은 모든 이후 반환 경로에 실려 사용자에게 보고되어야 한다
    # (조용한 자동 채움 금지). 전송 페이로드에서는 naver_client 가 이미 제거했다.
    notice_filled = list(payload.get("notice_filled_from_config") or [])

    # ------------------------------------------------------------------ #
    # 불일치 트립와이어 (고시 타입 단일 진실).
    #
    # 게이트가 판정한 고시 타입과 빌드된 페이로드에 실린
    # productInfoProvidedNoticeType 이 다르면 등록을 차단한다 (네이버 호출 0회).
    # 이 클래스의 재발을 구조적으로 막는 장치다. 게이트가 FURNITURE 로 검사해
    # 판매자에게 가구 필드를 물었는데, 페이로드는 ETC 로 신고되는 조용한 잘못
    # 신고를 허용하지 않는다.
    #
    # 명시 타입이 product 에 주어진 경우 양쪽 모두 그것을 쓰므로 자동으로 일치한다.
    # 명시 타입이 없으면 양쪽 모두 categoryId 에서 경로를 조회해 같은 휴리스틱을
    # 돌리므로 일치해야 한다. 일치하지 않으면 구조 결함이고, 조용히 통과시키지
    # 않고 트립와이어로 드러낸다.
    # ------------------------------------------------------------------ #
    payload_type = _payload_notice_type(payload)
    gate_type = _gate_notice_type(category_id, product)
    if payload_type and gate_type and payload_type != gate_type:
        return {
            "ok": False,
            "status_code": None,
            "origin_product_no": None,
            "channel_product_no": None,
            "missing_channel_no": True,
            "name_truncated": name_truncated,
            "raw": None,
            "seller_tags": None,
            "blocked_by": "notice_type_tripwire",
            "gate_notice_type": gate_type,
            "payload_notice_type": payload_type,
            "filled_from_prepared": filled_from_prepared,
            "prepared_lookup": prepared_lookup,
            "notice_filled_from_config": notice_filled,
            "requested_status": status,
            "applied_status": None,
            "dry_run": _dry_run,
            "message": (
                f"고시 타입 불일치 — 게이트는 {gate_type!r} 로 검사했지만 "
                f"페이로드는 {payload_type!r} 로 신고하려 한다. "
                "판매자가 한쪽 질문에 답하고 통과했는데 다른 타입으로 신고되는 "
                "조용한 잘못 신고를 차단한다."
            ),
            "error": None,
        }

    # 결정론 컴플라이언스 게이트 (fail-closed).
    # 네이버 API 호출 직전에 고시 필수 필드/KC/원산지 검사를 실행한다.
    # FAIL 심각도 위반이 있으면 네이버를 호출하지 않고 거부한다.
    # 예외를 삼켜 등록을 진행시키지 않는다 (무동작·identity 금지).
    #
    # DRY_RUN(COMMERCE_DRY_RUN=1) 도 게이트를 통과한다. DRY_RUN 은 실제 네이버
    # 호출만 생략하는 리허설이다 — "실제로 등록했다면 어떤 판정이 났을까" 를
    # 보고해야 하므로, 게이트는 항상 실행된다. 스킵하면 비컴플라이언스 상품이
    # DRY_RUN 에서는 성공으로 보고되고, 실제 경로에서는 거부되는 모순이 생긴다.
    try:
        gate = _run_compliance_gate(name, category_id, payload)
    except Exception as exc:
        # 검사 자체가 예외로 실패하면 fail-closed: 등록을 차단한다.
        return _fail(
            f"컴플라이언스 검사 중 오류(등록 차단): {_sanitize_error(exc)}",
            name_truncated=name_truncated,
            filled_from_prepared=filled_from_prepared,
            prepared_lookup=prepared_lookup,
            notice_filled_from_config=notice_filled,
            dry_run=_dry_run,
        )

    if gate["blocked"]:
        violations = gate["violations"]
        needs_user = gate["needs_user"]
        message_lines = [
            "컴플라이언스 위반으로 등록을 거부했습니다 (fail-closed).",
        ]
        for v in violations:
            message_lines.append(f"- [{v['rule']}] {v['detail']}")
        if needs_user:
            field_list = ", ".join(n["label"] for n in needs_user)
            message_lines.append(f"사용자 입력이 필요한 필수 항목: {field_list}")
        return {
            "ok": False,
            "status_code": None,
            "origin_product_no": None,
            "channel_product_no": None,
            "missing_channel_no": True,
            "name_truncated": name_truncated,
            "raw": None,
            "seller_tags": None,
            "blocked_by": "compliance",
            "violations": violations,
            "needs_user": needs_user,
            "filled_from_prepared": filled_from_prepared,
            "prepared_lookup": prepared_lookup,
            "notice_filled_from_config": notice_filled,
            "dry_run": _dry_run,
            "message": "\n".join(message_lines),
            "error": None,
        }

    # 우회 경로 차단.
    #
    # register_product 도구가 prepared payload 를 전혀 조회하지 않아, prepare 에서
    # 막힌 상품도 원시 인자로 다시 부르면 등록되는 우회 경로가 존재했다. 내부에서
    # product_key 를 유도해 prepared payload 가 존재하면 그 QA 집계를 완전 게이트로
    # 적용한다(PENDING/FAIL 차단 — 네이버 호출 0회). prepared 가 없으면 기존대로
    # 결정론 검사만 적용하고 응답에 gate:"deterministic_only" 를 표기한다.
    # 시그니처는 변경하지 않는다.
    #
    # DRY_RUN 도 이 게이트를 통과한다. DRY_RUN 은 리허설이므로 "실제 등록했다면
    # 어떤 게이트 판정이 났을까" 를 보고해야 한다. DRY_RUN 에서만 게이트를
    # 스킵하면 prepared QA 가 FAIL 인 상품이 DRY_RUN 에서는 성공으로 보고되는
    # 모순이 생긴다.
    #
    # product_key 는 위에서 *한 번* 유도한 것을 그대로 쓴다. 여기서 다시 유도하면
    # 이름이 50자로 잘린 뒤라 다른 키가 나오고, 자동 채움이 찾은 prepared 와 게이트가
    # 판정한 prepared 가 달라진다(FAIL 판정 우회). 같은 키 하나를 공유한다.
    gate_label = "deterministic_only"
    _pkey = _product_key
    if _pkey:
        try:
            _prepared = _register_mod.load_prepared_payload(product_key=_pkey)
        except FileNotFoundError:
            _prepared = None
        except ValueError:
            # version 불일치 등 — 조용히 무시하지 않고 결정론 게이트로만 진행.
            _prepared = None
        if isinstance(_prepared, dict):
            # prepared 가 존재하면 QA 집계를 완전 게이트로 적용.
            _qa = _prepared.get("qa") if isinstance(_prepared.get("qa"), dict) else {}
            _allowed, _reason = qa_agents.qa_gate(_prepared)
            if not _allowed:
                # PENDING/FAIL 차단 — 네이버 호출 없이 거부.
                return {
                    "ok": False,
                    "status_code": None,
                    "origin_product_no": None,
                    "channel_product_no": None,
                    "missing_channel_no": True,
                    "name_truncated": name_truncated,
                    "raw": None,
                    "seller_tags": None,
                    "blocked_by": "prepared_qa_gate",
                    "gate": "full",
                    "reason": _reason,
                    "needs_llm": _prepared.get("needs_llm") or [],
                    "needs_user": _prepared.get("needs_user") or [],
                    "filled_from_prepared": filled_from_prepared,
                    "prepared_lookup": prepared_lookup,
                    "notice_filled_from_config": notice_filled,
                    "dry_run": _dry_run,
                    "message": (
                        "prepared payload 의 QA 게이트가 등록을 차단했다 "
                        f"(reason={_reason}). submit_reviews 로 PENDING 을 "
                        "해소하거나 사용자 입력을 보완해야 한다."
                    ),
                    "error": None,
                }
            gate_label = "full"

    # 결정론 게이트 통과 — 네이버 API 호출 진행.
    try:
        outcome = naver_client.register_product(payload)
    except Exception as exc:  # Fix 7 — sanitized
        return _fail(
            f"등록 중 오류: {_sanitize_error(exc)}",
            name_truncated=name_truncated,
            filled_from_prepared=filled_from_prepared,
            prepared_lookup=prepared_lookup,
            notice_filled_from_config=notice_filled,
            dry_run=_dry_run,
        )

    # register_product 는 (status_code, body) 튜플을 반환하지만, DRY_RUN 시 dict.
    if isinstance(outcome, dict):
        return {
            "ok": bool(outcome.get("ok")),
            "status_code": None,
            "origin_product_no": outcome.get("originProductNo"),
            "channel_product_no": None,
            "missing_channel_no": True,
            "name_truncated": name_truncated,  # Fix 5
            "raw": outcome,
            "seller_tags": None,
            "gate": gate_label,
            "pending_reviews": gate["pending_reviews"],
            "filled_from_prepared": filled_from_prepared,
            "prepared_lookup": prepared_lookup,
            "notice_filled_from_config": notice_filled,
            "requested_status": status,
            "applied_status": outcome.get("statusType"),
            "dry_run": _dry_run,
            "error": None,
        }

    status_code, body = outcome
    ok = isinstance(status_code, int) and 200 <= status_code < 300
    origin_product_no = None
    if isinstance(body, dict):
        origin_product_no = body.get("originProductNo") or body.get("originProduct", {}).get(
            "originProductNo"
        )
    # 채널상품번호 추출 — register 모듈의 정본 추출기 재사용(새 로직 만들지 않는다).
    # 이 번호가 없으면 이후 수정·상태보정이 불가능하다(빈 값 가드로 드러낸다).
    channel_product_no = _register_mod._extract_channel_product_no(body) if ok else None
    missing_channel_no = channel_product_no is None
    seller_tags_meta = (
        naver_client.seller_tag_autostrip_meta(body) if isinstance(body, dict) else None
    )

    # ------------------------------------------------------------------ #
    # 판매상태 보정 (Defect 3).
    #
    # 네이버 커머스 API 는 생성 시점의 originProduct.statusType 을 무시하고
    # 기본값(SALE)로 저장하는 경우가 있다(실측 확인). 판매중지(SUSPENSION)로
    # 올리려던 판매자가 판매중인 상품을 갖게 되는데 아무도 모르는 조용한 잘못된
    # 상태다. 본 보정 경로는:
    #   1. 생성 응답의 statusType 이 요청값과 다르면 보정을 시도한다.
    #   2. 보정은 채널상품번호(channel-product) PUT 으로 한다.
    #   3. 그래도 다르면 ok=False 로 보고한다 (조용한 성공 금지).
    # status="SALE" 이고 응답도 SALE 이면 추가 호출이 일어나지 않는다.
    #
    # **보정 PUT 본문 (실측 확인된 정답)**: 채널상품 PUT 은 리소스를 교체한다.
    # 단편({"statusType": ...}) 은 네이버 API 가 무시한다 — 200 을 반환하지만
    # 상태는 바뀌지 않는다(실등록에서 확인). 따라서 보정은 다음 순서를 따른다:
    #   a. get_product(origin_no) 로 현재 전체 리소스를 읽는다.
    #   b. 응답의 originProduct.statusType 과
    #      smartstoreChannelProduct.channelProductDisplayStatusType 을
    #      요청값으로 덮어쓴다.
    #   c. update_product(channel_no, {전체 originProduct, 전체
    #      smartstoreChannelProduct}) 로 *전체* 본문을 보낸다.
    #   d. get_product 로 재확인한다.
    #
    # 보정 실패의 사유(예외 텍스트 또는 HTTP 상태)는 status_correction_error
    # 에 담는다. None 은 보정 시도가 없었거나 성공한 경우다. ok=False 만
    # 남기고 사유를 삼키면 판매자가 어찌할 바를 알 수 없다.
    # ------------------------------------------------------------------ #
    applied_status = _extract_status_type(body) if ok else ""
    status_corrected = False
    status_correction_attempted = False
    status_correction_error: str | None = None
    if ok and applied_status and applied_status != status.upper() and origin_product_no:
        status_correction_attempted = True
        # 보정은 채널상품번호로 한다. 위에서 추출한 값을 그대로 쓴다(응답의 정본).
        if channel_product_no:
            try:
                # a. 현재 리소스를 전체 읽어온다 — 단편 PUT 은 무시된다(실측).
                _rsc, _rbody = naver_client.get_product(origin_product_no)
                if not isinstance(_rsc, int) or _rsc != 200 or not isinstance(_rbody, dict):
                    raise RuntimeError(f"보정 전 리소스 조회 실패 (get_product status={_rsc})")
                _origin = _rbody.get("originProduct")
                _channel = _rbody.get("smartstoreChannelProduct")
                if not isinstance(_origin, dict):
                    raise RuntimeError("보정 전 리소스에 originProduct 가 없다")
                # b. 두 status 필드를 요청값으로 덮어쓴다.
                _origin["statusType"] = status
                if isinstance(_channel, dict):
                    _channel["channelProductDisplayStatusType"] = status
                # c. 전체 본문으로 PUT.
                correction_payload: dict[str, Any] = {"originProduct": _origin}
                if isinstance(_channel, dict):
                    correction_payload["smartstoreChannelProduct"] = _channel
                _usc, _ubody = naver_client.update_product(channel_product_no, correction_payload)
                if not isinstance(_usc, int) or not (200 <= _usc < 300):
                    raise RuntimeError(f"보정 PUT 이 거부되었다 (update_product status={_usc})")
                # d. 보정 후 재확인.
                _vsc, _vbody = naver_client.get_product(origin_product_no)
                if isinstance(_vbody, dict):
                    verified = _extract_status_type(_vbody)
                    if verified:
                        applied_status = verified
                status_corrected = applied_status == status.upper()
            except Exception as exc:
                # 보정 실패 — applied_status 는 보정 전 값으로 둔다.
                # 사유를 captured 해서 반환에 실는다 (ok=False 만 남기지 않는다).
                # HTTP 상태든 예외 텍스트든 _sanitize_error 로 위생화.
                status_correction_error = _sanitize_error(exc)
        # ok 재판정: 보정 후에도 다르면 실패로 보고 (조용한 성공 금지).
        if applied_status != status.upper():
            ok = False

    # 등록 기록(record) 저장 — 채널상품번호를 디스크에 남겨 이후 수정이 가능하게.
    # 성공(최종 ok) 일 때만 기록한다. 빈 값 가드: 채널번호가 없으면 기록은 쓰지
    # 않되 missing_channel_no 로 그 사실을 반환에 드러낸다.
    registration_record = None
    if ok and origin_product_no and _product_key:
        try:
            registration_record = _register_mod.write_registration_record(
                _product_key,
                origin_product_no=origin_product_no,
                channel_product_no=channel_product_no,
                name=name,
                sale_price=int(price),
                category_id=category_id,
                requested_status=status,
                applied_status=applied_status or status,
            )
        except Exception:
            # 기록 저장 실패가 등록 자체를 실패시키지는 않는다. 단, 기록 파일이
            # 없으면 read_registration_record 가 None 을 반환하므로 이후 수정
            # 기능이 안전하게 차단된다(조용한 성공이 아니다 — 채널번호가 없으면
            # missing_channel_no 로 이미 드러났다).
            registration_record = None

    # 에러 응답의 raw 본문은 화이트리스트 키만 남겨 노출.
    exposed_raw = _sanitize_body(body) if not ok else body

    return {
        "ok": ok,
        "status_code": status_code,
        "origin_product_no": origin_product_no,
        "channel_product_no": channel_product_no,
        "missing_channel_no": missing_channel_no,
        "name_truncated": name_truncated,  # Fix 5
        "raw": exposed_raw,
        "seller_tags": seller_tags_meta,
        "gate": gate_label,
        "pending_reviews": gate["pending_reviews"],
        "filled_from_prepared": filled_from_prepared,
        "prepared_lookup": prepared_lookup,
        "notice_filled_from_config": notice_filled,
        "requested_status": status,
        "applied_status": applied_status,
        "status_corrected": status_corrected,
        "status_correction_attempted": status_correction_attempted,
        # None 이면 보정 시도가 없었거나 성공한 것. 실패 시 예외/HTTP 상태 텍스트.
        # ok=False 만 남기면 판매자가 어찌할 바를 알 수 없다 — 사유를 반드시 실는다.
        "status_correction_error": status_correction_error,
        "registration_record": registration_record,
        "dry_run": _dry_run,
        "error": None if ok else _sanitize_text(f"API 반환 상태 {status_code}"),
    }


@mcp.tool()
def get_product(origin_product_no: str) -> dict[str, Any]:
    """등록된 상품을 origin product 기준으로 조회한다.

    Args:
        origin_product_no: 네이버 커머스 API 의 origin product 번호.
            (``register_product`` 반환의 ``origin_product_no`` 와 동일.)

    Returns:
        ``{"ok": bool, "status_code": int, "product": Any, "error": str | None}``
        ``ok`` 는 HTTP 200 일 때만 ``True``.
    """
    if not isinstance(origin_product_no, str) or not origin_product_no.strip():
        return {
            "ok": False,
            "status_code": None,
            "product": None,
            "error": "origin_product_no 는 비어있지 않은 문자열이어야 합니다.",
        }

    try:
        status_code, body = naver_client.get_product(origin_product_no)
    except Exception as exc:  # Fix 7 — sanitized
        return {
            "ok": False,
            "status_code": None,
            "product": None,
            "error": f"조회 중 오류: {_sanitize_error(exc)}",
        }

    ok = isinstance(status_code, int) and status_code == 200
    exposed_body = body if ok else _sanitize_body(body)
    return {
        "ok": ok,
        "status_code": status_code,
        "product": exposed_body if ok else None,
        "error": None if ok else _sanitize_text(f"API 반환 상태 {status_code}: {body}"),
    }


@mcp.tool()
def prepare_listing(product: dict[str, Any]) -> dict[str, Any]:
    """상품 정보 + 이미지 소스로 prepared payload 를 만든다.

    등록 전 단계를 수행한다: 이미지 정규화(images.attach_images), 상세 HTML
    렌더(detail_render), JPEG 비의존 QA 집계. 결과를 prepared payload 로
    저장한다. 이미지 QA 는 래스터 렌더를 요구하므로 PENDING 등록하고, 카피
    QA 도 LLM 판단이 필요하면 PENDING 이다(FAIL 로 만들지 않는다).

    Args:
        product: 상품 입력 dict. 필수 키:
            - ``name`` (또는 ``title_ko``): 상품명(한국어).
            - ``salePrice`` (또는 ``sell_price``/``price``): KRW 판매가.
            - ``image_sources``: 이미지 소스 리스트(로컬 경로/CDN URL/외부 URL).
            선택 키: ``options``, ``tags``, ``notice``, ``category_id`` 등.
            URL 키(``url``/``source_url``/``item_url``/``detail_url``)는 거부.

    Returns:
        ``{"ok": bool, "product_key": str, "needs_llm": [...],
        "needs_user": [...], "qa": {...}, "error": str | None}``

    안내:
        - ``needs_llm`` 의 각 항목은 ``submit_reviews`` 로 회신해야 한다.
        - 회신하지 않으면 PENDING 이 유지되어 등록이 차단된다.
        - 서버 자체는 LLM 을 호출하지 않는다(common._llm_hint 위임).
    """
    if not isinstance(product, dict):
        return {
            "ok": False,
            "product_key": None,
            "needs_llm": [],
            "needs_user": [],
            "qa": {},
            "error": "product 는 dict 여야 합니다.",
        }
    try:
        payload = _register_mod.prepare_listing(product)
    except ValueError as exc:
        return {
            "ok": False,
            "product_key": None,
            "needs_llm": [],
            "needs_user": [],
            "qa": {},
            "error": _sanitize_text(str(exc)),
        }
    except Exception as exc:
        return {
            "ok": False,
            "product_key": None,
            "needs_llm": [],
            "needs_user": [],
            "qa": {},
            "error": f"prepare_listing 중 오류: {_sanitize_error(exc)}",
        }
    # 정규화된 네이버 CDN URL 리스트를 images 키로 노출한다.
    # 상세 HTML 전문은 반환에 싣지 않는다(컨텍스트 비용·변형 위험).
    # 클라이언트는 images 를 보고 무엇이 올라갔는지 확인할 수 있다.
    _images_block = payload.get("images") if isinstance(payload.get("images"), dict) else {}
    _listing_urls = [
        str(u).strip()
        for u in (_images_block.get("listing_urls") or [])
        if isinstance(u, str) and u.strip()
    ]
    return {
        "ok": True,
        "product_key": payload.get("product_key"),
        "needs_llm": payload.get("needs_llm") or [],
        "needs_user": payload.get("needs_user") or [],
        "qa": payload.get("qa") or {},
        "images": _listing_urls,
        "preview_path": payload.get("preview_path"),
        "error": None,
    }


@mcp.tool()
def submit_reviews(product_key: str, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """클라이언트 LLM 의 검수 회신을 prepared payload 의 QA 기록에 병합.

    신뢰 모델(타협 불가):
      - **덮어쓰기가 아니라 병합**: 서버 verdict 와 클라이언트 회신의 *더 나쁜
        쪽* 을 채택한다(FAIL > PENDING > WARN > PASS).
      - 서버가 기록한 violations 은 **절대 삭제하지 않는다**.
      - 결과적으로 클라이언트는 ``PENDING -> PASS`` 로만 상향할 수 있고,
        ``FAIL -> PASS`` 는 불가능하다.
      - 제출 가능 agent 는 ``{"image","copy"}`` 로 고정. ``compliance`` 제출은
        ``ValueError`` (결정론 검사를 클라이언트가 뒤집을 수 없다).

    Args:
        product_key: prepared payload 의 product_key.
        reviews: ``[{"agent": "image"|"copy", "verdict": "PASS"|"WARN"|
            "FAIL"|"PENDING", "violations": [...], "summary": str}, ...]``.

    Returns:
        ``{"ok": bool, "qa": {...}, "gate_allowed": bool, "error": str | None}``
        - ``gate_allowed``: 갱신 후 QA 게이트가 등록을 허용하는지(PENDING/FAIL
          이 없으면 True).
    """
    if not isinstance(product_key, str) or not product_key.strip():
        return {
            "ok": False,
            "qa": {},
            "gate_allowed": False,
            "error": "product_key 는 비어있지 않은 문자열이어야 합니다.",
        }
    if not isinstance(reviews, list) or not reviews:
        return {
            "ok": False,
            "qa": {},
            "gate_allowed": False,
            "error": "reviews 는 최소 1개 이상의 검수 항목 리스트여야 합니다.",
        }
    try:
        aggregated = _register_mod.submit_reviews(product_key, reviews)
    except ValueError as exc:
        return {
            "ok": False,
            "qa": {},
            "gate_allowed": False,
            "error": _sanitize_text(str(exc)),
        }
    except Exception as exc:
        return {
            "ok": False,
            "qa": {},
            "gate_allowed": False,
            "error": f"submit_reviews 중 오류: {_sanitize_error(exc)}",
        }
    # 갱신 후 게이트 통과 여부를 계산해 회신.
    try:
        _prepared = _register_mod.load_prepared_payload(product_key=product_key)
        allowed, _reason = qa_agents.qa_gate(_prepared)
    except Exception:
        allowed = False
    return {
        "ok": True,
        "qa": aggregated,
        "gate_allowed": bool(allowed),
        "error": None,
    }


def _fail(
    message: str,
    *,
    name_truncated: bool = False,
    filled_from_prepared: list[str] | None = None,
    prepared_lookup: dict[str, Any] | None = None,
    notice_filled_from_config: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    # prepared_lookup 는 register_product 의 모든 반환 경로에서 무엇을 어느
    # 키로 찾았는지 드러낸다(조용한 치환 방지). 검증 실패 등 반환 시점에
    # 이미 결정된 lookup 이 있으면 그대로 실어 보낸다.
    # notice_filled_from_config 는 설정에서 자동으로 채워진 규제값 필드 목록이다.
    # 모든 반환 경로에서 이 키가 나와야 한다 (조용한 자동 채움 금지). 비었으면 빈 리스트.
    # dry_run 은 COMMERCE_DRY_RUN=1 모드에서의 실패임을 표시한다. 실제 등록과
    # 리허설 실패를 구분하기 위해 모든 반환 경로에 존재한다.
    return {
        "ok": False,
        "status_code": None,
        "origin_product_no": None,
        "channel_product_no": None,
        "missing_channel_no": True,
        "name_truncated": name_truncated,  # Fix 5 — validation-fail 시 기본값
        "raw": None,
        "seller_tags": None,
        "filled_from_prepared": filled_from_prepared if filled_from_prepared is not None else [],
        "prepared_lookup": prepared_lookup if prepared_lookup is not None else {},
        "notice_filled_from_config": (
            notice_filled_from_config if notice_filled_from_config is not None else []
        ),
        "dry_run": dry_run,
        "error": message,
    }


def main() -> None:
    """stdio MCP 서버 진입점. ``[project.scripts]`` 의 ``clossify`` 가 이 함수를 가리킨다."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
