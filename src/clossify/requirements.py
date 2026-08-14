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
from . import category_meta as _category_meta
from . import listing_templates as _listing_templates
from . import notice_labels as _notice_labels
from . import qa_agents as _qa_agents


def _candidates_from_title(title_ko: str) -> tuple[list[dict[str, Any]], bool]:
    """상품명 분류 결과로 (후보리스트, needs_category_choice) 를 반환.

    ``classify_category`` 의 결과에서 후보를 추출한다:
      - 강한 단일 후보인 경우에도 해당 카테고리는 상품명에서 유추한 후보일 뿐이다.
        ``needs_category_choice = True`` 로 두고 사용자가 확정하게 한다.
        확정 경로(str 반환)는 분류기가 점수를 주지 않으므로 ``score`` 를
        ``None`` 으로 둔다(분류기 점수 미제공 경로). 0 을 지어내지 않는다.
      - LLM 위임(ambiguous) 인 경우 → input.candidates **전체**(자르지 않음).
        ``needs_category_choice = True`` (호출자가 사용자에게 골라야 함).
      - 후보 없음 → 빈 리스트.

    Returns:
        ``(candidates, needs_category_choice)``.
        ``candidates``: ``[{"category_id": str, "path": str, "score": int | None}, ...]``.
        확정 경로(str) 에서는 ``score`` 가 ``None`` 이다(점수가 없다).
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
        # 분류기 점수 미제공 경로 — score 에 None 을 넣는다.
        # 0 을 지어내면 소비처가 "0점" 으로 오해한다.
        cat_id = result
        try:
            path = _category.category_path(cat_id) or ""
        except Exception:
            path = ""
        return [{"category_id": cat_id, "path": path, "score": None}], True
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

    라벨은 ``notice_labels._notice_field_label`` 을 재사용한다 (중복 구현 금지).
    계층 역전 해소: ``notice_labels`` (아래층) 에서 모듈 최상위 import 로
    읽는다. 이전의 ``mcp_server`` 역참조(함수 내부 import) 는 제거했다.
    """
    out: list[dict[str, str]] = []
    for field in field_names:
        try:
            label, _hint = _notice_labels._notice_field_label(field, notice_type)
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

    구조(F4 — XOR 그룹 분리 + scope 추적)::

        {
            "scope": "confirmed_category" | "top_tied_candidates" | "universal_only",
            "is_complete": bool,                  # true → 모든 것 알음
            "completion_blocked_by": [str, ...],  # incomplete 일 때 미해결 reason
            "certain": [ {"field","label"}, ... ],          # 모든 타입 교집합(XOR 제외)
            "certain_one_of": [ [ {"field","label"}, ... ], ... ], # 공통 XOR 그룹
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

    key 설명 (정확히 7개):
      - **scope**: 카테고리 확정 상태. 세 값 중 하나:
        * ``confirmed_category`` — ``is_explicit_confirmed=True``, 사용자가
          고시타입이나 categoryId 로 명확히 지정함.
        * ``top_tied_candidates`` — 상품명 기반 분류로 후보가 나왔으나 아직
          선택되지 않음.
        * ``universal_only`` — 입력이 비거나 모든 타입이 불분명(unresolved).
      - **is_complete**: true 면 확실히 모든 것을 안다(정식 카테고리 선택 전
        이라도 ``confirmed_category`` 는 완전). false 면 ``completion_blocked_by``
        를 참고. 카테고리 미확정이면 절대 True 가 아니다.
      - **completion_blocked_by**: incomplete 원인 목록. ``category_choice``,
        ``unknown_notice_type`` 등. 완료가능하면 빈 리스트.
      - **certain**: 범위 안에서 안전한 필드 ``[{"field","label"}]``.
        모든 타입 교집합(XOR 제외).
      - **certain_one_of**: 그중 XOR 그룹 ``[[{"field","label"}, ...], ...]``.
        공통 XOR 그룹.
      - **candidate_groups**: 타입별 추가 요구
        ``{타입: {"extra":[...], "one_of":[[...]]}}`` 형태로, 각 그룹의
        ``additional`` 은 해당 타입 regular fields - common certain,
        ``additional_one_of`` 은 해당 타입 XOR groups - commonCertainOneOf.
        title 유추 candidate 군은 사용자 선택을 필요로 함.
        명시 확인된 경우만 confirmed_category/complete.
        각 그룹 객체는 정확히 다음 semantic key 만 갖는다:
        ``notice_type``, ``candidates``, ``additional``, ``additional_one_of``.
        (per-group ``scope``, ``is_complete``, ``completion_blocked_by``,
        ``label`` 은 쓰지 않음.)
      - **unresolved_notice_types**: 필드를 못 구한 고시타입 목록.
        full fields 로드 실패 또는 빈 결과인 타입. first-seen order.
        이 목록이 비어 있으면 최소 하나의 타입은 성공했음을 뜻함.

    동작 요약:
      1. empty input → universal_only scope, is_complete=False,
         blockers/groups/unresolved=[].
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
      9. scope/completion = confirmed_category→true/top_tied_candidates→false/
          universal_only→false.

    F7 — 해석 실패/빈 결과는 빈 집합으로 취급: ``_notice_type_fields_for(nt)``
    가 실패하거나 빈 결과를 주면 그 타입은 **불분명(unresolved)** 처리된다
    ("모르면 아무것도 확실하지 않다").
    """
    # --- Empty / universal_only scope → M2 ---
    if not candidates or not notice_types:
        return {
            "scope": "universal_only",
            "is_complete": False,
            "completion_blocked_by": [],
            "certain": [],
            "certain_one_of": [],
            "candidate_groups": [],
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
            "scope": unresolved_scope,
            "is_complete": False,
            "completion_blocked_by": unresolved_blockers,
            "certain": [],
            "certain_one_of": [],
            "candidate_groups": groups,
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

    # === Build final result ===
    label_type = (
        notice_type
        if notice_type
        else likely_notice_type
        if likely_notice_type
        else (first_seen_order[0] if first_seen_order else None)
    )
    return {
        "scope": overall_scope,
        "is_complete": overall_complete,
        "completion_blocked_by": overall_blocked,
        "certain": _fields_with_labels(intersection_fields, label_type),
        "certain_one_of": [_fields_with_labels(tuple(g), label_type) for g in common_xor_groups],
        "candidate_groups": groups,
        "unresolved_notice_types": unresolved_list,
    }


def _category_id_known(cid: str) -> bool:
    """categoryId 가 로컬 메타에 존재하는지 확인 (F1).

    ``category_meta.category_path(cid, raise_if_unknown=False)`` 가 빈 문자열이면
    존재하지 않는 ID 이다. ``requires_kc`` 는 모르는 ID 에 대해 ``False`` 를
    반환하므로, 이 판정이 F1 의 핵심이다.
    """
    try:
        path = _category.category_path(cid) or ""
    except Exception:
        path = ""
    return bool(path)


# R1 — 조립기(naver_client)가 상품 입력에서 실제로 읽는 키, **정확히 그것만**.
# 넓혀도 좁혀도 왕복이 재발한다(wo-n76-round3.md 표 참조).
#   - 원산지 코드: ``_resolve_origin_area_code`` (naver_client.py:278 부근) 는
#     ``p.origin_code`` 만 읽는다. ``origin_area_code``/``originAreaCode`` 는
#     **config 측 키** 이므로 상품측 후보에서 제거.
#   - 원산지 표기: ``_notice_defaults`` made_in (naver_client.py:353 부근) 은
#     ``p.made_in``·``p.origin_content`` 만 읽는다. ``originContent`` 는 config
#     측 camelCase 별칭이므로 상품측 후보에서 제거.
#   - A/S 전화: ``_notice_defaults`` as_tel (naver_client.py:336~342) 은
#     ``p.as_tel``·``p.seller_tel`` 만 읽는다. ``customerServicePhoneNumber`` 는
#     **config 측** 키 이므로 상품측 후보에서 제거.
_ORIGIN_CODE_KEYS = ("origin_code",)
_ORIGIN_CONTENT_KEYS = ("made_in", "origin_content")
_AS_TEL_KEYS = ("as_tel", "seller_tel")


def _has_text_in(product: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """``product`` 의 후보 키 중 하나라도 **placeholder 가 아닌** 실질 값을 가지면 True.

    R5 — ``qa_agents._is_placeholder_value`` 의 단일 진실 공급원을 재사용한다.
    "상세페이지 참조" 같은 placeholder 를 "제공됨" 으로 세면 QA 가 나중에 거부한다.
    의존 방향: ``qa_agents`` → 본 모듈 (허용됨).
    """
    for key in keys:
        v = product.get(key)
        if isinstance(v, str) and v.strip() and not _qa_agents._is_placeholder_value(v):
            return True
        # 비-문자열 값(숫자 등)은 placeholder 판정을 건너뛰고 "값 있음" 으로 본다.
        if v is not None and not isinstance(v, str) and v != "":
            return True
    return False


def _build_compliance_block(
    product: dict[str, Any],
    category_block: dict[str, Any],
    config_flags: dict[str, Any] | None,
    all_candidates: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """KC·원산지·A/S 컴플라이언스 정보를 만든다.

    **순수 함수** — config 값을 읽지 않고 존재 여부(bool)만 받는다.
    법적 신고값을 지어내지 않는다.

    Args:
        product: 상품 입력 dict. ``origin_code``/``as_tel`` 등의 키를 본다.
        category_block: ``diagnose`` 가 이미 만든 category 블록.
        config_flags: R2 — 성분별 플래그.

            ::

                {
                  "origin_code_configured":    bool | None,  # origin_area_code 존재
                  "origin_content_configured": bool | None,  # origin_content 존재
                  "as_configured":             bool | None,  # as_tel|seller_tel|customerServicePhoneNumber 존재
                }

            어느 키가 없거나 None 이면 "모름"으로 다룬다.
            (이전 ``origin_configured`` 단일 키는 폐기됐다 — 원산지는 code 와
            content 가 별도로 흐르기 때문에 한 플래그로 묶으면 왕복이 재발한다.)
        all_candidates: 상품명 분류의 전체 후보 (KC 후보 집계용). 없으면 사용 안 함.

    Returns:
        ``{"kc": {...}, "origin": {...}, "after_service": {...}}`` dict.

    콤플라이언스 리뷰 수정 (F1~F5):

    - **F1**: categoryId 가 로컬 메타에 없으면 ``requires_kc=False`` 라도
      ``not_required`` 로 단정하지 않고 ``unknown`` 으로 다룬다.
    - **F2**: 후보별 ``requires_kc`` 가 None(메타 불완전) 이면
      ``kc_unresolved_candidates`` 카운트에 넣는다. 분류 가능한 후보가 0이면
      ``status="unknown"``.
    - **F3**: note 를 카운트에서 만든다 — 고정 문구 금지.
    - **F4**: 원산지를 code_provided + content_provided 두 부분으로 분해.
    - **F5**: A/S 판정에 ``as_tel`` **또는 ``seller_tel``**. 원산지도 payload 가
      받는 키 후보 전부(``origin_code`` 계열·``made_in``·``origin_content``)와 맞춘다.

    R1/R2 — 조립기 실측 기반 키 좁히기 + config 플래그 성분별 분리(wo-n76-round3.md).
    """
    flags = config_flags if isinstance(config_flags, dict) else {}

    # --- KC ---
    # categoryId 가 확정됐으면 그 카테고리의 KC 필요 여부를 본다.
    # 후보 기반이면 최고점 동점자 각각의 KC 여부를 집계한다.
    # 둘 다 아니면 unknown.
    kc_status: str
    kc_required_candidates = 0
    kc_free_candidates = 0
    kc_unresolved_candidates = 0
    kc_note: str

    # categoryId 직접 지정 여부 확인
    cid = ""
    for key in ("categoryId", "category_id"):
        v = product.get(key)
        if isinstance(v, str) and v.strip():
            cid = v.strip()
            break
        if isinstance(v, int) and v:
            cid = str(v)
            break

    if cid:
        # F1: categoryId 가 로컬 메타에 없으면 requires_kc=False 라도
        # not_required 로 단정하지 않는다.
        cid_known = _category_id_known(cid)
        if not cid_known:
            kc_status = "unknown"
        else:
            try:
                kc_needed = _category_meta.requires_kc(
                    cid, raise_if_unknown=False, raise_if_incomplete=False
                )
            except Exception:
                kc_needed = None
            if kc_needed is True:
                kc_status = "required"
            elif kc_needed is False:
                kc_status = "not_required"
            else:
                # 불명(incomplete/데이터 문제) — unknown 으로 다룬다.
                kc_status = "unknown"
    elif all_candidates:
        max_score = max(c.get("score", 0) for c in all_candidates)
        top_tied = [c for c in all_candidates if c.get("score", 0) == max_score]
        kc_required_candidates = 0
        kc_free_candidates = 0
        kc_unresolved_candidates = 0
        for c in top_tied:
            c_id = str(c.get("category_id") or "")
            if not c_id:
                continue
            # R6 — 후보는 category_meta 유래라 로컬 메타에 "있는" ID 이다
            # (category.classify_category 가 메타에 없는 ID 는 내놓지 않는다).
            # 그래서 requires_kc 가 모르는 ID 에 대해 False 를 주는 함정이
            # 실제로는 발생하지 않는다. 만약을 위해 try/except 로 None 처리하고
            # None 은 unresolved 카운트로 흘린다(F2). 새 가드를 만들지 않고
            # 이유를 주석으로 남긴다(리뷰어 제안 그대로).
            try:
                kc_needed = _category_meta.requires_kc(
                    c_id, raise_if_unknown=False, raise_if_incomplete=False
                )
            except Exception:
                kc_needed = None
            if kc_needed is True:
                kc_required_candidates += 1
            elif kc_needed is False:
                kc_free_candidates += 1
            else:
                # None(불명) 은 unresolved 카운트에 넣는다 (F2).
                kc_unresolved_candidates += 1
        # F2: 분류 가능한 후보가 0이면 unknown.
        classifiable = kc_required_candidates + kc_free_candidates
        if classifiable == 0:
            kc_status = "unknown"
        else:
            kc_status = "depends_on_category"
    else:
        kc_status = "unknown"

    # F3: note 를 카운트에서 만든다 — 고정 문구 금지.
    do_not_fabricate = "값을 지어내지 마라."
    if kc_status == "required":
        kc_note = "이 카테고리는 KC 인증 대상이다. " + do_not_fabricate
    elif kc_status == "not_required":
        kc_note = "이 카테고리는 KC 인증 대상이 아니다."
    elif kc_status == "depends_on_category":
        parts: list[str] = []
        if kc_required_candidates and kc_free_candidates:
            parts.append(
                f"후보 카테고리들 중 KC 대상 {kc_required_candidates}개와 "
                f"비대상 {kc_free_candidates}개가 섞여 있다."
            )
        elif kc_required_candidates:
            parts.append(f"후보 카테고리 {kc_required_candidates}개 모두 KC 대상이다.")
        elif kc_free_candidates:
            parts.append(f"후보 카테고리 {kc_free_candidates}개 모두 KC 대상이 아니다.")
        if kc_unresolved_candidates:
            parts.append(f"KC 필요 여부를 확정할 수 없는 후보 {kc_unresolved_candidates}개가 있다.")
        parts.append("카테고리 확정 후 판정된다. " + do_not_fabricate)
        kc_note = " ".join(parts)
    elif kc_status == "unknown" and cid and not _category_id_known(cid):
        kc_note = (
            f"categoryId {cid} 를 로컬 메타에서 찾을 수 없어 "
            "KC 인증 대상 여부를 알 수 없다. 카테고리를 확인하라. " + do_not_fabricate
        )
    elif (
        kc_status == "unknown"
        and all_candidates
        and kc_unresolved_candidates
        and not kc_required_candidates
        and not kc_free_candidates
    ):
        kc_note = (
            f"후보 {kc_unresolved_candidates}개 모두 KC 필요 여부가 불명이다. "
            "카테고리를 확인하라. " + do_not_fabricate
        )
    elif kc_status == "unknown":
        kc_note = "KC 인증 대상 여부를 알 수 없다. 카테고리를 확인하라. " + do_not_fabricate

    # --- origin (F4: 두 부분으로 분해, R1: 상품측 키 좁힘, R2: 성분별 config 플래그) ---
    origin_code_provided = _has_text_in(product, _ORIGIN_CODE_KEYS)
    origin_content_provided = _has_text_in(product, _ORIGIN_CONTENT_KEYS)
    # R2 — config 측 플래그도 code 와 content 를 별도로.
    # 조립기가 config 에서 읽는 키: code 쪽은 origin_area_code,
    # content 쪽은 origin_content. (이전 origin_configured 단일 키 폐기.)
    origin_code_cfg_raw = flags.get("origin_code_configured")
    origin_code_configured: bool | None = (
        bool(origin_code_cfg_raw) if origin_code_cfg_raw is not None else None
    )
    origin_content_cfg_raw = flags.get("origin_content_configured")
    origin_content_configured: bool | None = (
        bool(origin_content_cfg_raw) if origin_content_cfg_raw is not None else None
    )

    # --- after_service (F5: as_tel 또는 seller_tel, R1: 상품측 키 좁힘) ---
    as_in_product = _has_text_in(product, _AS_TEL_KEYS)
    # R2 — config 측은 as_tel|seller_tel|customerServicePhoneNumber 셋 중 하나.
    as_cfg_raw = flags.get("as_configured")
    as_configured: bool | None = bool(as_cfg_raw) if as_cfg_raw is not None else None

    return {
        "kc": {
            "status": kc_status,
            "kc_required_candidates": kc_required_candidates,
            "kc_free_candidates": kc_free_candidates,
            "kc_unresolved_candidates": kc_unresolved_candidates,
            "note": kc_note,
        },
        "origin": {
            "code_provided": origin_code_provided,
            "content_provided": origin_content_provided,
            # R2 — 단일 configured 대신 성분별 플래그.
            "code_configured": origin_code_configured,
            "content_configured": origin_content_configured,
        },
        "after_service": {
            "provided_in_product": as_in_product,
            "configured": as_configured,
        },
    }


def _compliance_missing_items(compliance: dict[str, Any]) -> list[dict[str, str]]:
    """컴플라이언스 블록에서 missing 에 넣을 항목을 만든다 (보수 규칙).

    원산지(R2): ``code_provided`` 와 ``content_provided`` 를 **별도로** 판정한다.
    각 성분에 대해 (상품에 없음) **and** (해당 성분 configured==False, 명시적) 일
    때만 missing 에 넣는다. 어느 쪽이든 ``configured=None``(모름) 이면 해당
    성분은 missing 에 추가하지 않는다.
    A/S(F5): ``provided_in_product=False`` **이고** ``configured=False``
    (명시적 False) 일 때만 missing 에 추가.
    KC: missing 에 넣지 않는다 — 값 요구가 아니라 정보 제공이다.
    """
    items: list[dict[str, str]] = []

    origin = compliance.get("origin") or {}
    # R2 — 성분별 configured 를 본다 (이전 단일 configured 폐기).
    if origin.get("code_configured") is False and not origin.get("code_provided"):
        items.append(
            {
                "field": "origin_code",
                "label": "원산지 코드",
                "why": (
                    "원산지 코드(origin_code)가 상품 입력에 없고 설정에도 없습니다. "
                    "둘 중 하나에 실제 원산지 코드를 입력해야 합니다."
                ),
                "answer_shape": "text",
            }
        )
    if origin.get("content_configured") is False and not origin.get("content_provided"):
        items.append(
            {
                "field": "origin_content",
                "label": "원산지 표시문구",
                "why": (
                    "원산지 표시문구(made_in)가 상품 입력에 없고 설정에도 없습니다. "
                    "둘 중 하나에 실제 원산지 표시문구를 입력해야 합니다."
                ),
                "answer_shape": "text",
            }
        )

    after_service = compliance.get("after_service") or {}
    if (
        after_service.get("provided_in_product") is False
        and after_service.get("configured") is False
    ):
        items.append(
            {
                "field": "as_tel",
                "label": "A/S 전화번호",
                "why": (
                    "A/S 안내 전화번호가 상품 입력에 없고 설정에도 없습니다. "
                    "둘 중 하나에 실제 전화번호를 입력해야 합니다."
                ),
                "answer_shape": "text",
            }
        )

    return items


def _common_notice_missing_items(
    product: dict[str, Any],
    config_flags: dict[str, Any] | None,
    deferred_notice_fields: list[str] | None = None,
) -> list[dict[str, str]]:
    """공통 고시 5필드 중 상품·설정 모두에 없는 항목을 진단한다.

    후보 키·중첩 고시 본문·자리표시자 규칙은 등록 조립기(``naver_client``)의
    공유 해석을 그대로 쓴다. 미루기 판정도 컴플라이언스 경로의
    ``qa_agents._field_missing_with_deferred`` 를 그대로 호출한다. 준비 진단이
    별도 후보 목록이나 미루기 판정으로 등록 경계와 어긋나지 않게 하기 위함이다.
    config 값 자체는 읽거나 반환하지 않고, 호출자가 넘긴 존재 여부 플래그만 사용한다.
    """
    flags = config_flags if isinstance(config_flags, dict) else {}
    configured = flags.get("common_notice_configured")
    if not isinstance(configured, dict):
        return []

    from . import naver_client

    items: list[dict[str, str]] = []
    for notice_field, product_keys, _config_keys in naver_client._NOTICE_COMMON_FIELD_CANDIDATES:
        # config 읽기 실패/미상(None)은 미설정으로 단정하지 않는다.
        if configured.get(notice_field) is not False:
            continue
        if naver_client._notice_common_field_provided_by_product(
            product, notice_field, product_keys
        ):
            continue
        # 컴플라이언스가 쓰는 동일 판정기로, 명시적으로 미룬 문자열 필드는
        # 누락 질문에서 제외한다. 빈 본문을 주는 것은 이 시점에 필요한 사실이
        # "해당 필드가 비어 있다면 미루기가 적용되는가" 하나뿐이기 때문이다.
        if notice_field not in _qa_agents._field_missing_with_deferred(
            {}, (notice_field,), deferred_notice_fields
        ):
            continue
        try:
            label, _hint = _notice_labels._notice_field_label(notice_field, None)
        except Exception:
            label = notice_field
        items.append(
            {
                "field": notice_field,
                "label": str(label),
                "why": (
                    f"공통 고시 필드 {notice_field}가 상품 입력과 설정 모두에 없습니다. "
                    "실제 신고값을 입력해야 합니다."
                ),
                "answer_shape": "text",
            }
        )
    return items


def diagnose(
    product: dict[str, Any],
    *,
    config_flags: dict[str, Any] | None = None,
    deferred_notice_fields: list[str] | None = None,
) -> dict[str, Any]:
    """거부 시점에 알 수 있는 필요사항을 한 번에 진단한다.

    **순수 함수** — 네트워크·LLM·파일쓰기 0회. 읽기 전용 데이터만 쓴다.
    config 값을 읽지 않는다 — 호출자가 존재 플래그만 넘긴다(``config_flags``).

    Args:
        product: 상품 입력 dict. ``name``/``title_ko``/``salePrice``/
            ``image_sources``/``origin_code``/``as_tel`` 등의 키를 읽는다.
        config_flags: R2 — 성분별 플래그::

            {
              "origin_code_configured":    bool | None,  # origin_area_code 존재
              "origin_content_configured": bool | None,  # origin_content 존재
              "as_configured":             bool | None,  # as_tel|seller_tel|customerServicePhoneNumber 존재
              "common_notice_configured": {
                "returnCostReason": bool | None,
                "noRefundReason": bool | None,
                "qualityAssuranceStandard": bool | None,
                "compensationProcedure": bool | None,
                "troubleShootingContents": bool | None,
              },
            }
        deferred_notice_fields: 판매자가 명시적으로 미루기로 선택한 고시 필드명.
            공통 고시 5필드는 컴플라이언스와 같은 판정으로, 실제로 미룰 수 있는
            필드만 ``missing`` 에서 제외한다.

            원산지·A/S·공통 고시 필드의 설정 존재 여부(값이 아님). 어느 키가
            ``None`` 이면 "모름" — 모름을 미설정(False)으로 단정하지 마라.
            ``diagnose`` 자체는 config 파일을 읽지 않으므로 호출자(mcp_server)가
            이 플래그를 만들어 넘겨야 한다.

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
                  "scope": "confirmed_category"|"top_tied_candidates"|"universal_only",
                  "is_complete": bool,                  # 카테고리 미확정이면 절대 True 아님
                  "completion_blocked_by": [str, ...],  # 완료를 막고 있는 사유
                  "certain": [ {"field": str, "label": str}, ... ],  # 교집합(XOR 제외)
                  "certain_one_of": [ [ {"field","label"}, ... ], ... ], # XOR 그룹
                  "candidate_groups": [ {"notice_type": str, "candidates": [...],
                                         "additional": [...], "additional_one_of": [[...]]}, ... ],
                  "unresolved_notice_types": [str, ...],  # 필드를 못 구한 고시타입
              },
              "compliance": {
                  "kc": {
                      "status": "required"|"not_required"|"depends_on_category"|"unknown",
                      "kc_required_candidates": int,  # depends_on_category 일 때만 의미
                      "kc_free_candidates": int,
                      "kc_unresolved_candidates": int,  # KC 필요 여부 불명 후보 수 (F2)
                      "note": str,  # 카운트에서 만든 문구 (F3). 값을 지어내지 말라는 안내 포함
                  },
                  "origin": {
                      "code_provided": bool,       # product 의 origin_code (F4, R1)
                      "content_provided": bool,    # product 의 made_in / origin_content (F4, R1)
                      "code_configured": bool | None,    # config origin_area_code 존재 (R2)
                      "content_configured": bool | None, # config origin_content 존재 (R2)
                  },
                  "after_service": {
                      "provided_in_product": bool,  # product.as_tel 또는 seller_tel (F5, R1)
                      "configured": bool | None,
                  },
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
      - ``notice_required_fields.candidate_groups`` 의 타입별 ``additional`` /
        ``additional_one_of`` 는 해당 타입으로 확정됐을 때 추가로 필요한 항목이다.
        ``category.likely_notice_type`` 이 추정 타입이므로 참고용으로만 쓰라.
      - ``compliance.kc`` 는 KC 인증 대상 여부를 알려준다. ``status="required"``
        면 인증정보가 필요하다 — **KC 인증번호를 지어내지 마라.**
      - ``compliance.origin.code_configured``/``content_configured`` 및
        ``compliance.after_service.configured`` 가 ``None`` 이면 "모름"이다.
        모름을 미설정으로 단정하지 마라.
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
    all_candidates: list[dict[str, Any]] | None = None
    # 최고점 동점자와 그 고시타입을 한 번만 계산해 재사용.
    # 아래 category_block 조립과 notice_required_fields 산출 양쪽에서 쓴다.
    top_tied: list[dict[str, Any]] = []
    top_notice_types: list[str] = []

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
            # top_tied/top_notice_types 를 한 번만 계산한다.
            # 이 결과는 notice_required_fields 산출에서도 그대로 재사용된다.
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
        # 위 category_block 조립에서 이미 계산한 top_tied/top_notice_types
        # 를 재사용한다. 같은 all_candidates 에서 같은 max_score 로 고른 같은
        # 리스트이므로 다시 계산할 필요가 없다.
        candidates_for_fields = top_tied
        notice_types_for_fields = top_notice_types

    notice_required_fields = _build_notice_required_fields_block(
        candidates_for_fields,
        notice_types_for_fields,
        notice_type,
        likely_notice_type,
        is_explicit_confirmed=(explicit_notice_type is not None or cid_notice_type is not None),
    )

    # --- compliance: KC · 원산지 · A/S ---
    compliance = _build_compliance_block(product, category_block, config_flags, all_candidates)
    missing.extend(_compliance_missing_items(compliance))
    # 공통 고시 5필드는 모든 고시 타입의 교집합이다. build_payload 가 A/S
    # fail-closed 에서 먼저 멈춰도, 준비 진단은 같은 턴에 이 누락을 함께 알린다.
    missing.extend(_common_notice_missing_items(product, config_flags, deferred_notice_fields))

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
        "compliance": compliance,
        "images": images_block,
    }


__all__ = [
    "_build_candidates_by_notice_type",
    "_build_compliance_block",
    "_candidates_from_title",
    "_candidates_with_all_types",
    "_category_id_known",
    "_common_notice_missing_items",
    "_compliance_missing_items",
    "_explicit_notice_type_from_category_id",
    "_explicit_notice_type_from_input",
    "_has_text_in",
    "_intersect_field_lists",
    "_top_candidates",
    "_valid_image_count",
    "_xor_groups_for_types",
    "diagnose",
]
