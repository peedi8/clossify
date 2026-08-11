# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""거부 시점 진단 — "첫 결핍"이 아니라 "필요한 전부"를 돌려준다.

``prepare_listing`` 이 거부할 때, 그 시점에 **오프라인으로** 알아낼 수 있는
필요사항을 한 번에 모아 돌려준다. 호출자가 한 턴에 전부 물어볼 수 있게 한다.

.. warning::

    이 모듈의 **상품명→카테고리 추론은 거부 시점 안내 전용**이다.
    **등록 경로·카테고리 확정에 쓰지 마라.**
    운영 계약(``naver-seo-rules.md`` 43행):
    *"카테고리 먼저 확정 → 제목 생성. 제목에서 카테고리 역추론 금지."*

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
      - 강한 단일 후보인 경우에도 해당 카테고리는 상품명에서 유추한 후보일 뿐이다.
        ``needs_category_choice = True`` 로 두고 사용자가 확정하게 한다.
      - LLM 위임(ambiguous) 인 경우 → input.candidates **전체**(자르지 않음).
        ``needs_category_choice = True`` (호출자가 사용자에게 골라야 함).
      - 후보 없음 → 빈 리스트.

    Returns:
        ``(candidates, needs_category_choice)``.
        ``candidates``: ``[{"category_id": str, "path": str, "score": int}, ...]``.
        ``needs_category_choice``: 비어 있지 않은 상품명 기반 후보면 항상 ``True``.
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
        return [{"category_id": cat_id, "path": path, "score": 0}], True
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


def _build_candidates_by_notice_type(
    candidates: list[dict[str, Any]],
    notice_types: list[str],
) -> dict[str, dict[str, Any]]:
    """최고점 동점자 전부를 고시타입별로 묶는다 (M1).

    각 그룹에:
      - ``count``: 그 타입의 동점 후보 총수
      - ``examples``: 점수순 최대 3개 (``{"category_id","path","score"}``)

    **모든 타입이 반드시 포함**돼야 한다 — 상한은 타입 안에서만 건다.
    """
    result: dict[str, dict[str, Any]] = {}
    for cidx, cand in enumerate(candidates):
        nt = notice_types[cidx] if cidx < len(notice_types) else ""
        if not nt:
            continue
        if nt not in result:
            result[nt] = {"count": 0, "examples": []}
        result[nt]["count"] += 1
        if len(result[nt]["examples"]) < 3 and isinstance(cand, dict):
            result[nt]["examples"].append(
                {
                    "category_id": str(cand.get("category_id") or ""),
                    "path": str(cand.get("path") or ""),
                    "score": int(cand.get("score") or 0),
                }
            )
    return result


def _candidates_with_all_types(
    candidates: list[dict[str, Any]],
    notice_types: list[str],
) -> list[dict[str, Any]]:
    """표시용 candidates 목록에서 **각 타입의 대표 1개 이상**이 보이게 한다 (M1).

    점수순으로만 잘라 특정 타입이 통째로 사라지는 지금 동작을 고친다.
    각 고시타입의 첫 후보를 우선 배치하고, 나머지를 뒤에 붙인다.
    """
    seen_types: set[str] = set()
    representatives: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for cidx, cand in enumerate(candidates):
        nt = notice_types[cidx] if cidx < len(notice_types) else ""
        if nt and nt not in seen_types and isinstance(cand, dict):
            seen_types.add(nt)
            representatives.append(cand)
        elif isinstance(cand, dict):
            rest.append(cand)
    return representatives + rest


def _build_notice_required_fields_block(
    candidates: list[dict[str, Any]],
    notice_types: list[str],
    notice_type: str | None,
    likely_notice_type: str | None,
    *,
    xor_groups: list[list[str]] | None = None,
    is_explicit_confirmed: bool = False,
) -> dict[str, Any]:
    """``notice_required_fields`` dict 를 만든다.

    새로운 구조(F4 — XOR 그룹 분리 + scope 추적)::

        {
            "certain": [ {"field","label"}, ... ],          # 모든 타입 교집합(XOR 제외)
            "certain_one_of": [ [ {"field","label"}, ... ], ... ], # 공통 XOR 그룹
            "likely_type": str | None,
            "likely_extra": [ {"field","label"}, ... ],      # likely_type - (certain + XOR)
            "likely_extra_one_of": [ [ ... ], ... ],
            # ── 신규 키 ──
            "scope": "confirmed_category" | "top_tied_candidates" | "unknown",
            "is_complete": bool,                  # true → 모든 것 알음
            "completion_blocked_by": [str, ...],  # incomplete 일 때 미해결 reason
            "candidate_groups": [                 # notice_type별 candidate 분류 (리스트)
                {
                    "notice_type": str,
                    "candidates": [...],            # title-derived: matching top-tied dicts
                    "additional": [{"field","label"},...],
                    "additional_one_of": [[{"field","label"},...],...],
                }, ...
            ],
            "unresolved_notice_types": [str, ...],  # first-seen order
        }

    새 파라미터:
        ``is_explicit_confirmed``: 사용자가 명시한 고시타입 / categoryId 가
        존재하면 ``True`` (diagnose 에서 계산).

    key 설명:
      - **scope**: 카테고리 확정 상태. 세 값 중 하나:
        * ``confirmed_category`` — ``is_explicit_confirmed=True``, 사용자가
          고시타입이나 categoryId 로 명확히 지정함.
        * ``top_tied_candidates`` — 상품명 기반 분류로 후보가 나왔으나 아직
          선택되지 않음.
        * ``unknown`` — 입력이 비거나 모든 타입이 불분명(unresolved).
      - **is_complete**: true 면 확실히 모든 것을 안다(정식 카테고리 선택 전
       이라도 ``confirmed_category`` 는 완전). false 면 ``completion_blocked_by``
        를 참고.
      - **completion_blocked_by**: incomplete 원인 목록. ``category_choice``,
        ``unknown_notice_type`` 등.
      - **candidate_groups**: ``notice_types_for_fields`` 의 첫 등장 순서대로
        정리된 notice_type 별 candidate 그룹 리스트. 각 그룹의 ``additional`` 은
        해당 타입 regular fields - common certain,
        ``additional_one_of`` 은 해당 타입 XOR groups - commonCertainOneOf.
        title 유추 candidate 군은 사용자 선택을 필요로 함.
        명시 확인된 경우만 confirmed_category/complete.
        각 그룹 객체는 정확히 다음 semantic key 만 갖는다:
        ``notice_type``, ``candidates``, ``additional``, ``additional_one_of``.
        (per-group ``scope``, ``is_complete``, ``completion_blocked_by``,
        ``label`` 은 쓰지 않음.)
      - **unresolved_notice_types**: full fields 로드 실패 또는 빈 결과인
        고시타입 목록. 이 목록이 비어 있으면 최소 하나의 타입은 성공했음을 뜻함.

    동작 요약:
      1. empty input → unknown scope, is_complete=False, blockers/groups/unresolved=[]
      2. per type: full fields 와 해당 type 의 XOR 그룹 로드.
         regular fields = full - 해당 type 만의 XOR 멤버.
      3. any unresolved type → uncertain/certain_one_of 공백, is_complete=False,
         ["unknown_notice_type"] blocker. candidate_groups 는 empty additions 로 emit.
      4. common certain = per-type regular fields 교집합(첫 type 순서 유지).
      5. common certain_one_of = every type 의 full fields 에 모두 속하고
         every type 에 나타나는 XOR 그룹; 첫 type 표준 순서로 emit.
      6. candidate_groups = first-seen distinct notice type order. actual first index 사용.
      7. explicit confirmed → candidates=[], title-derived → matching top-tied dicts.
      8. known type 에 대해 combined(regular+XOR) ⊆ type full fields 검증(문자열 비교).
      9. likely_extra/mirroring = 매칭 candidate group 의 additions (estimate).
      10. scope/completion = confirmed_category→true/top_tied_candidates→false/
          unknown→false.

    F7 — 해석 실패/빈 결과는 빈 집합으로 취급: ``_notice_type_fields_for(nt)``
    가 실패하거나 빈 결과를 주면 그 타입은 **불분명(unresolved)** 처리된다
    ("모르면 아무것도 확실하지 않다").
    """
    # --- Empty / universal_only scope → M2 ---
    if not candidates or not notice_types:
        return {
            "certain": [],
            "certain_one_of": [],
            "likely_type": None,
            "likely_extra": [],
            "likely_extra_one_of": [],
            "scope": "universal_only",
            "complete": False,
            "is_complete": False,  # backward compat
            "completion_blocked_by": [],
            "candidate_groups": [],
            "by_notice_type": {},  # M3
            "unresolved_notice_types": [],
        }

    # === Requirement 4: per-type full fields + XOR groups ===
    type_full_ordered: list[tuple[str, ...]] = []
    type_xor_groups_raw: list[list[list[str]]] = []
    unresolved_list: list[str] = []  # requirement 5: first-seen order
    unresolved_set: set[str] = set()  # fast lookup only

    for nt in notice_types:
        try:
            fields = _listing_templates._notice_type_fields_for(nt)
        except Exception:
            fields = ()
        if not fields and nt not in unresolved_set:
            unresolved_list.append(nt)
            unresolved_set.add(nt)
        type_full_ordered.append(tuple(fields))
        try:
            xors = _qa_agents._notice_xor_groups(nt)
        except Exception:
            xors = []
        type_xor_groups_raw.append(list(xors) if isinstance(xors, list) else [])

    # === Requirement 1: per-type regular fields ===
    type_regular_ordered: list[tuple[str, ...]] = []
    for idx in range(len(notice_types)):
        xor_members_of_nt: set[str] = set()
        for grp in type_xor_groups_raw[idx]:
            xor_members_of_nt.update(grp)
        reg = tuple(f for f in type_full_ordered[idx] if f not in xor_members_of_nt)
        type_regular_ordered.append(reg)

    # === Requirement 3: any unresolved type? ===
    if unresolved_set:
        # Emit candidate groups with empty additions for each type
        groups: list[dict[str, Any]] = []
        group_index_by_type: dict[str, int] = {}
        for cidx in range(len(candidates)):
            nt = notice_types[cidx] if cidx < len(notice_types) else ""
            if not nt:
                continue
            if nt not in group_index_by_type:
                group_index_by_type[nt] = len(groups)
                groups.append(
                    {
                        "notice_type": nt,
                        "candidates": [],
                        "additional": [],
                        "additional_one_of": [],
                    }
                )
            if not is_explicit_confirmed and isinstance(candidates[cidx], dict):
                groups[group_index_by_type[nt]]["candidates"].append(candidates[cidx])
        unresolved_scope = "confirmed_category" if is_explicit_confirmed else "top_tied_candidates"
        unresolved_blockers = [] if is_explicit_confirmed else ["category_choice"]
        unresolved_blockers.append("unknown_notice_type")
        return {
            "certain": [],
            "certain_one_of": [],
            "likely_type": likely_notice_type,
            "likely_extra": [],
            "likely_extra_one_of": [],
            "scope": unresolved_scope,
            "complete": False,
            "is_complete": False,  # backward compat
            "completion_blocked_by": unresolved_blockers,
            "candidate_groups": groups,
            "by_notice_type": {},  # M3 — unresolved types
            "unresolved_notice_types": unresolved_list,
        }

    # === Requirement 4 (continued): build first-seen distinct groups and populate candidates ===
    # Track which type index each distinct notice_type maps to (group index within candidate_groups)
    type_to_group_idx: dict[str, int] = {}
    type_to_first_data_idx: dict[str, int] = {}
    first_seen_order: list[str] = []  # distinct nt in first-seen order
    groups: list[dict[str, Any]] = []  # candidate_groups as list

    for cidx, cand in enumerate(candidates):
        nt = notice_types[cidx] if cidx < len(notice_types) else ""
        if not nt:
            continue
        if nt not in type_to_group_idx:
            type_to_group_idx[nt] = len(first_seen_order)
            type_to_first_data_idx[nt] = cidx
            first_seen_order.append(nt)
            groups.append(
                {
                    "notice_type": nt,
                    "candidates": [],
                    "additional": [],
                    "additional_one_of": [],
                }
            )
        # Append candidate to its group
        if not is_explicit_confirmed and cand and isinstance(cand, dict):
            groups[type_to_group_idx[nt]]["candidates"].append(cand)

    # === Requirement 7: common certain (intersection of per-type regulars) ===
    intersection_fields = _intersect_field_lists(type_regular_ordered)
    intersection_set = set(intersection_fields)

    # === Requirement 8: common certain_one_of ===
    common_xor_groups: list[list[str]] = []
    seen_group_keys: set[frozenset[str]] = set()
    for group in type_xor_groups_raw[0]:
        if not isinstance(group, list) or len(group) < 2:
            continue
        gkey = frozenset(group)
        if gkey in seen_group_keys:
            continue
        # Every type must have this XOR group AND all its members in that type's full fields
        if all(
            gkey in [frozenset(g) for g in type_xor_groups_raw[i]]
            and frozenset(group).issubset(set(tfo))
            for i, tfo in enumerate(type_full_ordered)
        ):
            seen_group_keys.add(gkey)
            common_xor_groups.append(list(group))
    common_xor_member_set: set[str] = set()
    for cg in common_xor_groups:
        common_xor_member_set.update(cg)

    # Populate additional / additional_one_of per group
    for gi, nt in enumerate(first_seen_order):
        ti = type_to_first_data_idx[nt]
        add_fields_ordered = tuple(f for f in type_regular_ordered[ti] if f not in intersection_set)
        groups[gi]["additional"] = _fields_with_labels(add_fields_ordered, nt)
        # additional_one_of = this type's XOR groups minus common XOR groups
        remaining_xors: list[list[str]] = []
        for grp in type_xor_groups_raw[ti]:
            if not isinstance(grp, list) or len(grp) < 2:
                continue
            gkey = frozenset(grp)
            if gkey in seen_group_keys:
                continue
            if frozenset(grp).intersection(set(type_full_ordered[ti])):
                remaining_xors.append(grp)
        groups[gi]["additional_one_of"] = [
            _fields_with_labels(tuple(g), nt) for g in remaining_xors
        ]

    # === Requirement 11: reconstruction check (audit only — string compare) ===
    for gi, nt in enumerate(first_seen_order):
        ti = type_to_first_data_idx[nt]
        combined_reg: set[str] = set(intersection_fields)
        for item in groups[gi]["additional"]:
            if isinstance(item, dict):
                combined_reg.add(item["field"])
        combined_xor: set[str] = set()
        for og in common_xor_groups:
            combined_xor.update(og)
        for ag in groups[gi]["additional_one_of"]:
            for item in ag:
                if isinstance(item, dict):
                    combined_xor.add(item["field"])
        expected_full: set[str] = set(type_full_ordered[ti])
        assert combined_reg.isdisjoint(combined_xor), f"Regular-XOR overlap for {nt}"
        assert (
            combined_reg.union(combined_xor) == expected_full
        ), f"Reconstruction mismatch for {nt}: has={combined_reg.union(combined_xor)}, exp={expected_full}"

    # === Requirement 12: overall scope/completion ===
    if is_explicit_confirmed:
        overall_scope = "confirmed_category"
        overall_complete = True
        overall_blocked: list[str] = []
    else:
        overall_scope = "top_tied_candidates"
        overall_complete = False
        overall_blocked = ["category_choice"]

    # === M3: by_notice_type — 타입별 추가 요구 (extra / one_of) ===
    # 각 타입의 additional(= extra) 과 additional_one_of(= one_of) 를 dict 로 옮긴다.
    # 이것이 likely_type/likely_extra 를 대체한다 (M3).
    by_notice_type: dict[str, dict[str, Any]] = {}
    for group in groups:
        nt = group["notice_type"]
        by_notice_type[nt] = {
            "extra": list(group["additional"]),
            "one_of": [list(clause) for clause in group["additional_one_of"]],
        }

    likely_extra: list[dict[str, str]] = []
    likely_extra_one_of: list[list[dict[str, str]]] = []
    for group in groups:
        if group["notice_type"] == likely_notice_type:
            likely_extra = list(group["additional"])
            likely_extra_one_of = [list(clause) for clause in group["additional_one_of"]]
            break

    # === Build final result ===
    label_type = (
        notice_type
        if notice_type
        else likely_notice_type
        if likely_notice_type
        else (first_seen_order[0] if first_seen_order else None)
    )
    return {
        "certain": _fields_with_labels(intersection_fields, label_type),
        "certain_one_of": [_fields_with_labels(tuple(g), label_type) for g in common_xor_groups],
        "likely_type": likely_notice_type,
        "likely_extra": likely_extra,
        "likely_extra_one_of": likely_extra_one_of,
        "scope": overall_scope,
        "complete": overall_complete,
        "is_complete": overall_complete,  # backward compat
        "completion_blocked_by": overall_blocked,
        "candidate_groups": groups,
        "by_notice_type": by_notice_type,  # M3
        "unresolved_notice_types": unresolved_list,
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
                                   "score": int}, ...],  # 최고점 동점자 전부
                  "needs_category_choice": bool,
                  "notice_type": str | None,        # status=="confident" 일 때만
                  "likely_notice_type": str | None, # 최고점 후보들이 같을 때 (추정)
                  "notice_types_seen": [str, ...],  # 최고점 동점자에서 나온 전부
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
      - ``notice_required_fields.certain`` 은 후보군 전체의 공통 안전 부분집합일
        뿐이다. ``is_complete=false`` 면 완전한 요구목록이 아니므로 blocker와
        카테고리를 먼저 해결해야 한다.
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
            "candidates_by_notice_type": {},
            "needs_category_choice": False,
            "notice_type": None,
            "likely_notice_type": None,
            "notice_types_seen": [],
        }
    else:
        all_candidates, _ = _candidates_from_title(name)
        if not all_candidates:
            category_block = {
                "status": "unknown",
                "candidates": [],
                "candidates_by_notice_type": {},
                "needs_category_choice": False,
                "notice_type": None,
                "likely_notice_type": None,
                "notice_types_seen": [],
            }
        else:
            max_score = max(c.get("score", 0) for c in all_candidates)
            top_tied = [c for c in all_candidates if c.get("score", 0) == max_score]
            top_notice_types = _notice_types_for_candidates(top_tied)
            unique_top_types: list[str] = []
            for nt in top_notice_types:
                if nt not in unique_top_types:
                    unique_top_types.append(nt)
            if not unique_top_types:
                status = "unknown"
                likely_type = None
            elif len(unique_top_types) == 1:
                status = "likely"
                likely_type = unique_top_types[0]
            else:
                status = "ambiguous"
                likely_type = None
            # M1: 고시타입별 후보 묶기 + 각 타입 대표가 보이게 정렬
            cbnt = _build_candidates_by_notice_type(top_tied, top_notice_types)
            candidates_display = _candidates_with_all_types(top_tied, top_notice_types)
            category_block = {
                "status": status,
                "candidates": candidates_display,
                "candidates_by_notice_type": cbnt,
                "needs_category_choice": True,
                "notice_type": None,
                "likely_notice_type": likely_type,
                "notice_types_seen": unique_top_types,
            }

    # --- F3: 사용자가 명시한 고시타입/categoryId 가 있으면 그것으로 확정한다 ---
    # 우선순위 1: notice.productInfoProvidedNoticeType / notice_type
    # 우선순위 2: categoryId / category_id
    if explicit_notice_type is not None:
        category_block["status"] = "confident"
        category_block["notice_type"] = explicit_notice_type
        category_block["likely_notice_type"] = explicit_notice_type
        category_block["needs_category_choice"] = False
        category_block["candidates"] = []
        category_block["candidates_by_notice_type"] = {}
        category_block["notice_types_seen"] = [explicit_notice_type]
    elif cid_notice_type is not None:
        category_block["status"] = "confident"
        category_block["notice_type"] = cid_notice_type
        category_block["likely_notice_type"] = cid_notice_type
        category_block["needs_category_choice"] = False
        category_block["candidates"] = []
        category_block["candidates_by_notice_type"] = {}
        category_block["notice_types_seen"] = [cid_notice_type]

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
        is_explicit_confirmed=(explicit_notice_type is not None or cid_notice_type is not None),
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
    "_build_candidates_by_notice_type",
    "_candidates_from_title",
    "_candidates_with_all_types",
    "_explicit_notice_type_from_category_id",
    "_explicit_notice_type_from_input",
    "_intersect_field_lists",
    "_top_candidates",
    "_valid_image_count",
    "_xor_groups_for_types",
    "diagnose",
]
