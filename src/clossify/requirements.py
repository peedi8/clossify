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


def _top_candidates(title_ko: str, limit: int = 3) -> list[dict[str, Any]]:
    """상위 카테고리 후보를 ``limit`` 개까지 반환.

    ``classify_category`` 의 결과에서 후보를 추출한다:
      - 강한 단일 후보(확정 id) 인 경우 → 해당 카테고리를 단일 후보로.
      - LLM 위임(ambiguous) 인 경우 → input.candidates 상위 ``limit`` 개.
      - 후보 없음 → 빈 리스트.

    Returns:
        ``[{"category_id": str, "path": str, "score": int}, ...]``
    """
    if not title_ko or not str(title_ko).strip():
        return []
    try:
        result = _category.classify_category(str(title_ko), {}, "")
    except Exception:
        return []
    if isinstance(result, str):
        # 강한 단일 후보 확정 — category_path 로 경로를 가져온다.
        cat_id = result
        try:
            path = _category.category_path(cat_id) or ""
        except Exception:
            path = ""
        return [{"category_id": cat_id, "path": path, "score": 0}]
    if isinstance(result, dict) and result.get("needs_llm"):
        raw_candidates = (
            result.get("input", {}).get("candidates")
            if isinstance(result.get("input"), dict)
            else None
        ) or []
        out: list[dict[str, Any]] = []
        for c in raw_candidates[:limit]:
            if not isinstance(c, dict):
                continue
            out.append(
                {
                    "category_id": str(c.get("category_id") or ""),
                    "path": str(c.get("path") or ""),
                    "score": int(c.get("score") or 0),
                }
            )
        return out
    return []


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

    image_sources = product.get("image_sources")
    if not isinstance(image_sources, list) or not image_sources:
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
) -> dict[str, Any]:
    """``notice_required_fields`` dict 를 만든다.

    구조::

        {
            "certain": [ {"field","label"}, ... ],   # 후보 전체의 교집합
            "likely_type": str | None,               # likely_notice_type 과 같음
            "likely_extra": [ {"field","label"}, ... ],  # likely_type 전체 - certain
        }

    - ``certain`` 은 후보 고시타입 전체의 필수필드 **교집합**. 안전하다.
    - ``likely_extra`` 는 추정(likely_notice_type) 분. **추정 표시**다.
    - 후보가 없으면 셋 다 빈 값.
    """
    if not candidates or not notice_types:
        return {"certain": [], "likely_type": None, "likely_extra": []}

    # 각 후보의 고시타입별 필드 목록을 가져온다.
    type_field_lists: list[tuple[str, ...]] = []
    for nt in notice_types:
        try:
            fields = _listing_templates._notice_type_fields_for(nt)
        except Exception:
            fields = ()
        if fields:
            type_field_lists.append(fields)

    # 교집합 (certain)
    certain_fields = _intersect_field_lists(type_field_lists)
    certain = _fields_with_labels(certain_fields, notice_type or likely_notice_type)

    # likely_extra: likely_notice_type 이 있으면 그 타입 전체에서 certain 을 뺀 나머지.
    likely_extra: list[dict[str, str]] = []
    if likely_notice_type:
        try:
            likely_all = _listing_templates._notice_type_fields_for(likely_notice_type)
        except Exception:
            likely_all = ()
        certain_set = set(certain_fields)
        extra_fields = tuple(f for f in likely_all if f not in certain_set)
        likely_extra = _fields_with_labels(extra_fields, likely_notice_type)

    return {
        "certain": certain,
        "likely_type": likely_notice_type,
        "likely_extra": likely_extra,
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
                                   "score": int}, ...],  # 상위 3개까지
                  "notice_type": str | None,        # status=="confident" 일 때만
                  "likely_notice_type": str | None, # 최고점 후보들이 같을 때 (추정)
                  "notice_types_seen": [str, ...],  # 후보들에서 나온 전부
              },
              "notice_required_fields": {
                  "certain": [ {"field": str, "label": str}, ... ],  # 교집합
                  "likely_type": str | None,
                  "likely_extra": [ {"field": str, "label": str}, ... ],
              },
              "images": {"min_required": 1, "provided": int, "note": str},
            }
    """
    if not isinstance(product, dict):
        product = {}

    # --- missing: 지금 없는 것 전부 ---
    missing = _build_missing_items(product)

    # --- category 진단 ---
    name = str(product.get("name") or product.get("title_ko") or "").strip()

    if not name:
        category_block: dict[str, Any] = {
            "status": "unknown",
            "candidates": [],
            "notice_type": None,
            "likely_notice_type": None,
            "notice_types_seen": [],
        }
    else:
        candidates = _top_candidates(name, limit=3)
        notice_types = _notice_types_for_candidates(candidates)
        unique_types: list[str] = []
        for nt in notice_types:
            if nt not in unique_types:
                unique_types.append(nt)

        if not candidates:
            category_block = {
                "status": "unknown",
                "candidates": [],
                "notice_type": None,
                "likely_notice_type": None,
                "notice_types_seen": [],
            }
        elif len(unique_types) == 1:
            # 상위 3후보의 고시타입이 전부 같다 → confident.
            nt_val = unique_types[0]
            category_block = {
                "status": "confident",
                "candidates": candidates,
                "notice_type": nt_val,
                "likely_notice_type": nt_val,
                "notice_types_seen": unique_types,
            }
        else:
            # 최고점(score 최댓값) 후보들이 서로 같은 타입인지 확인.
            max_score = max(c.get("score", 0) for c in candidates)
            top_types: list[str] = []
            for c, nt in zip(candidates, notice_types, strict=True):
                if c.get("score", 0) == max_score:
                    if nt not in top_types:
                        top_types.append(nt)

            if len(top_types) == 1:
                # 최고점 후보들끼리는 같다 → likely (추정).
                likely_nt = top_types[0]
                category_block = {
                    "status": "likely",
                    "candidates": candidates,
                    "notice_type": None,
                    "likely_notice_type": likely_nt,
                    "notice_types_seen": unique_types,
                }
            else:
                # 최고점 후보들끼리도 갈린다 → ambiguous.
                category_block = {
                    "status": "ambiguous",
                    "candidates": candidates,
                    "notice_type": None,
                    "likely_notice_type": None,
                    "notice_types_seen": unique_types,
                }

    # --- notice_required_fields ---
    notice_type = category_block.get("notice_type")
    likely_notice_type = category_block.get("likely_notice_type")

    if not name:
        # 후보가 없음 — 셋 다 빈 값.
        notice_required_fields: dict[str, Any] = {
            "certain": [],
            "likely_type": None,
            "likely_extra": [],
        }
    else:
        candidates_for_fields = category_block.get("candidates") or []
        notice_types_for_fields = _notice_types_for_candidates(candidates_for_fields)
        notice_required_fields = _build_notice_required_fields_block(
            candidates_for_fields,
            notice_types_for_fields,
            notice_type,
            likely_notice_type,
        )

    # --- images ---
    image_sources = product.get("image_sources")
    provided = len(image_sources) if isinstance(image_sources, list) else 0
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


__all__ = ["diagnose"]
