# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""거부 시점 진단 — "첫 결핍"이 아니라 "필요한 전부"를 돌려준다.

``prepare_listing`` 이 거부할 때, 그 시점에 **오프라인으로** 알아낼 수 있는
필요사항을 한 번에 모아 돌려준다. 호출자가 한 턴에 전부 물어볼 수 있게 한다.

계약:
  - **순수 함수** — 네트워크·LLM·파일쓰기 0회. 읽기 전용 데이터만 쓴다.
  - **법적 신고값을 지어내지 않는다.** 원산지·KC·인증·고시 필드 값을 채우지 않는다.
  - **카테고리를 확정하지 않는다.** 후보와 점수를 보여줄 뿐이다.
  - **거부를 통과로 바꾸지 않는다.** 이 모듈은 진단만 한다.

의존 방향: ``category`` · ``qa_agents`` · ``listing_templates`` · ``mcp_server``
(읽기 전용) → 본 모듈. 본 모듈은 위 모듈의 공개 함수만 사용한다.
"""

from __future__ import annotations

from typing import Any

from . import category as _category
from . import listing_templates as _listing_templates
from . import qa_agents as _qa_agents


def _candidates_from_title(title_ko: str) -> tuple[list[dict[str, Any]], bool]:
    """상품명 분류 결과로 (후보리스트, needs_category_choice) 를 반환.

    ``classify_category`` 의 결과에서 후보를 추출한다:
      - 강한 단일 후보(확정 id) 인 경우 → 해당 카테고리를 단일 후보로.
        ``needs_category_choice = False`` (카테고리 확정됨).
      - LLM 위임(ambiguous) 인 경우 → input.candidates **전체**(자르지 않음).
        ``needs_category_choice = True`` (호출자가 사용자에게 골라야 함).
      - 후보 없음 → 빈 리스트.

    Returns:
        ``(candidates, needs_category_choice)``.
        ``candidates``: ``[{"category_id": str, "path": str, "score": int}, ...]``.
        ``needs_category_choice``: ``classify_category`` 가 dict(LLM 위임) 을
        반환했으면 ``True``, str(확정 id) 이면 ``False``.
    """
    if not title_ko or not str(title_ko).strip():
        return [], False
    try:
        result = _category.classify_category(str(title_ko), {}, "")
    except Exception:
        return [], False
    if isinstance(result, str):
        # 강한 단일 후보 확정 — category_path 로 경로를 가져온다.
        cat_id = result
        try:
            path = _category.category_path(cat_id) or ""
        except Exception:
            path = ""
        return [{"category_id": cat_id, "path": path, "score": 0}], False
    if isinstance(result, dict) and result.get("needs_llm"):
        raw_candidates = (
            result.get("input", {}).get("candidates")
            if isinstance(result.get("input"), dict)
            else None
        ) or []
        out: list[dict[str, Any]] = []
        for c in raw_candidates:
            if not isinstance(c, dict):
                continue
            out.append(
                {
                    "category_id": str(c.get("category_id") or ""),
                    "path": str(c.get("path") or ""),
                    "score": int(c.get("score") or 0),
                }
            )
        return out, True
    return [], False


def _top_candidates(title_ko: str, limit: int = 3) -> list[dict[str, Any]]:
    """상위 카테고리 후보를 ``limit`` 개까지 반환(표시용 얕은 래퍼).

    판정에는 ``_candidates_from_title`` 이 주는 **전체** 후보를 써야 한다(F1).
    본 함수는 하위호환을 위해 남겨둔 얇은 래퍼로, **표시용** 으로만 쓴다.
    """
    candidates, _ = _candidates_from_title(title_ko)
    return candidates[:limit]


def _notice_types_for_candidates(candidates: list[dict[str, Any]]) -> list[str]:
    """후보 리스트에서 각 후보의 고시 타입을 추론.

    ``qa_agents._infer_notice_type`` 에 ``{"category_path": ...}`` dict 를 넘긴다
    (문자열을 넘기면 무조건 ETC 가 나오는 함정을 피한다).
    """
    types: list[str] = []
    for c in candidates:
        path = str(c.get("path") or "")
        try:
            nt = _qa_agents._infer_notice_type({"category_path": path})
        except Exception:
            nt = "ETC"
        types.append(str(nt or "ETC"))
    return types


def _explicit_notice_type_from_input(product: dict[str, Any]) -> str | None:
    """사용자가 **명시한** 고시타입을 입력에서 읽는다(F3 우선순위 1).

    ``notice.productInfoProvidedNoticeType`` 또는 ``notice.notice_type`` 또는
    top-level ``notice_type`` / ``productInfoProvidedNoticeType`` 을 본다.
    추측이 아니라 사용자가 준 확정 정보다.

    Returns:
        대문자 고시타입 문자열, 또는 ``None`` (입력에 없을 때).
    """
    if not isinstance(product, dict):
        return None
    notice = product.get("notice")
    if isinstance(notice, dict):
        for key in ("productInfoProvidedNoticeType", "notice_type"):
            v = notice.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().upper()
    for key in ("productInfoProvidedNoticeType", "notice_type"):
        v = product.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return None


def _explicit_notice_type_from_category_id(product: dict[str, Any]) -> str | None:
    """``categoryId`` / ``category_id`` 에서 고시타입을 정한다(F3 우선순위 2).

    ``category_path(cid)`` 로 경로를 얻어 ``_infer_notice_type`` 에 넘긴다.
    모르는 ID 는 **빈 문자열**을 돌려주고 예외를 던지지 않는다. 빈 경로면
    ``None`` 을 반환해 호출자가 3번 경로(상품명 분류)로 넘어가게 한다.

    Returns:
        대문자 고시타입 문자열, 또는 ``None`` (ID 가 없거나 경로를 못 얻었을 때).
    """
    if not isinstance(product, dict):
        return None
    cid = ""
    for key in ("categoryId", "category_id"):
        v = product.get(key)
        if isinstance(v, str) and v.strip():
            cid = v.strip()
            break
        if isinstance(v, int) and v:
            cid = str(v)
            break
    if not cid:
        return None
    try:
        path = _category.category_path(cid) or ""
    except Exception:
        path = ""
    if not path:
        return None
    try:
        nt = _qa_agents._infer_notice_type({"category_path": path})
    except Exception:
        return None
    if isinstance(nt, str) and nt.strip():
        return nt.strip().upper()
    return None


def _xor_groups_for_types(notice_types: list[str]) -> list[list[str]]:
    """여러 고시타입의 XOR 그룹을 합친다(F4).

    ``qa_agents._notice_xor_groups`` 로 각 타입의 XOR 그룹을 읽어, 중복 없이
    합친다. 같은 그룹(멤버 집합이 같은) 은 한 번만 담는다.

    Returns:
        XOR 그룹 리스트. 각 그룹은 필드명 리스트(길이 2 이상).
    """
    seen_keys: set[frozenset[str]] = set()
    out: list[list[str]] = []
    for nt in notice_types:
        try:
            groups = _qa_agents._notice_xor_groups(nt)
        except Exception:
            groups = []
        for group in groups or []:
            if not isinstance(group, list) or len(group) < 2:
                continue
            key = frozenset(group)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(list(group))
    return out


def _valid_image_count(image_sources: Any) -> int:
    """``image_sources`` 에서 **유효한**(비어있지 않은 문자열) 항목 수를 센다(F5).

    문자열이 아니거나 공백만인 항목은 세지 않는다. **파일 존재 확인·네트워크
    확인은 하지 않는다**(진단은 순수 함수여야 한다). 공백 판정까지만 한다.

    Returns:
        유효한(비어있지 않은 문자열) 항목 수.
    """
    if not isinstance(image_sources, list):
        return 0
    count = 0
    for src in image_sources:
        if isinstance(src, str) and src.strip():
            count += 1
    return count


def _build_missing_items(product: dict[str, Any]) -> list[dict[str, str]]:
    """지금 없는 필수 항목 전부를 ``missing`` 리스트로 만든다.

    이미 있는 항목은 넣지 않는다. 라벨은 사람이 읽는 한국어.
    """
    missing: list[dict[str, str]] = []

    name = str(product.get("name") or product.get("title_ko") or "").strip()
    if not name:
        missing.append(
            {
                "field": "name",
                "label": "상품명",
                "why": "등록할 상품의 이름이 필요합니다.",
                "answer_shape": "text",
            }
        )

    sale_price = product.get("salePrice")
    if sale_price is None:
        sale_price = product.get("sell_price") or product.get("price")
    if sale_price is None:
        missing.append(
            {
                "field": "salePrice",
                "label": "판매가",
                "why": "KRW 판매가가 필요합니다.",
                "answer_shape": "number",
            }
        )

    # F5: 유효한(공백 아닌 문자열) 이미지가 0개면 missing 에 넣는다.
    image_sources = product.get("image_sources")
    if _valid_image_count(image_sources) == 0:
        missing.append(
            {
                "field": "image_sources",
                "label": "상품 이미지",
                "why": "최소 1장 이상의 실재 상품 사진이 필요합니다.",
                "answer_shape": "image_list",
            }
        )

    return missing


def _fields_with_labels(
    field_names: tuple[str, ...] | list[str],
    notice_type: str | None,
) -> list[dict[str, str]]:
    """필드명 목록을 ``[{"field": str, "label": str}, ...]`` 로 바꾼다.

    라벨은 ``mcp_server._notice_field_label`` 을 재사용한다 (중복 구현 금지).
    """
    from . import mcp_server as _mcp_server

    out: list[dict[str, str]] = []
    for field in field_names:
        try:
            label, _hint = _mcp_server._notice_field_label(field, notice_type)
        except Exception:
            label = field
        out.append({"field": str(field), "label": str(label)})
    return out


def _intersect_field_lists(
    lists: list[tuple[str, ...]],
) -> tuple[str, ...]:
    """여러 필드 목록의 **교집합** 을 구한다 (순서는 첫 목록을 따른다).

    빈 목록이 하나라도 있으면 교집합은 빈 튜플이다 — 전체가 다 필요하므로.
    입력이 빈 리스트면 빈 튜플.
    """
    if not lists:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    first = lists[0]
    rest_sets = [set(lst) for lst in lists[1:]]
    for field in first:
        if field in seen:
            continue
        if all(field in s for s in rest_sets):
            result.append(field)
            seen.add(field)
    return tuple(result)


def _build_notice_required_fields_block(
    candidates: list[dict[str, Any]],
    notice_types: list[str],
    notice_type: str | None,
    likely_notice_type: str | None,
    *,
    xor_groups: list[list[str]] | None = None,
) -> dict[str, Any]:
    """``notice_required_fields`` dict 를 만든다.

    구조(F4 — XOR 그룹 분리)::

        {
            "certain": [ {"field","label"}, ... ],          # XOR 그룹에 속하지 않은 교집합
            "certain_one_of": [ [ {"field","label"}, ... ], ... ], # 각 XOR 그룹
            "likely_type": str | None,
            "likely_extra": [ {"field","label"}, ... ],      # likely_type - (certain + XOR)
            "likely_extra_one_of": [ [ ... ], ... ],
        }

    - ``certain`` 은 후보 고시타입 전체의 필수필드 **교집합** 중 **XOR 그룹에
      속하지 않은** 필드. 안전하다.
    - ``certain_one_of`` 는 교집합에 걸친 XOR 그룹을, 교집합 원본 필드로만
      제한한 것. 각 그룹에서 **정확히 하나만** 채워야 한다. XOR 그룹의 필드가
      해당 목록(교집합 원본)에 **하나라도** 있으면 그 그룹은 ``*_one_of`` 로
      옮긴다(목록에 있는 멤버만 담는다).
    - ``likely_extra`` / ``likely_extra_one_of`` 는 추정(likely_notice_type) 분.
      **추정 표시**다.
    - 후보가 없으면 전부 빈 값.

    **F7 — 해석 실패/빈 결과는 빈 집합으로 취급**: ``_notice_type_fields_for(nt)``
    가 실패하거나 빈 결과를 주면 그 타입은 **빈 튜플** 로 교집합에 참여시킨다
    (→ 교집합이 비게 된다). "모르면 아무것도 확실하지 않다" 가 맞는 답이다.
    """
    empty_block: dict[str, Any] = {
        "certain": [],
        "certain_one_of": [],
        "likely_type": None,
        "likely_extra": [],
        "likely_extra_one_of": [],
    }
    if not candidates or not notice_types:
        return empty_block

    # F7: 각 후보의 고시타입별 필드 목록을 가져온다 — 빈/실패도 **빈 튜플** 로 참여.
    type_field_lists: list[tuple[str, ...]] = []
    for nt in notice_types:
        try:
            fields = _listing_templates._notice_type_fields_for(nt)
        except Exception:
            fields = ()
        # F7: fields 가 빈 튜플이어도 **그대로** 추가한다 (과거에는 skip 했다).
        type_field_lists.append(fields)

    # 교집합 (certain 원본)
    intersection_fields = _intersect_field_lists(type_field_lists)
    intersection_set = set(intersection_fields)

    # XOR 그룹 처리 (F4).
    if xor_groups is None:
        xor_groups = _xor_groups_for_types(notice_types)
    certain_one_of_fields: list[list[str]] = []
    xor_member_set: set[str] = set()
    for group in xor_groups:
        if not isinstance(group, list):
            continue
        # XOR 그룹의 필드가 교집합에 **하나라도** 있으면 그 그룹을 *_one_of 로.
        in_intersection = [f for f in group if f in intersection_set]
        if in_intersection:
            certain_one_of_fields.append(in_intersection)
            for f in in_intersection:
                xor_member_set.add(f)
    # certain 은 교집합에서 XOR 그룹 멤버를 뺀 나머지.
    certain_fields = tuple(f for f in intersection_fields if f not in xor_member_set)
    certain = _fields_with_labels(certain_fields, notice_type or likely_notice_type)
    certain_one_of = [
        _fields_with_labels(group, notice_type or likely_notice_type)
        for group in certain_one_of_fields
    ]

    # likely_extra: likely_notice_type 이 있으면 그 타입 전체에서 certain + XOR 를 뺀 나머지.
    likely_extra: list[dict[str, str]] = []
    likely_extra_one_of: list[list[dict[str, str]]] = []
    if likely_notice_type:
        try:
            likely_all = _listing_templates._notice_type_fields_for(likely_notice_type)
        except Exception:
            likely_all = ()
        likely_all_set = set(likely_all)
        # likely 분의 XOR 그룹 처리: likely 타입의 XOR 그룹 중 교집합에 안 들어간 것.
        likely_xor_groups = _xor_groups_for_types([likely_notice_type])
        likely_xor_member_set: set[str] = set()
        for group in likely_xor_groups:
            if not isinstance(group, list):
                continue
            # likely 분 XOR 그룹이 이미 certain_one_of 에 들어있으면 스킵.
            group_key = frozenset(group)
            already_in_certain = any(frozenset(g) == group_key for g in certain_one_of_fields)
            if already_in_certain:
                continue
            in_likely = [f for f in group if f in likely_all_set and f not in intersection_set]
            if in_likely:
                likely_extra_one_of.append(_fields_with_labels(in_likely, likely_notice_type))
                for f in in_likely:
                    likely_xor_member_set.add(f)
        # certain 원본(certain_fields + certain_one_of flatten) 을 뺀 나머지.
        already_required = set(certain_fields) | xor_member_set
        extra_fields = tuple(
            f for f in likely_all if f not in already_required and f not in likely_xor_member_set
        )
        likely_extra = _fields_with_labels(extra_fields, likely_notice_type)

    return {
        "certain": certain,
        "certain_one_of": certain_one_of,
        "likely_type": likely_notice_type,
        "likely_extra": likely_extra,
        "likely_extra_one_of": likely_extra_one_of,
    }


def diagnose(product: dict[str, Any]) -> dict[str, Any]:
    """거부 시점에 알 수 있는 필요사항을 한 번에 진단한다.

    **순수 함수** — 네트워크·LLM·파일쓰기 0회. 읽기 전용 데이터만 쓴다.

    Args:
        product: 상품 입력 dict. ``name``/``title_ko``/``salePrice``/
            ``image_sources`` 등의 키를 읽는다.

    Returns:
        진단 결과 dict::

            {
              "missing": [ {"field": str, "label": str, "why": str,
                            "answer_shape": str}, ... ],
              "category": {
                  "status": "confident" | "likely" | "ambiguous" | "unknown",
                  "candidates": [ {"category_id": str, "path": str,
                                   "score": int}, ...],  # 상위 3개까지(표시용)
                  "needs_category_choice": bool,  # classify_category 가 dict 였으면 True
                                                    # → candidates 를 사용자에게 보여 고르게 하라.
                  "notice_type": str | None,        # status=="confident" 일 때만
                  "likely_notice_type": str | None, # 최고점 후보들이 같을 때 (추정)
                  "notice_types_seen": [str, ...],  # 후보들에서 나온 전부
              },
              "notice_required_fields": {
                  "certain": [ {"field": str, "label": str}, ... ],  # 교집합(XOR 제외)
                  "certain_one_of": [ [ {"field","label"}, ... ], ... ], # XOR 그룹
                  "likely_type": str | None,
                  "likely_extra": [ {"field": str, "label": str}, ... ],
                  "likely_extra_one_of": [ [ {"field","label"}, ... ], ... ],
              },
              "images": {"min_required": 1, "provided": int, "note": str},
            }

    안내:
      - ``category.needs_category_choice`` 가 True 면 카테고리를 임의로 정하지 말고
        ``candidates`` 를 사용자에게 보여 고르게 하라(F2).
      - ``notice_required_fields.certain`` 은 어느 쪽으로 확정되든 반드시 필요한
        고시 항목이다(후보 고시타입들의 교집합).
      - ``notice_required_fields.certain_one_of`` 의 각 그룹에서 **정확히 하나만**
        채워야 한다(F4 — 상호배제). 둘 다 채우면 네이버가 거절한다.
      - ``notice_required_fields.likely_*`` 은 추정이다. ``likely_type`` 이 맞을 때만
        필요하다 — 물을 때 추정임을 밝혀라.
    """
    if not isinstance(product, dict):
        product = {}

    # --- missing: 지금 없는 것 전부 ---
    missing = _build_missing_items(product)

    # --- F3: 입력 우선순위 ---
    # 1. 사용자가 명시한 notice.productInfoProvidedNoticeType / notice_type
    explicit_notice_type = _explicit_notice_type_from_input(product)
    # 2. categoryId / category_id 로부터의 타입
    cid_notice_type = _explicit_notice_type_from_category_id(product)

    # --- category 진단 ---
    name = str(product.get("name") or product.get("title_ko") or "").strip()

    # 기본값(후보 없음/이름 없음) — needs_category_choice 도 False 다.
    if not name:
        category_block: dict[str, Any] = {
            "status": "unknown",
            "candidates": [],
            "needs_category_choice": False,
            "notice_type": None,
            "likely_notice_type": None,
            "notice_types_seen": [],
        }
    else:
        # F1: 판정에는 **전체 후보** 를 쓴다. 표시용 candidates 만 상위 3개로 줄인다.
        all_candidates, needs_category_choice = _candidates_from_title(name)
        # F1: 최고점 동점자 전부를 판정에 쓴다.
        notice_types = _notice_types_for_candidates(all_candidates)
        unique_types: list[str] = []
        for nt in notice_types:
            if nt not in unique_types:
                unique_types.append(nt)

        if not all_candidates:
            category_block = {
                "status": "unknown",
                "candidates": [],
                "needs_category_choice": False,
                "notice_type": None,
                "likely_notice_type": None,
                "notice_types_seen": [],
            }
        elif len(unique_types) == 1:
            # 모든 후보의 고시타입이 같다 → confident.
            nt_val = unique_types[0]
            category_block = {
                "status": "confident",
                "candidates": all_candidates[:3],  # 표시용 상위 3
                "needs_category_choice": needs_category_choice,
                "notice_type": nt_val,
                "likely_notice_type": nt_val,
                "notice_types_seen": unique_types,
            }
        else:
            # 최고점(score 최댓값) 동점자 전부를 판정에 쓴다(F1).
            max_score = max(c.get("score", 0) for c in all_candidates)
            top_types: list[str] = []
            for c, nt in zip(all_candidates, notice_types, strict=True):
                if c.get("score", 0) == max_score:
                    if nt not in top_types:
                        top_types.append(nt)

            if len(top_types) == 1:
                # 최고점 동점자끼리는 같다 → likely (추정).
                likely_nt = top_types[0]
                category_block = {
                    "status": "likely",
                    "candidates": all_candidates[:3],  # 표시용 상위 3
                    "needs_category_choice": needs_category_choice,
                    "notice_type": None,
                    "likely_notice_type": likely_nt,
                    "notice_types_seen": unique_types,
                }
            else:
                # 최고점 동점자끼리도 갈린다 → ambiguous.
                category_block = {
                    "status": "ambiguous",
                    "candidates": all_candidates[:3],  # 표시용 상위 3
                    "needs_category_choice": needs_category_choice,
                    "notice_type": None,
                    "likely_notice_type": None,
                    "notice_types_seen": unique_types,
                }

    # --- F3: 사용자가 명시한 고시타입/categoryId 가 있으면 그것으로 확정한다 ---
    # 우선순위 1: notice.productInfoProvidedNoticeType / notice_type
    # 우선순위 2: categoryId / category_id
    if explicit_notice_type is not None:
        category_block["status"] = "confident"
        category_block["notice_type"] = explicit_notice_type
        category_block["likely_notice_type"] = explicit_notice_type
        category_block["needs_category_choice"] = False
        if explicit_notice_type not in (category_block.get("notice_types_seen") or []):
            category_block["notice_types_seen"] = list(
                (category_block.get("notice_types_seen") or []) + [explicit_notice_type]
            )
    elif cid_notice_type is not None:
        category_block["status"] = "confident"
        category_block["notice_type"] = cid_notice_type
        category_block["likely_notice_type"] = cid_notice_type
        category_block["needs_category_choice"] = False
        if cid_notice_type not in (category_block.get("notice_types_seen") or []):
            category_block["notice_types_seen"] = list(
                (category_block.get("notice_types_seen") or []) + [cid_notice_type]
            )

    # --- notice_required_fields ---
    notice_type = category_block.get("notice_type")
    likely_notice_type = category_block.get("likely_notice_type")

    # F1/F3: 판정에 쓸 후보/타입은 **최고점 동점자 전부** 또는 확정된 단일 타입.
    # F3 로 단일 타입이 확정됐을 때는 그 타입만으로 필드를 산출한다.
    if explicit_notice_type is not None or cid_notice_type is not None:
        # 확정된 단일 타입 — candidates 는 비어있어도 상관없이 타입으로 산출.
        nt_for_fields = explicit_notice_type or cid_notice_type
        # 단일 타입의 필드를 certain 의 원본으로 쓴다(교집합 = 자기 자신).
        candidates_for_fields = [{"category_id": "", "path": "", "score": 0}]
        notice_types_for_fields = [str(nt_for_fields)]
    elif not name or not all_candidates:
        # 이름이 없거나 후보가 없음 — 빈 값.
        candidates_for_fields = []
        notice_types_for_fields = []
    else:
        # F1: 상품명 분류 경로 — **최고점 동점자 전부** 의 고시타입을 쓴다.
        # (전체 후보가 아니다. F1 본문: "판정에는 동점자 전부를 쓴다.")
        max_score = max(c.get("score", 0) for c in all_candidates)
        candidates_for_fields = [c for c in all_candidates if c.get("score", 0) == max_score]
        # 동점자들의 고시타입을 다시 구한다(순서 유지).
        notice_types_for_fields = _notice_types_for_candidates(candidates_for_fields)

    notice_required_fields = _build_notice_required_fields_block(
        candidates_for_fields,
        notice_types_for_fields,
        notice_type,
        likely_notice_type,
    )

    # --- images (F5) ---
    # 유효한(공백 아닌 문자열) 이미지 수로 센다.
    image_sources = product.get("image_sources")
    provided = _valid_image_count(image_sources)
    images_block = {
        "min_required": 1,
        "provided": provided,
        "note": (
            "실재하는 상품의 사진이 최소 1장 필요합니다."
            if provided == 0
            else f"이미지 {provided}장 제공됨."
        ),
    }

    return {
        "missing": missing,
        "category": category_block,
        "notice_required_fields": notice_required_fields,
        "images": images_block,
    }


__all__ = [
    "_candidates_from_title",
    "_explicit_notice_type_from_category_id",
    "_explicit_notice_type_from_input",
    "_intersect_field_lists",
    "_top_candidates",
    "_valid_image_count",
    "_xor_groups_for_types",
    "diagnose",
]
