# Clossify — Naver SmartStore listing automation.
# Copyright (c) 2026 3rdhand. Licensed under the Sustainable Use License v1.0.
# You may use and modify this software for your own internal business or personal
# purposes. Providing it to others — including as a hosted or paid service — is
# permitted only free of charge and for non-commercial purposes. See LICENSE.md.
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
_NOTICE_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "material": ("소재", "이 카테고리 고시 필수 항목이며 추정할 수 없습니다"),
    "size": ("치수/사이즈", "이 카테고리 고시 필수 항목이며 추정할 수 없습니다"),
    "color": ("색상", "이 카테고리 고시 필수 항목이며 추정할 수 없습니다"),
    "components": ("구성품", "이 카테고리 고시 필수 항목이며 추정할 수 없습니다"),
    "caution": ("주의사항", "이 카테고리 고시 필수 항목입니다"),
    "manufacturer": ("제조자", "이 카테고리 고시 필수 항목입니다"),
    "importer": ("수입자", "이 카테고리 고시 필수 항목입니다"),
    "producer": ("생산자", "이 카테고리 고시 필수 항목입니다"),
    "itemName": ("품명", "이 카테고리 고시 필수 항목입니다"),
    "modelName": ("모델명", "이 카테고리 고시 필수 항목입니다"),
    "certificationType": ("인증 정보", "이 카테고리는 인증 정보가 필요합니다"),
    "ratedVoltage": ("정격전압", "이 카테고리 고시 필수 항목입니다"),
    "powerConsumption": ("소비전력", "이 카테고리 고시 필수 항목입니다"),
    "energyEfficiencyRating": ("에너지효율 등급", "이 카테고리 고시 필수 항목입니다"),
    "releaseDate": ("출시일", "이 카테고리 고시 필수 항목입니다"),
    "releaseDateText": ("출시일 정보", "이 카테고리 고시 필수 항목입니다"),
    "weight": ("무게/중량", "이 카테고리 고시 필수 항목입니다"),
    "specification": ("규격/스펙", "이 카테고리 고시 필수 항목입니다"),
    "purity": ("순도", "이 카테고리 고시 필수 항목입니다"),
    "bandMaterial": ("밴드 소재", "이 카테고리 고시 필수 항목입니다"),
    "telecomType": ("통신사 정보", "이 카테고리 고시 필수 항목입니다"),
    "recommendedAge": ("권장 연령", "이 카테고리 고시 필수 항목입니다"),
    "title": ("제목", "이 카테고리 고시 필수 항목입니다"),
    "author": ("저자", "이 카테고리 고시 필수 항목입니다"),
    "publisher": ("출판사", "이 카테고리 고시 필수 항목입니다"),
    "pages": ("쪽수", "이 카테고리 고시 필수 항목입니다"),
    "publishDate": ("발행일", "이 카테고리 고시 필수 항목입니다"),
    "capacity": ("용량/내용량", "이 카테고리 고시 필수 항목입니다"),
    "expirationDate": ("유통기한", "이 카테고리 고시 필수 항목입니다"),
    "usage": ("사용방법", "이 카테고리 고시 필수 항목입니다"),
    "ingredients": ("원재료/성분", "이 카테고리 고시 필수 항목입니다"),
    "nutritionFacts": ("영양성분", "이 카테고리 고시 필수 항목입니다"),
    "foodType": ("식품 유형", "이 카테고리 고시 필수 항목입니다"),
    "location": ("원산지/생산지", "이 카테고리 고시 필수 항목입니다"),
}


def _notice_field_label(field: str) -> tuple[str, str]:
    """고시 필드명에 대한 (라벨, 사유) 반환. 매핑 없으면 필드명 그대로."""
    return _NOTICE_FIELD_LABELS.get(field, (field, "이 카테고리 고시 필수 항목입니다"))


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

    # needs_user: 결정론 위반 중 "고시 필수필드" 위반에서 누락 필드명을 추출해
    # 사용자 입력 요청으로 변환. 요구되는 구조:
    #   {"field": ..., "label": ..., "why": ...}
    needs_user: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for row in check_result.get("violations") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("rule") or "") != "고시 필수필드":
            continue
        if str(row.get("severity") or "").upper() != qa_agents.FAIL:
            continue
        detail_text = str(row.get("detail") or "")
        # detail 형태: "고시 타입 WEAR 필수 필드 누락: material, size, color"
        if "누락:" in detail_text:
            after = detail_text.split("누락:", 1)[1]
            for field in after.split(","):
                field = field.strip()
                if field and field not in seen_fields:
                    seen_fields.add(field)
                    label, why = _notice_field_label(field)
                    needs_user.append(
                        {
                            "field": field,
                            "label": label,
                            "why": why,
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
    image_urls: list[str],
    category_id: str,
    detail_html: str,
    *,
    options: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    status: str = "SALE",
    stock: int = 1,
    delivery_fee: int = 3000,
    courier: str = "CJGLS",
    notice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """상품 정보를 받아 등록 페이로드를 빌드하고 네이버 커머스 API 로 등록한다.

    본 도구는 naver_client.build_payload() + register_product() 를 순차 호출한다.
    페이로드 빌딩·고시 정보 자동 완성·판매자태그 제한어 자동 제거 등의 복잡도는
    naver_client 가 처리한다.

    Args:
        name: 상품명 (네이버 정책상 길이 제한이 있음, naver_client 가 50자 컷).
        price: 판매가 (KRW, 양의 정수).
        image_urls: ``upload_images`` 가 반환한 CDN URL 리스트.
            첫 번째 URL 이 대표 이미지가 된다.
        category_id: 네이버 상품 카테고리 트리의 리프 카테고리 ID.
        detail_html: 상세페이지 HTML (``<html>``... 또는 조각 HTML).
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

    Returns:
        ``{"ok": bool, "status_code": int | None, "origin_product_no": str | None,
        "raw": Any, "seller_tags": {...} | None, "error": str | None}``
        - ``ok``: HTTP 상태가 2xx(성공)인지.
        - ``raw``: API 응답 본문 (에러 메시지 포함 가능).
        - ``seller_tags``: 제한어 자동 제거 메타가 있을 때만 존재.

    Note:
        환경변수 ``COMMERCE_DRY_RUN=1`` 시 실제 등록 없이 페이로드를
        ``.local/dry_run_payload.json`` 에 덤프한다 (naver_client 동작).
    """
    if not isinstance(name, str) or not name.strip():
        return _fail("name 은 비어있지 않은 문자열이어야 합니다.")
    if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        return _fail("price 는 0보다 큰 정수(KRW)여야 합니다.")
    # 진입 게이트: 단순 길이검사가 아니라 내용검사로 교체.
    # 빈 문자열·공백·None·비문자열 항목이 섞이면 거부한다 (조용한 필터링 금지).
    try:
        naver_client._require_original_images(image_urls)
    except ValueError as exc:
        return _fail(str(exc))
    if not isinstance(category_id, str) or not category_id.strip():
        return _fail("category_id 는 비어있지 않은 문자열이어야 합니다.")
    if not isinstance(detail_html, str) or not detail_html.strip():
        return _fail("detail_html 은 비어있지 않은 HTML 문자열이어야 합니다.")
    if status not in {"SALE", "SUSPENSION"}:
        return _fail("status 는 'SALE' 또는 'SUSPENSION' 이어야 합니다.")

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
        return _fail(f"등록 중 오류(페이로드 빌드): {_sanitize_error(exc)}")

    # 결정론 컴플라이언스 게이트 (fail-closed).
    # 네이버 API 호출 직전에 고시 필수 필드/KC/원산지 검사를 실행한다.
    # FAIL 심각도 위반이 있으면 네이버를 호출하지 않고 거부한다.
    # 예외를 삼켜 등록을 진행시키지 않는다 (무동작·identity 금지).
    #
    # 단, COMMERCE_DRY_RUN=1 인 경우에는 게이트를 건너뛴다. DRY_RUN 은 실제
    # 등록이 일어나지 않고 페이로드를 파일로 덤프만 하는 개발/테스트 모드다.
    # 게이트의 목적은 비컴플라이언스 상품의 **등록 차단**이며, DRY_RUN 에서는
    # 등록 자체가 발생하지 않으므로 게이트가 무의미하다. 대신 DRY_RUN 이
    # 아닌 모든 실제 등록 경로에서 게이트가 동작한다.
    _dry_run = os.environ.get("COMMERCE_DRY_RUN") == "1"
    if _dry_run:
        gate: dict[str, Any] = {
            "blocked": False,
            "violations": [],
            "needs_user": [],
            "pending_reviews": [
                "copy_qa: 카피 품질 LLM 판단 대기 중",
                "image_qa: 이미지 적합성 LLM 판단 대기 중",
            ],
        }
    else:
        try:
            gate = _run_compliance_gate(name, category_id, payload)
        except Exception as exc:
            # 검사 자체가 예외로 실패하면 fail-closed: 등록을 차단한다.
            return _fail(f"컴플라이언스 검사 중 오류(등록 차단): {_sanitize_error(exc)}")

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
            "name_truncated": name_truncated,
            "raw": None,
            "seller_tags": None,
            "blocked_by": "compliance",
            "violations": violations,
            "needs_user": needs_user,
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
    gate_label = "deterministic_only"
    if not _dry_run:
        try:
            _pkey = _register_mod.make_product_key(name, int(price))
        except Exception:
            _pkey = None
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
                        "name_truncated": name_truncated,
                        "raw": None,
                        "seller_tags": None,
                        "blocked_by": "prepared_qa_gate",
                        "gate": "full",
                        "reason": _reason,
                        "needs_llm": _prepared.get("needs_llm") or [],
                        "needs_user": _prepared.get("needs_user") or [],
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
        return _fail(f"등록 중 오류: {_sanitize_error(exc)}")

    # register_product 는 (status_code, body) 튜플을 반환하지만, DRY_RUN 시 dict.
    if isinstance(outcome, dict):
        return {
            "ok": bool(outcome.get("ok")),
            "status_code": None,
            "origin_product_no": outcome.get("originProductNo"),
            "name_truncated": name_truncated,  # Fix 5
            "raw": outcome,
            "seller_tags": None,
            "gate": gate_label,
            "pending_reviews": gate["pending_reviews"],
            "error": None,
        }

    status_code, body = outcome
    ok = isinstance(status_code, int) and 200 <= status_code < 300
    origin_product_no = None
    if isinstance(body, dict):
        origin_product_no = body.get("originProductNo") or body.get("originProduct", {}).get(
            "originProductNo"
        )
    seller_tags_meta = (
        naver_client.seller_tag_autostrip_meta(body) if isinstance(body, dict) else None
    )

    # 에러 응답의 raw 본문은 화이트리스트 키만 남겨 노출.
    exposed_raw = _sanitize_body(body) if not ok else body

    return {
        "ok": ok,
        "status_code": status_code,
        "origin_product_no": origin_product_no,
        "name_truncated": name_truncated,  # Fix 5
        "raw": exposed_raw,
        "seller_tags": seller_tags_meta,
        "gate": gate_label,
        "pending_reviews": gate["pending_reviews"],
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
    return {
        "ok": True,
        "product_key": payload.get("product_key"),
        "needs_llm": payload.get("needs_llm") or [],
        "needs_user": payload.get("needs_user") or [],
        "qa": payload.get("qa") or {},
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


def _fail(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status_code": None,
        "origin_product_no": None,
        "name_truncated": False,  # Fix 5 — validation-fail 시 기본값
        "raw": None,
        "seller_tags": None,
        "error": message,
    }


def main() -> None:
    """stdio MCP 서버 진입점. ``[project.scripts]`` 의 ``clossify`` 가 이 함수를 가리킨다."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
