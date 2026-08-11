# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""상품군별 등록 템플릿 저장소.

본 모듈은 **상품군별 복수 템플릿**을 저장·조회·명시적 적용한다.
``templates.py``(브랜드 상세 HTML 렌더러) 와 이름이 비슷하지만 관계없다 —
본 모듈은 ``listing_templates`` 이다.

안전 설계 (이슈: 템플릿 저장 — 상품군별 복수, 자동 적용 금지)
----------------------------------------------------------
템플릿에 담기는 값 중 **고시·원산지·AS 는 규제 신고값**이다. 템플릿은 창작은
아니지만 **다른 상품의 값을 가져오는 것**이라, 검증 없이 흐르면 규제 원칙의
우회로가 된다. 같은 상품군이라도 공급처가 다르면 원산지가 다르다.

따라서 본 모듈은 다음 세 가지를 지킨다:

1. **자동 적용 금지.** 사용자가 *명시적으로 지목한* 템플릿만 적용한다.
   "가장 최근 것"·"하나뿐이니까" 같은 암묵 적용 경로를 만들지 않는다.
2. **출처를 남긴다.** 적용된 값은 *어느 템플릿에서 왔는지* 결과에 드러난다.
3. **비밀값을 담지 않는다.** 키·토큰·API 자격증명은 저장·출력·로그에 남기지
   않는다. ``_TEMPLATE_FIELD_KEYS`` 화이트리스트에 없는 키는 저장 단계에서
   거부된다(조용한 채움 금지).

저장소
------
- 파일: ``<STATE_DIR>/templates.json`` (``config.json`` 과 **별도 파일**).
- 수명이 다르고 복수다. 설정 정본(``config.json``) 에 섞지 않는다.
- 파일이 없으면 **빈 상태로 취급**(오류 아님).
- 파일이 손상됐으면 **조용히 덮어쓰지 않고** 사유를 알린다(``TemplateStoreError``).
- 쓰기 전 **백업** — ``config_form_server._backup_config`` 관례를 따른다:
  ``shutil.copy2`` → ``<path>.bak.<UTC타임스탬프>``.
- 원자적 쓰기 — ``common._write_json_file``(tmp + ``os.replace``).

담는 값 / 안 담는 값
--------------------
담는다  : ``productInfoProvidedNotice``(고시 본문) · ``afterServiceInfo``(AS)
          · ``delivery_policy``(반품/교환 배송비) · ``origin``(원산지)
          · ``manufacturer_brand``(제조사/브랜드)
안 담는다: 상품명 · 가격 · 재고 · 이미지 · 옵션 · 태그 · ``categoryId``
          · 키/토큰 등 비밀값

경계가 애매한 필드는 담지 않는다. 안 담아서 다시 묻는 건 불편이고, 잘못 담아서
다른 상품에 흘러가는 건 **규제 사고**다. 각 템플릿에는 **고시 타입**을 함께
저장한다(그게 상품군 축이다).

적용 규칙
---------
- 템플릿 값은 **사용자가 이번에 직접 준 값을 덮어쓰지 않는다.** 빈 자리만 채운다.
- 적용 결과에 **어떤 필드가 어느 템플릿에서 왔는지** 목록을 실는다.
- 요청한 이름의 템플릿이 **없으면 조용히 넘어가지 않는다** — 명확한 사유
  (``TemplateNotFoundError``).
"""

from __future__ import annotations

import datetime
import json
import re as _re
import shutil
from pathlib import Path
from typing import Any

from . import common

# ---------------------------------------------------------------------------
# 저장소 경로.
#
# ``templates.json`` 은 ``config.json`` 과 **별도 파일**이다. 수명이 다르고
# 복수다(상품군별로 여러 개). ``STATE_DIR``(<cwd>/.local 또는 CLOSSIFY_STATE_DIR)
# 아래에 둔다 — 새 디렉터리 규약을 만들지 않는다.
# ``.gitignore`` 가 이미 ``.local/`` 을 커버하고 있다(비밀 누출 방지).
# ---------------------------------------------------------------------------
_TEMPLATE_FILE_NAME = "templates.json"
_TEMPLATE_VERSION = 1
_BACKUP_SUFFIX = ".bak"


def templates_path() -> Path:
    """템플릿 저장 파일 경로를 반환(단일 진실 공급원).

    ``STATE_DIR`` 아래 ``templates.json``. 파일 존재 여부는 검사하지 않는다
    (호출자가 부재/손상 케이스를 다룬다).
    """
    return Path(common.STATE_DIR) / _TEMPLATE_FILE_NAME


# ---------------------------------------------------------------------------
# 예외.
#
# 조용한 실패 금지 계약: 템플릿이 없거나 저장소가 손상됐을 때 빈 결과로
# 떨어지지 않고 명확한 사유를 던진다. ``apply_template`` 은 이 예외를 잡아
# 결과 메타에 사유를 담는다(호출자가 사용자에게 보이게).
# ---------------------------------------------------------------------------
class TemplateStoreError(RuntimeError):
    """템플릿 저장소가 손상됐거나 읽을 수 없는 경우."""


class TemplateNotFoundError(KeyError):
    """요청한 이름의 템플릿이 없는 경우(조용한 실패 금지)."""


class TemplateNameError(ValueError):
    """템플릿 이름이 비어 있거나 허용 문자 집합을 벗어난 경우."""


# ---------------------------------------------------------------------------
# 템플릿 이름 검증.
#
# 이름은 파일시스템·JSON 키·로그에 안전해야 한다. 빈 문자열/공백/제어문자
# 금지. 100자 이하(과도한 이름 차단). ``[A-Za-z0-9 _./-]`` 외 문자 금지 —
# 경로 구분자·JSON 이스케이프·셸 확장에 안전한 문자 집합.
# ---------------------------------------------------------------------------
_NAME_CHARS = _re.compile(r"^[A-Za-z0-9 _./\uAC00-\uD7A3-]+$")
_NAME_MAX_LEN = 100


def _validate_name(name: str) -> str:
    """템플릿 이름을 검증·정규화한다.

    Returns:
        strip 된 이름.

    Raises:
        TemplateNameError: 빈 문자열/공백이거나 허용 문자 집합을 벗어나거나
            100자를 초과할 때.
    """
    text = str(name or "").strip()
    if not text:
        raise TemplateNameError("템플릿 이름이 비어 있습니다 (이름이 있어야 저장한다).")
    if len(text) > _NAME_MAX_LEN:
        raise TemplateNameError(
            f"템플릿 이름이 {_NAME_MAX_LEN}자를 초과합니다 (got {len(text)}자)."
        )
    if not _NAME_CHARS.match(text):
        raise TemplateNameError(
            "템플릿 이름에 허용되지 않는 문자가 있습니다. "
            "한글·알파벳·숫자·공백·밑줄·점·슬래시·하이픈만 쓸 수 있습니다."
        )
    return text


def _validate_notice_type(notice_type: str) -> str:
    """고시 타입을 정규화한다(대문자, strip).

    빈 문자열은 그대로 둔다(저장 시점에 상품군 정보가 없을 수 있다). 단, 빈
    문자열로 저장된 템플릿은 조회 시 고시타입 축이 모호해지므로 저장 호출자가
    가능하면 채워 넣도록 안내한다.
    """
    return str(notice_type or "").strip().upper()


# ---------------------------------------------------------------------------
# 담는 값 화이트리스트 / 안 담는 값.
#
# **경계가 애매한 필드는 담지 않는다.** 담는 필드는 상품 입력 키 기준으로
# 정확히 나열한다. 화이트리스트에 없는 키가 입력에 있어도 저장하지 않는다
# (조용한 채움 금지 — 상품에만 해당하는 값이 다른 상품에 흘러가는 것을 막는다).
#
# 구조: 각 섹션은 (입력에서 읽을 키 튜플, 빈 값 판정) 의 리스트.
# 빈 값 판정은 "None 이거나 빈 문자열이거나 공백만" 을 기준으로 한다 —
# qa_agents 의 placeholder 판정을 여기서 다시 만들지 않는다(단일 진실 공급원).
# ---------------------------------------------------------------------------
_TEMPLATE_FIELD_KEYS: tuple[str, ...] = (
    # 고시 본문(productInfoProvidedNotice). notice dict 의 키 중 안 담는 것
    # (itemName 같은 상품명·타입 노드 키) 은 빼고 본문 값만 담는다.
    "productInfoProvidedNotice",
    # AS 정보.
    "afterServiceInfo",
    # 배송 정책(반품/교환 배송비).
    "delivery_policy",
    # 원산지.
    "origin",
    # 제조사/브랜드.
    "manufacturer_brand",
)

# 고시 본문 노드에서 **상품 특정값이라 담지 않는** 키.
# itemName(=상품명) 은 상품마다 다르고, productInfoProvidedNoticeType 은
# 템플릿의 notice_type 메타로 이미 저장하므로 본문에서는 뺀다.
# 정책: "상품마다 달라지는 식별값만 뺀다." 이 목록 외에 새로 제외할 필드를
# 늘리지 않는다 — 무엇을 뺄지는 정책이고, 지금 정책은 이 세 가지다.
_NOTICE_BODY_SKIP_KEYS: frozenset[str] = frozenset(
    {"itemName", "productInfoProvidedNoticeType", "name"}
)

# camelCase → snake_case 수동 별칭 후보.
# 정본(``data/notice_types.json``)에 있는 camelCase 필드는 **전부** 후보가
# 되되, top-level common 입력 키로 쓰이는 snake_case 별칭을 같이 후보로
# 둔다. 이 매핑은 ``naver_client._notice_defaults`` 가 읽는 키 일관성을
# 유지한다 — 새 별칭을 만들지 않는다(단일 진실 공급원).
# 본 매핑에 없는 camelCase 필드는 (camelCase, (camelCase,)) 만 후보가 된다.
_NOTICE_BODY_SNAKE_ALIASES: dict[str, tuple[str, ...]] = {
    # 공통 5필드.
    "returnCostReason": ("return_cost_reason",),
    "noRefundReason": ("no_refund_reason",),
    "qualityAssuranceStandard": ("quality_assurance_standard",),
    "compensationProcedure": ("compensation_procedure",),
    "troubleShootingContents": ("trouble_shooting_contents",),
    # AS/제조 관련.
    "manufactureDate": ("manufacture_date",),
    "modelName": ("model_name",),
    "certDetail": ("cert_detail",),
    "madeIn": ("made_in", "origin_content"),
    "safetyStandard": ("safety_standard",),
}


def _camel_to_snake(name: str) -> str:
    """camelCase → snake_case 변환(단순 규칙 기반).

    정본 필드명(예: ``returnCostReason``) 에서 top-level common 입력 키
    후보(``return_cost_reason``) 를 만들 때 쓴다. 본 함수는 *후보를 만들 뿐*
    이며, 실제 매핑의 단일 진실 공급원은 ``_NOTICE_BODY_SNAKE_ALIASES``
    와 ``naver_client._notice_defaults`` 다.
    """
    out: list[str] = []
    for ch in name:
        if ch.isupper():
            if out:
                out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 고시 본문 후보 입력 키(상품 입력 dict 에서 어디서 읽을지).
#
# **정본(``data/notice_types.json``)에서 읽는다.** 과거에는 17개 필드를
# 하드코딩했으나, 정본에 120개 필드가 있어 107개가 조용히 버려지는 결함이
# 있었다(식품·화장품 판매자는 템플릿을 저장해도 대부분이 안 담겼다).
#
# 이제 ``data/notice_types.json`` 의 verified 35타입 전체에서 선언된 모든
# camelCase 필드의 합집합을 후보로 둔다. ``naver_client._load_notice_type_specs``
# 가 이미 이 파일을 캐싱해 읽으므로, 같은 단일 진실 공급원을 따른다(코드를
# 읽고 확인 — 이 파일은 ``common.package_data_path`` + ``importlib.resources``
# 관례를 쓴다).
#
# snake_case 별칭 후보(``return_cost_reason`` 등)는 기존 관례를 유지한다.
# ---------------------------------------------------------------------------
_NOTICE_BODY_FIELD_CANDIDATES_CACHE: tuple[tuple[str, tuple[str, ...]], ...] | None = None


def _load_notice_body_field_candidates() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """``data/notice_types.json`` 에서 고시 본문 후보 필드를 읽는다(캐싱).

    verified 35타입 전체의 ``fields`` 배열 합집합을 구성한다. 각 camelCase
    필드는 (camelCase, (camelCase, snake_case...)) 튜플이 된다. snake_case
    별칭은 ``_NOTICE_BODY_SNAKE_ALIASES`` 우선, 없으면 ``_camel_to_snake``
    자동 변환.

    정본 읽기가 실패하면 **빈 튜플**을 반환하지 않고 **방어적 폴백**으로
    공통 5필드만 반환한다 — 조용한 누락(전체 필드가 사라지는)보다 안전하다.
    다만 이 폴백은 ``naver_client`` 로더와 같은 예외를 전파하지 않으므로
    호출자가 결과 메타에서 후보 개수를 볼 수 있게 한다.
    """
    global _NOTICE_BODY_FIELD_CANDIDATES_CACHE
    if _NOTICE_BODY_FIELD_CANDIDATES_CACHE is not None:
        return _NOTICE_BODY_FIELD_CANDIDATES_CACHE
    try:
        from . import naver_client as _nc

        specs = _nc._load_notice_type_specs()
    except Exception:
        specs = []
    seen: dict[str, tuple[str, ...]] = {}
    for entry in specs:
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, str):
                continue
            camel = field.strip()
            if not camel or camel in seen:
                continue
            aliases = _NOTICE_BODY_SNAKE_ALIASES.get(camel)
            if aliases is None:
                snake = _camel_to_snake(camel)
                aliases = (snake,) if snake and snake != camel else ()
            seen[camel] = (camel, *aliases)
    candidates = tuple((camel, p_keys) for camel, p_keys in seen.items())
    _NOTICE_BODY_FIELD_CANDIDATES_CACHE = candidates
    return candidates


def _notice_body_field_candidates() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """고시 본문 후보를 반환(테스트 주입 가능한 thin wrapper).

    ``_load_notice_body_field_candidates`` 가 정본에서 읽은 후보를 캐싱한다.
    본 함수는 그 결과를 반환한다 — 모듈 수준 상수 대신 함수로 둬서 테스트가
    monkeypatch 하기 쉽게 한다(정본 파일을 일시적으로 바꾸지 않아도 됨).
    """
    return _load_notice_body_field_candidates()


# AS 정보(afterServiceInfo) 후보 입력 키.
_AS_INFO_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("afterServiceTelephoneNumber", ("as_tel", "afterServiceTelephoneNumber")),
    ("afterServiceGuideContent", ("as_guide", "afterServiceGuideContent")),
)

# 배송 정책 후보 입력 키(반품/교환 배송비).
_DELIVERY_POLICY_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("return_delivery_fee", ("return_delivery_fee",)),
    ("exchange_delivery_fee", ("exchange_delivery_fee",)),
)

# 원산지 후보 입력 키.
_ORIGIN_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("origin_area_code", ("origin_code", "origin_area_code")),
    ("origin_content", ("origin_content", "made_in")),
    ("importer", ("importer",)),
)

# 제조사/브랜드 후보 입력 키.
_MANUFACTURER_BRAND_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("manufacturer", ("manufacturer", "seller_name_ko", "sellerNameKo")),
    ("brand", ("brand",)),
)


def _has_text(value: Any) -> bool:
    """값이 "비어있지 않은 텍스트/숫자/bool" 인지 판정.

    None/빈 문자열/공백만 → ``False``. 그 외(숫자·bool·비어있지 않은 문자열) →
    ``True``. placeholder 판정(해당없음/상세참조/TBD)은 여기서 하지 *않는다* —
    저장하는 단계에서는 사용자가 "상세참조" 를 의도적으로 넣었을 수도 있으므로
    저장은 하되, 적용 시점에 받는 쪽이 검수한다. placeholder 판정의 단일 진실
    공급원(qa_agents._is_placeholder_value) 을 여기서 다시 만들지 않는다.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return True  # boolean 고시 필드값(False 포함) 은 유효 입력.
    if isinstance(value, int | float):
        return True
    if isinstance(value, str):
        return bool(value.strip())
    return False


def _pick(product: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    """상품 입력에서 후보 키 중 첫 번째 비어있지 않은 값을 반환.

    Returns:
        ``(발견한 값, 찾은 키)``. 못 찾으면 ``(None, "")``.
    """
    for key in candidates:
        if key in product and _has_text(product.get(key)):
            return product.get(key), key
    return None, ""


def _extract_notice_body(product: dict[str, Any]) -> dict[str, Any]:
    """상품 입력에서 고시 본문 필드만 뽑는다(상품 특정값 itemName 등은 뺀다).

    ``product.notice`` dict 가 있으면 그 안의 노드(etc/wear/...) 본문에서
    camelCase 필드를 읽는다(naver_client._merge_notice 가 쓰는 구조).
    동시에 top-level common 키(return_cost_reason 등) 도 후보로 읽는다 —
    사용자가 두 자리 중 어디에 값을 넣었든 잡는다.
    """
    body: dict[str, Any] = {}
    candidates = _notice_body_field_candidates()
    # (1) ``product.notice.<node>`` 에서 camelCase 필드를 읽는다.
    user_notice = product.get("notice")
    if isinstance(user_notice, dict):
        for node_key, node_value in user_notice.items():
            if not isinstance(node_value, dict):
                continue
            if node_key in ("productInfoProvidedNoticeType", "notice_type"):
                continue
            for camel_field, _ in candidates:
                if camel_field in _NOTICE_BODY_SKIP_KEYS:
                    continue
                if camel_field in node_value and _has_text(node_value.get(camel_field)):
                    body[camel_field] = node_value.get(camel_field)
    # (2) top-level common 키 후보.
    for camel_field, p_keys in candidates:
        if camel_field in _NOTICE_BODY_SKIP_KEYS:
            continue
        if camel_field in body:
            continue
        value, _ = _pick(product, p_keys)
        if value is not None:
            body[camel_field] = value
    return body


def _extract_section(
    product: dict[str, Any],
    candidates: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, Any]:
    """후보 (출력키, 입력키튜플) 리스트에서 비어있지 않은 값만 모은다."""
    section: dict[str, Any] = {}
    for out_key, p_keys in candidates:
        value, _ = _pick(product, p_keys)
        if value is not None:
            section[out_key] = value
    return section


def _utc_now_iso() -> str:
    """현재 UTC 시각을 ISO 8601 문자열로."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 저장소 IO.
#
# 파일이 없으면 빈 상태. 손상됐으면 **조용히 덮어쓰지 않고**
# ``TemplateStoreError`` 로 사유를 알린다(계약: 조용한 덮어쓰기 금지).
# 쓰기 전 백업은 ``config_form_server._backup_config`` 관례를 따른다:
# ``shutil.copy2`` → ``<path>.bak.<UTC타임스탬프>``. 원자적 쓰기는
# ``common._write_json_file``(tmp + ``os.replace``).
# ---------------------------------------------------------------------------


def _empty_store() -> dict[str, Any]:
    """빈 저장소 구조를 반환."""
    return {"version": _TEMPLATE_VERSION, "templates": []}


def _load_store() -> dict[str, Any]:
    """템플릿 저장소를 로드한다.

    Returns:
        저장소 dict. 파일이 없으면 빈 저장소.

    Raises:
        TemplateStoreError: 파일이 있지만 JSON 파싱에 실패했거나 구조가
            올바르지 않은 경우. **조용히 덮어쓰지 않는다.**
    """
    path = templates_path()
    if not path.is_file():
        return _empty_store()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TemplateStoreError(
            f"템플릿 저장소를 읽을 수 없습니다: {path} ({exc}). "
            "조용히 덮어쓰지 않습니다 — 파일 권한을 확인하세요."
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise TemplateStoreError(
            f"템플릿 저장소가 손상되었습니다(JSON 파싱 실패): {path} ({exc}). "
            "조용히 덮어쓰지 않습니다 — 파일을 직접 점검하거나 백업에서 되돌리세요."
        ) from exc
    if not isinstance(data, dict):
        raise TemplateStoreError(
            f"템플릿 저장소 루트가 객체가 아닙니다: {path} "
            f"(got {type(data).__name__}). 조용히 덮어쓰지 않습니다."
        )
    templates = data.get("templates")
    if not isinstance(templates, list):
        raise TemplateStoreError(
            f"템플릿 저장소의 'templates' 가 배열이 아닙니다: {path} "
            f"(got {type(templates).__name__}). 조용히 덮어쓰지 않습니다."
        )
    return data


def _backup_templates(path: Path) -> str:
    """기존 템플릿 파일을 백업. 백업 경로를 반환(백업 안 했으면 빈 문자열).

    ``config_form_server._backup_config`` 와 동일한 관례:
    기존 파일이 있으면 ``<path>.bak.<UTC타임스탬프>`` 로 ``shutil.copy2``.
    백업 실패는 치명적이지 않다 — 쓰기는 계속 진행. 단, 호출자에게 알린다.
    """
    if not path.is_file():
        return ""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_suffix(path.suffix + f"{_BACKUP_SUFFIX}.{ts}")
    try:
        shutil.copy2(path, backup)
        return str(backup)
    except OSError:
        return ""


def _save_store(store: dict[str, Any]) -> tuple[Path, str]:
    """템플릿 저장소를 디스크에 쓴다(백업 후 원자적 쓰기).

    Returns:
        ``(쓴 파일 경로, 백업 경로)``. 백업을 안 했으면 빈 문자열.
    """
    path = templates_path()
    backup_path = _backup_templates(path)
    common._write_json_file(path, store)
    return path, backup_path


# ---------------------------------------------------------------------------
# 등록된 상품(API 응답) → 템플릿 입력 모양 변환.
#
# 이것이 본 작업의 핵심 이음매다. ``get_product`` 는 **네이버 API 모양**으로
# 돌려주고, ``save_template`` 은 **우리 입력 모양**을 받는다. 그대로 넘기면
# 조용히 빈 템플릿이 만들어진다(상단 티켓 배경 참조).
#
# 경로 차이는 앞부분뿐이고 노드 아래 구조는 동일하다:
#
#     API  originProduct.detailAttribute.productInfoProvidedNotice.<노드>.<필드>
#     우리 product.notice.<노드>.<필드>  + top-level common 키
#
# 본 변환 함수는 ``originProduct`` 까지의 경로를 걷어내어, ``save_template``
# 이 읽을 수 있는 ``product`` dict 을 만든다. 동시에 AS·원산지·배송비·제조사
# 도 우리 입력 키(as_tel/origin_code/...) 로 옮긴다.
#
# **조용한 실패 금지** (티켓 1항): 응답에서 경로가 하나라도 없으면 빈 결과를
# 내지 않고 사유(reason) 를 결과에 담는다.
#
# **고시 타입은 응답에서 읽는다** (티켓 1항): 호출자가 따로 주지 않아도 된다.
# 응답에 없으면 추측하지 않고 사유를 반환한다(규제값 창작 금지).
# ---------------------------------------------------------------------------


def _notice_type_fields_for(notice_type: str) -> tuple[str, ...]:
    """정본(``data/notice_types.json``) 에서 해당 고시 타입의 필드 목록을 반환.

    변환 단계에서 완전성 보고(읽어온 N 개가 정본 M 개 중 몇 개인지) 를 위해
    쓴다. 타입을 모르면 빈 튜플(호출자가 완전성 비교를 생략).
    """
    try:
        from . import naver_client as _nc

        spec = _nc._notice_type_spec(notice_type)
    except Exception:
        return ()
    if not isinstance(spec, dict):
        return ()
    fields = spec.get("fields")
    if not isinstance(fields, list):
        return ()
    return tuple(str(f) for f in fields if isinstance(f, str) and f.strip())


def transform_product_to_template_input(
    product_body: dict[str, Any],
) -> dict[str, Any]:
    """네이버 API 응답(originProduct 형태) 을 ``save_template`` 이 받는
    우리 입력 모양으로 변환한다.

    변환은 **명시적**이다 — ``save_template`` 이 읽을 자리로 값을 옮긴다:
      - ``detailAttribute.productInfoProvidedNotice.<node>.*`` → ``notice.<node>.*``
      - ``detailAttribute.afterServiceInfo.afterServiceTelephoneNumber`` → ``as_tel``
      - ``detailAttribute.afterServiceInfo.afterServiceGuideContent`` → ``as_guide``
      - ``detailAttribute.originAreaInfo.originAreaCode`` → ``origin_code``
      - ``detailAttribute.originAreaInfo.content`` → ``origin_content``
      - ``detailAttribute.originAreaInfo.importer`` → ``importer``
      - ``originProduct.deliveryInfo.claimDeliveryInfo.returnDeliveryFee``
        → ``return_delivery_fee``
      - ``originProduct.deliveryInfo.claimDeliveryInfo.exchangeDeliveryFee``
        → ``exchange_delivery_fee``

    **조용한 실패 금지** (티켓 1항): ``originProduct``/``detailAttribute``/
    ``productInfoProvidedNotice`` 가 없거나 고시 필드가 0개면 빈 product 와
    함께 사유(reason) 를 결과에 담는다. 빈 템플릿이 조용히 만들어지지 않는다.

    **고시 타입은 응답에서 읽는다** (티켓 1·4항):
    ``detailAttribute.productInfoProvidedNotice.productInfoProvidedNoticeType``
    에서 읽는다. 호출자가 따로 주지 않아도 된다. 응답에 없으면 추측하지 않고
    사유(reason) 를 결과에 담는다(규제값 창작 금지).

    Returns:
        변환 결과 메타::

            {"ok": bool,
             "notice_type": str,        # 응답에서 읽은 고시 타입(없으면 "")
             "product": dict,           # save_template 용 입력 모양(얕은 복사 가능)
             "reason": str | None,      # 변환 거부/부분 사유(None 이면 정상)
             "notice_field_count": int, # 변환된 고시 본문 필드 수(0 이면 의심)
             "completeness": {          # 정본 대비 완전성(티켓 4항)
                 "filled_count": int,      # 응답에서 읽은 고시 본문 필드 수
                 "type_field_total": int,  # 정본의 해당 타입 필드 수
                 "missing_fields": [...]   # 정본엔 있는데 응답에 없는 필드명
             }}

        ``ok=False`` 면 ``product`` 는 빈 dict 이고 ``reason`` 에 사유가 있다.
        이때 호출자는 저장을 진행하지 않아야 한다(조용한 빈 템플릿 금지).

    Note:
        본 함수는 값을 *옮길 뿐*이다. 값의 진위를 판별하지 않고, 새 값을
        지어내지 않는다(규제값 창작 금지).
    """
    if not isinstance(product_body, dict):
        return {
            "ok": False,
            "notice_type": "",
            "product": {},
            "reason": "product_body 가 dict 가 아닙니다 (네이버 API 응답 형태가 아님).",
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": 0,
                "missing_fields": [],
            },
        }

    origin = product_body.get("originProduct")
    if not isinstance(origin, dict):
        return {
            "ok": False,
            "notice_type": "",
            "product": {},
            "reason": (
                "응답에 originProduct 노드가 없습니다 — 고시를 읽을 수 없습니다. "
                "네이버 API 응답 형태(originProduct.detailAttribute...)인지 확인하세요."
            ),
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": 0,
                "missing_fields": [],
            },
        }

    detail = origin.get("detailAttribute")
    if not isinstance(detail, dict):
        return {
            "ok": False,
            "notice_type": "",
            "product": {},
            "reason": ("응답에 detailAttribute 노드가 없습니다 — 고시를 읽을 수 없습니다."),
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": 0,
                "missing_fields": [],
            },
        }

    notice = detail.get("productInfoProvidedNotice")
    if not isinstance(notice, dict):
        return {
            "ok": False,
            "notice_type": "",
            "product": {},
            "reason": (
                "응답에 productInfoProvidedNotice 노드가 없습니다 — 등록된 상품에 "
                "고시 본문이 없거나 응답이 잘렸습니다."
            ),
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": 0,
                "missing_fields": [],
            },
        }

    # 고시 타입은 응답에서 읽는다 (호출자가 주지 않아도 됨). 없으면 추측 금지.
    raw_type = str(notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    if not raw_type:
        return {
            "ok": False,
            "notice_type": "",
            "product": {},
            "reason": (
                "응답의 productInfoProvidedNotice.productInfoProvidedNoticeType 이 "
                "비어 있습니다 — 고시 타입을 추측하지 않습니다(규제값 창작 금지). "
                "상품이 실제로 고시 대상인지 확인하세요."
            ),
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": 0,
                "missing_fields": [],
            },
        }

    # 고시 본문 노드(etc/wear/...) 들을 우리 입력 모양(product.notice.<node>) 으로.
    # productInfoProvidedNoticeType 노드 키 자체는 메타이므로 product.notice 의
    # 동명 키로 옮긴다(naver_client._merge_notice 가 읽는 구조와 동일).
    our_notice: dict[str, Any] = {"productInfoProvidedNoticeType": raw_type}
    field_count = 0
    for node_key, node_value in notice.items():
        if node_key == "productInfoProvidedNoticeType":
            continue
        if not isinstance(node_value, dict):
            continue
        copied: dict[str, Any] = {}
        for field, value in node_value.items():
            # itemName/상품명/타입 키는 save_template 의 _extract_notice_body 가
            # 어차피 _NOTICE_BODY_SKIP_KEYS 로 빼지만, 변환 단계에서도 동일하게
            # 빼지 않는다 — save_template 의 단일 진실 공급원을 존중한다. 빈
            # 값(None/공백) 도 옮기지 않는다(조용한 빈 값 누적 방지).
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            copied[str(field)] = value
            field_count += 1
        if copied:
            our_notice[str(node_key)] = copied

    if field_count == 0:
        return {
            "ok": False,
            "notice_type": raw_type,
            "product": {},
            "reason": (
                f"productInfoProvidedNotice 의 노드({raw_type}) 에서 고시 필드를 "
                "0개 읽었습니다 — 본문이 비어 있거나 응답이 잘렸습니다. "
                "조용히 빈 템플릿을 만들지 않습니다."
            ),
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": len(_notice_type_fields_for(raw_type)),
                "missing_fields": list(_notice_type_fields_for(raw_type)),
            },
        }

    # AS·원산지·배송비 는 save_template 이 top-level common 키로 읽는다.
    # ``_extract_policy_values_from_product`` (mcp_server) 와 동일 경로/필드명.
    our_product: dict[str, Any] = {"notice": our_notice}
    as_info = detail.get("afterServiceInfo")
    if isinstance(as_info, dict):
        if _has_text(as_info.get("afterServiceTelephoneNumber")):
            our_product["as_tel"] = as_info.get("afterServiceTelephoneNumber")
        if _has_text(as_info.get("afterServiceGuideContent")):
            our_product["as_guide"] = as_info.get("afterServiceGuideContent")
    origin_info = detail.get("originAreaInfo")
    if isinstance(origin_info, dict):
        if _has_text(origin_info.get("originAreaCode")):
            our_product["origin_code"] = origin_info.get("originAreaCode")
        if _has_text(origin_info.get("content")):
            our_product["origin_content"] = origin_info.get("content")
        if _has_text(origin_info.get("importer")):
            our_product["importer"] = origin_info.get("importer")
    # 배송비(반품/교환) — claimDeliveryInfo.
    delivery = origin.get("deliveryInfo")
    if isinstance(delivery, dict):
        claim = delivery.get("claimDeliveryInfo")
        if isinstance(claim, dict):
            if _has_text(claim.get("returnDeliveryFee")):
                our_product["return_delivery_fee"] = claim.get("returnDeliveryFee")
            if _has_text(claim.get("exchangeDeliveryFee")):
                our_product["exchange_delivery_fee"] = claim.get("exchangeDeliveryFee")

    # 상품속성(productAttributes) 편승 — 응답에 있으면 그대로 보존(N58 슬라이스1).
    # 응답에 없으면 조용히 통과(지어내지 않음). 상품군 고정 성격의 속성을
    # 템플릿에 저장하는 것은 원장 판단이나, 그 선별은 이 티켓 밖이다 —
    # 본 티켓은 **읽어서 그대로 보존**까지만.
    raw_attrs = detail.get("productAttributes")
    if isinstance(raw_attrs, list) and raw_attrs:
        our_product["attributes"] = list(raw_attrs)

    # 정본 대비 완전성 — 응답에서 읽은 고시 본문 필드 집합 vs 정본의 해당
    # 타입 필드 집합. 조용한 누락 금지(티켓 4항).
    type_fields = _notice_type_fields_for(raw_type)
    skip_keys = _NOTICE_BODY_SKIP_KEYS
    filled_set: set[str] = set()
    for node_body in our_notice.values():
        if isinstance(node_body, dict):
            for f in node_body:
                if f not in skip_keys:
                    filled_set.add(str(f))
    type_field_set = {f for f in type_fields if f not in skip_keys}
    missing = sorted(type_field_set - filled_set)

    return {
        "ok": True,
        "notice_type": raw_type,
        "product": our_product,
        "reason": None,
        "notice_field_count": field_count,
        "completeness": {
            "filled_count": len(filled_set & type_field_set) if type_field_set else len(filled_set),
            "type_field_total": len(type_field_set),
            "missing_fields": missing,
        },
    }


# ---------------------------------------------------------------------------
# prepared payload → 템플릿 입력 변환 (N15).
#
# prepared payload 의 ``product`` 블록은 **이미 우리 입력 모양**이다 —
# ``notice``, ``as_tel``, ``origin_code``, ``manufacturer``, ``importer`` 등
# ``save_template`` 이 읽는 키가 이미 들어 있다. 따라서 API 응답 경로
# (``transform_product_to_template_input``) 처럼 경로를 걷어낼 필요가 없다.
#
# 본 함수가 하는 일은 **완전성 보고**다 — prepared 의 고시 본문에서 읽은
# 필드가 정본 대비 몇 개인지, 어느 필드가 빠졌는지를 ``transform_product_to_
# template_input`` 과 **동일한 형식**으로 계산한다. 새 변환 규칙을 만들지
# 않는다 — 완전성 계산의 단일 진실 공급원(``_notice_type_fields_for`` +
# ``_NOTICE_BODY_SKIP_KEYS``)을 그대로 쓴다.
#
# **네트워크 호출 0회** — 로컬 파일(prepared payload)과 정본 데이터
# (``data/notice_types.json`` 캐싱)만 읽는다. 이미지 재업로드가 없다.
# ---------------------------------------------------------------------------


def transform_prepared_to_template_input(
    prepared_product: dict[str, Any],
) -> dict[str, Any]:
    """prepared payload 의 ``product`` 블록에서 템플릿 입력과 완전성을 뽑는다.

    prepared payload 의 ``product`` 는 이미 우리 입력 모양이므로, 값을
    *옮기지 않는다* — 그대로 ``save_template`` 에 넘길 수 있다. 본 함수가
    추가로 하는 일은 ``transform_product_to_template_input`` 과 **동일한
    완전성 계산**을 수행해, 결과 메타에 ``notice_type``/``completeness``/
    ``notice_field_count`` 를 채우는 것이다.

    고시 타입은 ``product.notice.productInfoProvidedNoticeType`` 에서 읽는다.
    없으면 ETC 폴백 — prepared 단계에서 컴플라이언스 보정이 이미 타입을
    주입했을 수 있으므로, 여기서 새로 추론하지 않는다(규제값 창작 금지).
    다만 타입을 못 읽었을 때 ``ok=True`` 를 유지하되 ``reason`` 에 사실을
    담는다 — prepared 의 값 자체는 유효하므로 저장을 막지 않는다.

    **네트워크 호출 0회** — 로컬 파일만 읽는다.

    Args:
        prepared_product: prepared payload 의 ``product`` dict (또는 그와
            동일한 모양의 dict). ``notice``, ``as_tel``, ``origin_code`` 등을
            포함.

    Returns:
        ``transform_product_to_template_input`` 과 **동일한 형식**::

            {"ok": bool,
             "notice_type": str,
             "product": dict,           # save_template 용 입력(그대로 전달)
             "reason": str | None,
             "notice_field_count": int,
             "completeness": {filled_count, type_field_total, missing_fields}}
    """
    if not isinstance(prepared_product, dict):
        return {
            "ok": False,
            "notice_type": "",
            "product": {},
            "reason": "prepared_product 가 dict 가 아닙니다.",
            "notice_field_count": 0,
            "completeness": {
                "filled_count": 0,
                "type_field_total": 0,
                "missing_fields": [],
            },
        }

    # 고시 타입 읽기 — notice.productInfoProvidedNoticeType 우선.
    user_notice = prepared_product.get("notice")
    raw_type = ""
    if isinstance(user_notice, dict):
        raw_type = (
            str(
                user_notice.get("productInfoProvidedNoticeType")
                or user_notice.get("notice_type")
                or ""
            )
            .strip()
            .upper()
        )
    # prepared 단계에서 ETC 로 떨어질 수 있다 — 그것은 "정말 ETC" 일 수도
    # 있고 "추론 실패" 일 수도 있다. 여기서 새로 추론하지 않는다. ETC 도
    # 유효한 고시 타입이므로 그대로 둔다.

    # 고시 본문에서 채워진 필드 수 계산 — _extract_notice_body 와 동일한
    # 로직으로 filled_set 을 만든다 (단일 진실 공급원 존중).
    skip_keys = _NOTICE_BODY_SKIP_KEYS
    filled_set: set[str] = set()
    field_count = 0
    if isinstance(user_notice, dict):
        for node_key, node_value in user_notice.items():
            if not isinstance(node_value, dict):
                continue
            if node_key in ("productInfoProvidedNoticeType", "notice_type"):
                continue
            for field, value in node_value.items():
                if field in skip_keys:
                    continue
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                filled_set.add(str(field))
                field_count += 1

    # 완전성 계산 — transform_product_to_template_input 과 동일한 로직.
    type_fields = _notice_type_fields_for(raw_type) if raw_type else ()
    type_field_set = {f for f in type_fields if f not in skip_keys}
    if type_field_set:
        missing = sorted(type_field_set - filled_set)
        filled_count = len(filled_set & type_field_set)
    else:
        missing = []
        filled_count = len(filled_set)

    return {
        "ok": True,
        "notice_type": raw_type,
        "product": dict(prepared_product),  # 얕은 복사 — save_template 이 화이트리스트 추출.
        "reason": None,
        "notice_field_count": field_count,
        "completeness": {
            "filled_count": filled_count,
            "type_field_total": len(type_field_set),
            "missing_fields": missing,
        },
    }


# ---------------------------------------------------------------------------
# 공개 API: 저장·조회·적용.
# ---------------------------------------------------------------------------


def save_template(
    *,
    name: str,
    notice_type: str,
    product: dict[str, Any],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """상품 입력에서 안전한 필드만 뽑아 템플릿으로 저장한다.

    **자동 적용 금지** — 본 함수는 저장만 한다. 적용은 ``apply_template`` 이
    사용자가 명시적으로 지목할 때만 한다.

    **화이트리스트** — ``_TEMPLATE_FIELD_KEYS`` 에 없는 키는 저장하지 않는다.
    비밀값(키/토큰)·상품 특정값(이름/가격/재고/이미지/옵션/태그/categoryId) 은
    어떤 경우에도 담기지 않는다.

    같은 이름의 템플릿이 있으면 **덮어쓴다**(사용자가 이름으로 지목한 갱신).
    이때도 쓰기 전 백업을 남긴다.

    **출처 기록 (티켓 3항 — 규제값이므로 필수):** ``source`` 가 주어지면
    저장 엔트리에 *어느 상품에서 읽었는지*를 남긴다(상품번호·읽은 시각).
    이것이 "값을 창작하지 않았다" 는 증거다. ``source`` 는::

        {"origin_product_no": str,   # 필수 — 어느 상품에서 읽었는지
         "read_at": str,             # 선택 — 읽은 시각(ISO 8601 UTC).
         # transform_product_to_template_input 의 completeness/reason/notice_type
         # 도 함께 담겨도 좋다 — 모두 출처·완전성 증거다.
        }

    ``source`` 가 ``None`` 이면 출처를 기록하지 않는다(사용자가 직접 입력한
    상품에서 저장하는 기존 경로 — 회귀 없음).

    Args:
        name: 템플릿 이름. 비어있거나 허용 문자 집합을 벗어나면 ``TemplateNameError``.
        notice_type: 고시 타입(ETC/WEAR/...). 상품군 축으로 저장된다.
        product: 상품 입력 dict. 여기서 안전한 필드만 뽑는다.
        source: 등록된 상품에서 읽었을 때 출처 메타. ``origin_product_no`` 가
            비어있지 않은 문자열이면 저장 엔트리의 ``source`` 블록에 기록된다.

    Returns:
        결과 메타 dict::

            {"ok": True, "name": str, "notice_type": str,
             "saved_keys": [...],   # 저장된 섹션명 목록
             "skipped_keys": [...], # 입력에 있었으나 안 담는 키(상품특정/비밀)
             "notice_field_summary": {  # 고시 본문 가시성(조용한 누락 방지)
                 "filled_count": int,       # 채워진 고시 필드 수
                 "candidate_total": int,    # 정본 후보 총수(120 etc.)
                 "filled_fields": [...]     # 채워진 필드명(값 아님)
             },
             "completeness": {...} | None,  # 정본 대비 완전성(source 가 줄 때만)
             "source_recorded": bool,       # 출처가 엔트리에 기록됐는지
             "path": str, "backup_path": str, "created_at": str}

        ``saved_keys`` / ``skipped_keys`` / ``filled_fields`` 는 섹션/키/필드
        *이름* 만 담는다. 값은 절대 담지 않는다(비밀값 비노출 — 결과가
        로그/반환에 흘러도 안전).
    """
    sane_name = _validate_name(name)
    sane_type = _validate_notice_type(notice_type)
    if not isinstance(product, dict):
        raise ValueError("save_template: product 는 dict 여야 합니다.")

    # 화이트리스트 필드만 추출.
    fields: dict[str, Any] = {}
    fields["productInfoProvidedNotice"] = _extract_notice_body(product)
    fields["afterServiceInfo"] = _extract_section(product, _AS_INFO_CANDIDATES)
    fields["delivery_policy"] = _extract_section(product, _DELIVERY_POLICY_CANDIDATES)
    fields["origin"] = _extract_section(product, _ORIGIN_CANDIDATES)
    fields["manufacturer_brand"] = _extract_section(product, _MANUFACTURER_BRAND_CANDIDATES)
    # 빈 섹션은 저장은 하되 결과에 "빈 섹션" 임을 드러낸다(사용자가 뭘 넣었는지
    # 알 수 있게 — 조용한 빈 값 금지).
    saved_sections = [k for k, v in fields.items() if isinstance(v, dict) and v]

    # 고시 본문 가시성 — 얼마나 많은 필드가 채워졌는지, 정본에 몇 개 후보가
    # 있었는지를 결과에 싣는다. 과거에는 17개 하드코딩 필드만 쓰고 나머지는
    # 조용히 버려서, 사용자가 "식품 필드를 넣었는데 안 담겼다" 는 결함을
    # 알 수 없었다. 이제 후보 총수·채워진 수·채워진 필드명(값 아님) 을 보여준다.
    notice_body_filled = fields["productInfoProvidedNotice"]
    notice_filled_names = (
        sorted(notice_body_filled.keys()) if isinstance(notice_body_filled, dict) else []
    )
    notice_candidate_total = len(_notice_body_field_candidates())

    # 입력에 있었으나 안 담은 키(상품 특정값/비밀값/경계 애매) 목록 —
    # 사용자가 "내가 준 값이 다 저장됐다" 고 착각하지 않게.
    _skip_product_specific = {
        "name",
        "title_ko",
        "salePrice",
        "sell_price",
        "price",
        "stock",
        "stockQuantity",
        "image_sources",
        "images",
        "options",
        "option_groups",
        "optionGroupNames",
        "tags",
        "categoryId",
        "category_id",
        "courier",
        "delivery_fee",
        "display",
        "status",
        "product_key",
        "summary",
        "desc",
        "props",
        "attributes",
        "url",
        "source_url",
        "item_url",
        "detail_url",
        # 비밀/자격증명 키(어떤 형태든 담지 않는다).
        "client_id",
        "client_secret",
        "api_key",
        "access_token",
        "secret",
        "token",
        "password",
    }
    skipped: list[str] = []
    for key in product:
        if key in _skip_product_specific:
            skipped.append(key)
    # "notice" 자체는 고시 본문을 뽑아갔으므로 skipped 에 넣지 않는다(본문 값은
    # 담겼다). 단, notice 안의 itemName 같은 상품특정값은 _extract_notice_body
    # 가 이미 뺐다 — 그 사실은 saved_sections 의 productInfoProvidedNotice 에
    # 본문 키가 들어가 있어 드러난다.

    store = _load_store()  # 손상시 TemplateStoreError 전파(조용한 덮어쓰기 금지).
    templates = list(store.get("templates") or [])

    # 출처 블록 — 등록된 상품에서 읽었을 때 *어느 상품인지*를 엔트리에 남긴다
    # (티켓 3항 — 규제값이므로 출처가 있어야 "값을 창작하지 않았다" 는 증거가 됨).
    # ``origin_product_no`` 가 비어있지 않은 문자열일 때만 기록한다. 그 외
    # (source None / origin_product_no 빈값) 는 기존 경로(회귀 없음).
    source_block: dict[str, Any] | None = None
    source_completeness: dict[str, Any] | None = None
    if isinstance(source, dict):
        src_no = str(source.get("origin_product_no") or "").strip()
        if src_no:
            source_block = {
                "origin_product_no": src_no,
                "read_at": str(source.get("read_at") or _utc_now_iso()),
            }
            # transform_product_to_template_input 의 completeness/reason 도
            # 출처·완전성 증거로 같이 담는다(값이 있을 때만).
            if isinstance(source.get("completeness"), dict):
                comp = source["completeness"]
                source_block["completeness"] = {
                    "filled_count": int(comp.get("filled_count") or 0),
                    "type_field_total": int(comp.get("type_field_total") or 0),
                    "missing_fields": list(comp.get("missing_fields") or []),
                }
                source_completeness = source_block["completeness"]
            if source.get("reason"):
                source_block["reason"] = str(source["reason"])

    created_at = _utc_now_iso()
    entry: dict[str, Any] = {
        "name": sane_name,
        "notice_type": sane_type,
        "created_at": created_at,
        "fields": fields,
    }
    if source_block is not None:
        entry["source"] = source_block
    # 같은 이름 갱신 — 리스트에서 제거 후 append(순서는 의미 없음).
    replaced = False
    for i, existing in enumerate(templates):
        if isinstance(existing, dict) and existing.get("name") == sane_name:
            templates[i] = entry
            replaced = True
            break
    if not replaced:
        templates.append(entry)
    store["templates"] = templates
    store["version"] = _TEMPLATE_VERSION

    path, backup_path = _save_store(store)
    return {
        "ok": True,
        "name": sane_name,
        "notice_type": sane_type,
        "saved_keys": saved_sections,
        "skipped_keys": sorted(set(skipped)),
        # 고시 본문 가시성 — 필드 *이름* 만(값 안 담음). 사용자가 "내가 넣은
        # 식품/화장품 필드가 담겼는지" 확인할 수 있게.
        "notice_field_summary": {
            "filled_count": len(notice_filled_names),
            "candidate_total": notice_candidate_total,
            "filled_fields": notice_filled_names,
        },
        # 정본 대비 완전성 — source.completeness 가 줄 때만(등록된 상품에서
        # 읽은 경로). 사용자 직접 입력 경로(source=None) 에서는 None — 완전성
        # 비교의 기준이 "응답에서 읽은 N 개" 이므로.
        "completeness": source_completeness,
        # 출처가 엔트리에 기록됐는지(호출자가 "출처가 남았다" 확인 가능).
        "source_recorded": source_block is not None,
        "path": str(path),
        "backup_path": backup_path,
        "created_at": created_at,
        "replaced": replaced,
    }


def list_templates() -> list[dict[str, Any]]:
    """템플릿 이름·고시타입 목록만 반환(값은 안 보낸다).

    ``check_config`` 가 이 목록을 결과에 싣는다. **값 자체는 넣지 않는다.**

    Returns:
        ``[{"name": str, "notice_type": str, "created_at": str}, ...]``.
        파일이 없으면 빈 리스트.

    Raises:
        TemplateStoreError: 저장소가 손상된 경우(조용한 덮어쓰기 금지).
    """
    store = _load_store()
    out: list[dict[str, Any]] = []
    for entry in store.get("templates") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "notice_type": _validate_notice_type(str(entry.get("notice_type") or "")),
                "created_at": str(entry.get("created_at") or ""),
            }
        )
    return out


def _get_template(name: str) -> dict[str, Any]:
    """이름으로 템플릿 엔트리를 찾는다(내부 헬퍼).

    Raises:
        TemplateNameError: 이름이 빈 문자열/공백.
        TemplateNotFoundError: 그 이름의 템플릿이 없음(조용한 실패 금지).
    """
    sane_name = _validate_name(name)
    store = _load_store()
    for entry in store.get("templates") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == sane_name:
            return entry
    raise TemplateNotFoundError(sane_name)


def apply_template(
    *,
    name: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    """명시적으로 지목한 템플릿의 값을 상품 입력의 빈 자리에 채운다.

    **자동 적용 금지.** ``name`` 이 명시적으로 주어졌을 때만 적용한다. 빈
    문자열이면 적용하지 않고 빈 결과를 반환한다(호출자가 "이름 안 주면 어떤
    템플릿도 적용되지 않는다" 를 만족한다).

    **사용자가 직접 준 값을 덮어쓰지 않는다.** 빈 자리만 채운다.

    Args:
        name: 적용할 템플릿 이름. 빈 문자열 → 적용 안 함(암묵 적용 없음).
        product: 상품 입력 dict. **in-place** 로 갱신한다(호출자가 같은 객체를
            계속 쓴다).

    Returns:
        적용 결과 메타::

            {"applied": bool, "template_name": str, "notice_type": str,
             "filled": [...],   # [{"section": str, "field": str}, ...]
                                  어느 필드를 어느 템플릿에서 채웠는지(출처)
             "not_found": str|None,  # 템플릿이 없을 때 사유
             "skipped_existing": [...]}  # 사용자가 이미 준 값이라 안 덮은 필드

    Raises:
        TemplateNameError: 이름이 형식을 벗어날 때(빈 문자열 아님).
    """
    # 빈 이름 → 적용 안 함. 암묵 적용(가장 최근 것/하나뿐이니까) 절대 금지.
    if not str(name or "").strip():
        return {
            "applied": False,
            "template_name": "",
            "notice_type": "",
            "filled": [],
            "not_found": None,
            "skipped_existing": [],
            "reason": "이름이 주어지지 않았습니다 — 어떤 템플릿도 적용하지 않습니다.",
        }
    if not isinstance(product, dict):
        raise ValueError("apply_template: product 는 dict 여야 합니다.")

    try:
        entry = _get_template(name)
    except TemplateNotFoundError:
        return {
            "applied": False,
            "template_name": str(name).strip(),
            "notice_type": "",
            "filled": [],
            "not_found": (
                f"템플릿 '{str(name).strip()}' 을(를) 찾을 수 없습니다. "
                "조용히 넘기지 않습니다 — check_config 로 저장된 템플릿 목록을 "
                "확인하세요."
            ),
            "skipped_existing": [],
        }

    fields = entry.get("fields") if isinstance(entry.get("fields"), dict) else {}
    sane_name = str(entry.get("name") or "").strip()
    sane_type = _validate_notice_type(str(entry.get("notice_type") or ""))
    filled: list[dict[str, str]] = []
    skipped_existing: list[dict[str, str]] = []

    # 고시 본문: ``product.notice.<node>`` 또는 top-level common 키에 채운다.
    # 사용자가 이미 준 필드는 덮지 않는다(빈 자리만).
    notice_body = fields.get("productInfoProvidedNotice") or {}
    if isinstance(notice_body, dict) and notice_body:
        # ``product.notice`` 가 없으면 만든다. node 키는 notice_type 에서.
        # naver_client._merge_notice 가 같은 규칙(타입→node) 을 쓰므로, 템플릿의
        # notice_type 을 따라 node 키를 정한다.
        if not isinstance(product.get("notice"), dict):
            product["notice"] = {}
        user_notice = product["notice"]
        node_key = _node_key_for_type(sane_type)
        if not isinstance(user_notice.get(node_key), dict):
            user_notice[node_key] = {}
        node_body = user_notice[node_key]
        for field, value in notice_body.items():
            if field in _NOTICE_BODY_SKIP_KEYS:
                continue
            # 사용자가 이미 node 본문에 같은 필드를 줬으면 덮지 않는다.
            if _has_text(node_body.get(field)):
                skipped_existing.append({"section": "productInfoProvidedNotice", "field": field})
                continue
            # top-level common 키로도 사용자가 줬을 수 있다 — 그것도 존중.
            top_level_keys = _top_level_keys_for(field)
            if top_level_keys and any(
                key in product and _has_text(product.get(key)) for key in top_level_keys
            ):
                skipped_existing.append({"section": "productInfoProvidedNotice", "field": field})
                continue
            node_body[field] = value
            filled.append({"section": "productInfoProvidedNotice", "field": field})
        # productInfoProvidedNoticeType 이 비어있으면 템플릿의 타입으로 채운다
        # (사용자가 명시한 타입이 있으면 그것이 우선 — 덮지 않는다).
        existing_type = str(
            user_notice.get("productInfoProvidedNoticeType") or user_notice.get("notice_type") or ""
        ).strip()
        if not existing_type and sane_type:
            user_notice["productInfoProvidedNoticeType"] = sane_type
            filled.append(
                {"section": "productInfoProvidedNotice", "field": "productInfoProvidedNoticeType"}
            )

    # AS 정보.
    _fill_section(
        product,
        fields.get("afterServiceInfo") or {},
        _AS_INFO_CANDIDATES,
        section_name="afterServiceInfo",
        filled=filled,
        skipped=skipped_existing,
    )
    # 배송 정책.
    _fill_section(
        product,
        fields.get("delivery_policy") or {},
        _DELIVERY_POLICY_CANDIDATES,
        section_name="delivery_policy",
        filled=filled,
        skipped=skipped_existing,
    )
    # 원산지.
    _fill_section(
        product,
        fields.get("origin") or {},
        _ORIGIN_CANDIDATES,
        section_name="origin",
        filled=filled,
        skipped=skipped_existing,
    )
    # 제조사/브랜드.
    _fill_section(
        product,
        fields.get("manufacturer_brand") or {},
        _MANUFACTURER_BRAND_CANDIDATES,
        section_name="manufacturer_brand",
        filled=filled,
        skipped=skipped_existing,
    )

    return {
        "applied": bool(filled),
        "template_name": sane_name,
        "notice_type": sane_type,
        "filled": filled,
        "not_found": None,
        "skipped_existing": skipped_existing,
    }


def _node_key_for_type(notice_type: str) -> str:
    """고시 타입 → node 키(etc/wear/furniture/...).

    ``naver_client._notice_type_spec`` 과 같은 단일 진실 공급원을 쓴다.
    알 수 없는 타입/빈 문자열이면 ``etc`` 폴백(기존 동작 보존).
    """
    try:
        from . import naver_client as _nc

        spec = _nc._notice_type_spec(notice_type)
        if isinstance(spec, dict) and spec.get("node"):
            return str(spec["node"])
    except Exception:
        pass
    return "etc"


def _top_level_keys_for(field: str) -> tuple[str, ...]:
    """고시 camelCase 필드명 → top-level common 입력 키 후보.

    ``_notice_body_field_candidates()`` (정본에서 읽는 동적 후보) 에서
    찾는다 — 과거 하드코딩 상수를 쓰지 않는다.
    """
    for camel, p_keys in _notice_body_field_candidates():
        if camel == field:
            return p_keys
    return ()


def _fill_section(
    product: dict[str, Any],
    template_section: dict[str, Any],
    candidates: tuple[tuple[str, tuple[str, ...]], ...],
    *,
    section_name: str,
    filled: list[dict[str, str]],
    skipped: list[dict[str, str]],
) -> None:
    """템플릿 섹션의 값을 상품 입력의 빈 자리에 채운다(in-place).

    사용자가 이미 준 값(``product[p_key]`` 가 비어있지 않음) 은 덮지 않는다.
    """
    if not isinstance(template_section, dict) or not template_section:
        return
    # candidates 의 out_key 가 템플릿 섹션에 있는 값만 다룬다.
    for out_key, p_keys in candidates:
        value = template_section.get(out_key)
        if not _has_text(value):
            continue
        # 사용자가 이미 p_keys 중 하나로 값을 줬는지 확인.
        already = any(key in product and _has_text(product.get(key)) for key in p_keys)
        if already:
            for key in p_keys:
                if key in product and _has_text(product.get(key)):
                    skipped.append({"section": section_name, "field": out_key})
                    break
            continue
        # 빈 자리 — 첫 번째 p_keys 키에 채운다.
        first_key = p_keys[0]
        product[first_key] = value
        filled.append({"section": section_name, "field": out_key})


__all__ = [
    "TemplateNameError",
    "TemplateNotFoundError",
    "TemplateStoreError",
    "apply_template",
    "list_templates",
    "save_template",
    "templates_path",
    "transform_prepared_to_template_input",
    "transform_product_to_template_input",
]
