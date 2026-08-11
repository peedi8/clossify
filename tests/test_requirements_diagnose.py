"""``requirements.diagnose`` 회귀 테스트 — N19 거부 정보량 증대 (v2).

본 테스트는 ``prepare_listing`` 거부 시점에 ``diagnose`` 가 반환하는 정보를
고정한다. acceptance 2·3·4·6 (이미지만 빠진 경우 · 이름도 없는 경우 · 갈리는
경우 · 진단이 본체를 죽이지 않는다) 을 회귀로 잡는다.

N19-v2 변경: ``category.status`` 에 ``likely`` 가 추가되고,
``notice_required_fields`` 가 dict (``certain``/``likely_type``/``likely_extra``)
로 바뀌었다. 교집합(certain) 은 후보가 있으면 항상 채운다.

순수성: ``tests/conftest.py`` 의 외부 소켓 차단(autouse) 이 이미 깔려 있으므로,
본 테스트가 통과하면 ``diagnose`` 가 네트워크를 타지 않는 것이 자동으로 증명된다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import ClassVar
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, requirements


# --------------------------------------------------------------------------- #
# Acceptance 2: 사진만 빠진 경우 — 티셔츠.
# N19-v2: status 는 "likely", likely_notice_type 은 "WEAR",
#         certain 은 WEAR∩ETC 교집합(7개).
# --------------------------------------------------------------------------- #
class TestPhotoOnlyMissing:
    """이미지만 빠진 입력으로 ``prepare_listing`` 을 호출하면 풍부한 거부 응답."""

    def test_ok_is_false(self):
        """여전히 막힌다 (거부를 통과로 바꾸지 않는다)."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        assert result["ok"] is False

    def test_needs_user_has_image_requirement(self):
        """``needs_user`` 에 이미지 요구가 들어있어야 한다."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        needs_user = result.get("needs_user") or []
        fields = [item.get("field") for item in needs_user if isinstance(item, dict)]
        assert (
            "image_sources" in fields
        ), f"needs_user 에 image_sources 가 없음: {json.dumps(needs_user, ensure_ascii=False)}"

    def test_category_candidates_not_empty(self):
        """``requirements.category.candidates`` 가 비어있지 않아야 한다."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        candidates = category.get("candidates") or []
        assert (
            len(candidates) > 0
        ), f"candidates 가 비어있음: {json.dumps(category, ensure_ascii=False)}"

    def test_category_status_is_likely_or_confident(self):
        """``status`` 는 ``likely`` 또는 ``confident`` 여야 한다.

        N19-v2: 티셔츠 케이스는 상위 후보가 WEAR·WEAR·ETC 이므로
        최고점 후보들이 WEAR 로 같다 → ``likely``.
        하지만 분류기 버전에 따라 전부 WEAR 로 나올 수도 있으므로
        likely/confident 둘 다 허용한다.
        """
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        status = category.get("status")
        assert status in ("confident", "likely"), f"예상 밖 status: {status}"

    def test_likely_notice_type_is_wear(self):
        """``likely_notice_type`` 은 ``WEAR`` 이어야 한다."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        likely_nt = category.get("likely_notice_type")
        assert likely_nt == "WEAR", f"likely_notice_type 이 WEAR 이 아님: {likely_nt}"

    def test_notice_required_fields_is_dict_with_certain(self):
        """``notice_required_fields`` 는 dict 이고 ``certain`` 이 있다."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields")
        assert isinstance(nrf, dict), f"notice_required_fields 가 dict 가 아님: {type(nrf)}"
        assert "certain" in nrf, f"certain 키가 없음: {json.dumps(nrf, ensure_ascii=False)}"
        assert isinstance(nrf["certain"], list)
        assert (
            len(nrf["certain"]) > 0
        ), f"certain 이 빈 리스트: {json.dumps(nrf, ensure_ascii=False)}"

    def test_certain_is_intersection_not_union(self):
        """``certain`` + ``certain_one_of`` 평탄화 합은 13 이어야 한다 (F1).

        N19 리뷰 수정: 이제 **최고점 동점자 전부** 의 고시타입으로 교집합을 구한다.
        티셔츠 케이스는 최고점 동점자가 WEAR·WEAR (2개) 이므로 교집합 = WEAR 전체.
        XOR 그룹은 없다(WEAR 은 XOR 정의가 없음) → certain = 13, one_of = 0.
        acceptance 표 기대값 13 (certain + certain_one_of 평탄화 합).
        """
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain = nrf.get("certain") or []
        one_of = nrf.get("certain_one_of") or []
        one_of_flat = [f["field"] for g in one_of for f in g]
        total = len(certain) + len(one_of_flat)
        assert total == 13, (
            f"certain+one_of 합이 13이 아님: {total} — "
            f"certain={json.dumps([f['field'] for f in certain], ensure_ascii=False)}, "
            f"one_of={one_of_flat}"
        )

    def test_certain_fields_in_top_tie_types(self):
        """``certain`` 의 모든 필드가 최고점 동점자 고시타입 전체에 있어야 한다.

        N19 리뷰 수정: 최고점 동점자가 WEAR·WEAR 이므로 certain 은 WEAR 필드
        전체에서 온다. 교집합(WEAR ∩ WEAR) = WEAR.
        """
        from clossify import listing_templates

        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain_fields = {f["field"] for f in nrf.get("certain") or []}
        # 최고점 동점자가 WEAR 만 있으므로 WEAR ⊇ certain 이어야 한다.
        wear_fields = set(listing_templates._notice_type_fields_for("WEAR"))
        assert (
            certain_fields <= wear_fields
        ), f"certain 에 WEAR 에 없는 필드가 있음: {certain_fields - wear_fields}"

    def test_full_response_json(self):
        """실제 반환 JSON 을 출력한다 (acceptance 의 '실제 반환 JSON 을 붙여라')."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        print("\n=== Acceptance 2 JSON ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Acceptance 3: 이름도 없는 경우.
# --------------------------------------------------------------------------- #
class TestEmptyProduct:
    """``{}`` 로 호출하면 이름·가격·이미지가 전부 들어있어야 한다."""

    def test_needs_user_has_all_three(self):
        result = mcp_server.prepare_listing({})
        needs_user = result.get("needs_user") or []
        fields = {item.get("field") for item in needs_user if isinstance(item, dict)}
        assert {
            "name",
            "salePrice",
            "image_sources",
        } <= fields, f"이름·가격·이미지가 전부 들어있지 않음: {fields}"

    def test_category_status_unknown(self):
        result = mcp_server.prepare_listing({})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        assert (
            category.get("status") == "unknown"
        ), f"status 가 unknown 이 아님: {category.get('status')}"

    def test_category_candidates_empty(self):
        result = mcp_server.prepare_listing({})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        assert (
            category.get("candidates") == []
        ), f"candidates 가 빈 리스트가 아님: {category.get('candidates')}"

    def test_notice_required_fields_empty_dict(self):
        """이름이 없으면 notice_required_fields 는 빈 dict 구조여야 한다.

        N19-v2: ``certain``·``likely_extra`` 빈 리스트, ``likely_type`` None.
        """
        result = mcp_server.prepare_listing({})
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields")
        assert isinstance(nrf, dict), f"notice_required_fields 가 dict 가 아님: {type(nrf)}"
        assert nrf.get("certain") == [], f"certain 이 빈 리스트가 아님: {nrf.get('certain')}"
        assert (
            nrf.get("likely_type") is None
        ), f"likely_type 이 None 이 아님: {nrf.get('likely_type')}"
        assert (
            nrf.get("likely_extra") == []
        ), f"likely_extra 가 빈 리스트가 아님: {nrf.get('likely_extra')}"

    def test_full_response_json(self):
        result = mcp_server.prepare_listing({})
        print("\n=== Acceptance 3 JSON ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Acceptance 4: 갈리는 경우 — 유기농 아몬드.
# N19-v2: status 는 "likely" (최고점 후보들이 FOOD 로 같으므로).
#         likely_notice_type 은 "FOOD", certain 은 FOOD∩FASHION_ITEMS 교집합.
# --------------------------------------------------------------------------- #
class TestAmbiguousCategory:
    """``유기농 아몬드 500g`` 은 status 가 likely 여야 한다 (N19-v2)."""

    def test_status_likely(self):
        """N19-v2: 최고점 후보들이 FOOD 로 같다 → likely."""
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        assert (
            category.get("status") == "likely"
        ), f"status 가 likely 가 아님: {category.get('status')}"

    def test_notice_types_seen_has_food_and_fashion(self):
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        types_seen = category.get("notice_types_seen") or []
        assert "FOOD" in types_seen, f"FOOD 이 notice_types_seen 에 없음: {types_seen}"
        assert (
            "FASHION_ITEMS" in types_seen
        ), f"FASHION_ITEMS 가 notice_types_seen 에 없음: {types_seen}"

    def test_notice_type_is_none(self):
        """likely 상태이므로 notice_type 은 None 이다."""
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        assert (
            category.get("notice_type") is None
        ), f"notice_type 이 None 이 아님: {category.get('notice_type')}"

    def test_likely_notice_type_is_food(self):
        """최고점 후보들이 FOOD 로 같다 → likely_notice_type = FOOD."""
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        assert (
            category.get("likely_notice_type") == "FOOD"
        ), f"likely_notice_type 이 FOOD 가 아님: {category.get('likely_notice_type')}"

    def test_certain_is_intersection(self):
        """certain + certain_one_of 평탄화 합은 19 이어야 한다 (F1/F4).

        N19 리뷰 수정: 최고점 동점자가 FOOD 1개이므로 교집합 = FOOD 전체.
        FOOD 의 XOR 그룹(packDate/packDateText, consumptionDate/consumptionDateText)
        은 certain_one_of 로 옮겨간다. acceptance 표 기대값 19
        (certain=15, certain_one_of 평탄화=4).
        """
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain = nrf.get("certain") or []
        one_of = nrf.get("certain_one_of") or []
        one_of_flat = [f["field"] for g in one_of for f in g]
        total = len(certain) + len(one_of_flat)
        assert total == 19, (
            f"certain+one_of 합이 19이 아님: {total} — "
            f"certain={json.dumps([f['field'] for f in certain], ensure_ascii=False)}, "
            f"one_of={one_of_flat}"
        )
        # XOR 그룹이 실제로 채워져 있어야 한다 (F4).
        assert len(one_of) >= 1, "certain_one_of 가 비어있음 — F4 미적용"

    def test_full_response_json(self):
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        print("\n=== Acceptance 4 JSON ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Acceptance 6: 진단이 본체를 죽이지 않는다.
# --------------------------------------------------------------------------- #
class TestDiagnoseFailureSafe:
    """``diagnose`` 가 예외를 던져도 거부 응답이 살아있어야 한다."""

    def test_error_remains_when_diagnose_raises(self):
        """``diagnose`` 를 일시적으로 예외를 던지게 만들면 거부 응답의 ``error``
        가 그대로 남고 ``requirements`` 만 ``None`` 이 되어야 한다.
        """
        with mock.patch.object(
            requirements,
            "diagnose",
            side_effect=RuntimeError("강제 진단 실패"),
        ):
            result = mcp_server.prepare_listing(
                {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
            )
        assert result["ok"] is False
        # 원래 error 가 그대로 남아있어야 한다.
        assert result.get("error"), "error 가 비어있음"
        assert "강제 진단 실패" not in str(
            result.get("error")
        ), f"진단 예외 메시지가 error 에 섞임: {result.get('error')}"
        # requirements 만 None.
        assert (
            result.get("requirements") is None
        ), f"requirements 가 None 이 아님: {result.get('requirements')}"

    def test_error_remains_when_diagnose_raises_on_empty_product(self):
        """``{}`` 입력에서 진단이 실패해도 거부 응답이 살아있어야 한다."""
        with mock.patch.object(
            requirements,
            "diagnose",
            side_effect=RuntimeError("강제 진단 실패"),
        ):
            result = mcp_server.prepare_listing({})
        assert result["ok"] is False
        assert result.get("error"), "error 가 비어있음"
        assert result.get("requirements") is None


# --------------------------------------------------------------------------- #
# Acceptance 5: 순수성 — 네트워크를 타지 않는다.
# --------------------------------------------------------------------------- #
class TestPurity:
    """``diagnose`` 가 네트워크를 타지 않는다는 증명.

    ``tests/conftest.py`` 의 autouse 소켓 가드가 외부 연결을 차단하므로,
    위 모든 테스트가 ``ExternalNetworkBlockedError`` 없이 통과했다는 사실 자체가
    순수성의 증명이다. 본 클래스는 그 사실을 명시적으로 기록한다.
    """

    def test_diagnose_does_not_touch_network(self):
        """diagnose 호출이 ExternalNetworkBlockedError 를 일으키지 않는다."""
        # 이 호출이 성공하는 것 자체가 증거 — 가드가 켜져 있으므로
        # 외부로 나가려 했으면 ExternalNetworkBlockedError 가 발생한다.
        result = requirements.diagnose({"name": "스테인리스 텀블러 500ml", "salePrice": 15000})
        assert isinstance(result, dict)
        assert "missing" in result
        assert "category" in result


# =========================================================================== #
# N19 리뷰 수정 회귀 — F1 ~ F7 (wo-n19-review-fixes.md)
# =========================================================================== #


# --------------------------------------------------------------------------- #
# F1: 최고점 동점자 전부를 판정에 쓴다 — 7행 표.
# --------------------------------------------------------------------------- #
class TestF1TopTieAll:
    """``wo-n19-review-fixes.md`` F1 표 7행을 재현한다.

    교집합(원본) 은 **최고점 동점자 전부** 의 고시타입으로 구한다.
    ``certain`` + ``certain_one_of``(평탄화) 합 = 교집합 원본 개수.
    """

    CASES: ClassVar[list[tuple[str, set[str], int]]] = [
        # (상품명, 예상 status 집합, 예상 교집합 원본 개수)
        ("여성 반팔 티셔츠 면 100%", {"likely", "confident"}, 13),
        ("유기농 아몬드 500g", {"likely"}, 19),
        ("남성 캐주얼 셔츠", {"ambiguous", "likely"}, 11),
        ("수분 크림 50ml", {"likely"}, 19),
        ("스테인리스 텀블러 500ml", {"confident"}, 17),
        ("무선 블루투스 이어폰", {"likely"}, 18),
        ("시계", {"ambiguous"}, 11),
    ]

    def _diagnose(self, name):
        return requirements.diagnose({"name": name, "salePrice": 10000})

    def test_all_7_cases_intersection_counts(self):
        """7행 전부 — 교집합 원본 개수가 acceptance 표와 같아야 한다."""
        failures = []
        for name, exp_status_set, exp_total in self.CASES:
            result = self._diagnose(name)
            cat = result.get("category") or {}
            nrf = result.get("notice_required_fields") or {}
            certain = nrf.get("certain") or []
            one_of = nrf.get("certain_one_of") or []
            one_of_flat = [f["field"] for g in one_of for f in g]
            total = len(certain) + len(one_of_flat)
            status = cat.get("status")
            if status not in exp_status_set:
                failures.append(f"{name!r}: status={status!r} not in {exp_status_set}")
            if total != exp_total:
                failures.append(f"{name!r}: certain+one_of total={total}, expected {exp_total}")
        assert not failures, "\n".join(failures)

    def test_watch_is_ambiguous_with_furniture_jewellery(self):
        """``시계`` 는 ambiguous + FURNITURE/JEWELLERY (★가장 위험 케이스)."""
        result = self._diagnose("시계")
        cat = result.get("category") or {}
        assert cat.get("status") == "ambiguous", f"status: {cat.get('status')}"
        types_seen = cat.get("notice_types_seen") or []
        assert "FURNITURE" in types_seen, f"FURNITURE 없음: {types_seen}"
        assert "JEWELLERY" in types_seen, f"JEWELLERY 없음: {types_seen}"

    def test_intersection_preserved_in_certain_and_one_of(self):
        """certain + certain_one_of(평탄화) 는 교집합 원본과 같은 집합.

        XOR 그룹 멤버를 빼먹지 않는지 확인 (F4 의 무결성 조건).
        """
        from clossify import listing_templates

        for name, _, _ in self.CASES:
            result = self._diagnose(name)
            nrf = result.get("notice_required_fields") or {}
            certain = nrf.get("certain") or []
            one_of = nrf.get("certain_one_of") or []
            actual_set = {f["field"] for f in certain}
            for g in one_of:
                actual_set |= {f["field"] for f in g}
            # 교집합 원본을 직접 다시 구한다 (최고점 동점자 기준).
            cands, _ = requirements._candidates_from_title(name)
            if not cands:
                continue
            max_score = max(c.get("score", 0) for c in cands)
            top_ties = [c for c in cands if c.get("score", 0) == max_score]
            top_types = requirements._notice_types_for_candidates(top_ties)
            lists = []
            for nt in top_types:
                try:
                    lists.append(listing_templates._notice_type_fields_for(nt))
                except Exception:
                    lists.append(())
            intersection = set(requirements._intersect_field_lists(lists))
            assert actual_set == intersection, (
                f"{name!r}: actual != intersection — "
                f"missing={intersection - actual_set}, "
                f"extra={actual_set - intersection}"
            )


# --------------------------------------------------------------------------- #
# F2: needs_category_choice 플래그.
# --------------------------------------------------------------------------- #
class TestF2NeedsCategoryChoice:
    """classify_category 가 dict(ambiguous) 면 needs_category_choice=True."""

    def test_dict_input_sets_needs_category_choice_true(self):
        """``소설 책`` 은 카테고리가 ambiguous → True."""
        result = requirements.diagnose({"name": "소설 책", "salePrice": 10000})
        cat = result.get("category") or {}
        assert (
            cat.get("needs_category_choice") is True
        ), f"needs_category_choice 가 True 가 아님: {cat}"

    def test_empty_input_sets_needs_category_choice_false(self):
        """이름이 없으면 needs_category_choice=False."""
        result = requirements.diagnose({})
        cat = result.get("category") or {}
        assert cat.get("needs_category_choice") is False

    def test_docstring_documents_needs_category_choice(self):
        """독스트링에 needs_category_choice 안내가 들어있어야 한다."""
        doc = requirements.diagnose.__doc__ or ""
        assert "needs_category_choice" in doc, "독스트링에 needs_category_choice 없음"
        assert "사용자" in doc or "고르" in doc, "독스트링에 사용자 고르라는 안내 없음"


# --------------------------------------------------------------------------- #
# F3: 입력에 명시된 categoryId / noticeType 을 우선 쓴다.
# --------------------------------------------------------------------------- #
class TestF3ExplicitInputPriority:
    """사용자가 명시한 정보를 무시하지 않는다 — 3가지 경우."""

    def test_case1_explicit_notice_type_jewellery(self):
        """① notice.productInfoProvidedNoticeType=JEWELLERY → confident/JEWELLERY."""
        result = requirements.diagnose(
            {
                "name": "아무거나",
                "salePrice": 1,
                "notice": {"productInfoProvidedNoticeType": "JEWELLERY"},
            }
        )
        cat = result.get("category") or {}
        assert cat.get("status") == "confident", f"status: {cat.get('status')}"
        assert cat.get("notice_type") == "JEWELLERY", f"notice_type: {cat.get('notice_type')}"
        assert cat.get("needs_category_choice") is False

    def test_case2_category_id_infers_wear(self):
        """② categoryId=50000803 → WEAR (패션의류>여성의류>티셔츠)."""
        result = requirements.diagnose(
            {"name": "아무거나", "salePrice": 1, "categoryId": "50000803"}
        )
        cat = result.get("category") or {}
        assert cat.get("status") == "confident", f"status: {cat.get('status')}"
        assert cat.get("notice_type") == "WEAR", f"notice_type: {cat.get('notice_type')}"

    def test_case3_nameless_with_category_id(self):
        """③ categoryId 만 있고 이름이 없어도 → WEAR + missing 에 name/salePrice/image."""
        result = requirements.diagnose({"categoryId": "50000803"})
        cat = result.get("category") or {}
        assert cat.get("notice_type") == "WEAR", f"notice_type: {cat.get('notice_type')}"
        missing = result.get("missing") or []
        missing_fields = {m.get("field") for m in missing}
        assert "name" in missing_fields, f"name 이 missing 에 없음: {missing_fields}"
        assert "salePrice" in missing_fields
        assert "image_sources" in missing_fields

    def test_unknown_category_id_returns_none(self):
        """모르는 categoryId 는 None (예외 X) → 이름 분류 경로로 넘어간다."""
        nt = requirements._explicit_notice_type_from_category_id({"categoryId": "99999999"})
        assert nt is None


# --------------------------------------------------------------------------- #
# F4: XOR 필드를 certain_one_of 로 분리한다.
# --------------------------------------------------------------------------- #
class TestF4XorFields:
    """아몬드·크림·텀블러·이어폰 — certain_one_of 가 비어있지 않다."""

    CASES: ClassVar[list[tuple[str, str]]] = [
        ("유기농 아몬드 500g", "FOOD"),
        ("수분 크림 50ml", "COSMETIC"),
        ("스테인리스 텀블러 500ml", "KITCHEN_UTENSILS"),
        ("무선 블루투스 이어폰", "HOME_APPLIANCES"),
    ]

    def test_certain_one_of_non_empty_for_all(self):
        """4건 전부 certain_one_of 가 비어있지 않아야 한다."""
        for name, _ in self.CASES:
            result = requirements.diagnose({"name": name, "salePrice": 10000})
            nrf = result.get("notice_required_fields") or {}
            one_of = nrf.get("certain_one_of") or []
            assert len(one_of) >= 1, f"{name!r}: certain_one_of 가 비어있음"

    def test_no_xor_field_duplicated_in_certain(self):
        """certain 의 필드가 certain_one_of 멤버와 중복되지 않는다."""
        for name, _ in self.CASES:
            result = requirements.diagnose({"name": name, "salePrice": 10000})
            nrf = result.get("notice_required_fields") or {}
            certain_fields = {f["field"] for f in nrf.get("certain") or []}
            one_of_fields = {f["field"] for g in nrf.get("certain_one_of") or [] for f in g}
            overlap = certain_fields & one_of_fields
            assert not overlap, f"{name!r}: certain ∩ one_of = {overlap} (중복)"


# --------------------------------------------------------------------------- #
# F5: 쓸 수 없는 이미지를 provided 에서 뺀다.
# --------------------------------------------------------------------------- #
class TestF5InvalidImages:
    """공백만인 image_sources 는 provided=0, missing 에 들어간다."""

    def test_empty_string_image_sources(self):
        result = requirements.diagnose({"name": "테스트", "salePrice": 1000, "image_sources": [""]})
        images = result.get("images") or {}
        assert images.get("provided") == 0, f"provided: {images.get('provided')}"
        missing = result.get("missing") or []
        missing_fields = {m.get("field") for m in missing}
        assert "image_sources" in missing_fields

    def test_whitespace_only_image_sources(self):
        result = requirements.diagnose(
            {"name": "테스트", "salePrice": 1000, "image_sources": ["   "]}
        )
        images = result.get("images") or {}
        assert images.get("provided") == 0
        missing = result.get("missing") or []
        missing_fields = {m.get("field") for m in missing}
        assert "image_sources" in missing_fields

    def test_valid_images_not_in_missing(self):
        """유효한 이미지가 있으면 missing 에 image_sources 가 없다."""
        result = requirements.diagnose(
            {
                "name": "테스트",
                "salePrice": 1000,
                "image_sources": ["real.jpg"],
            }
        )
        images = result.get("images") or {}
        assert images.get("provided") == 1
        missing = result.get("missing") or []
        missing_fields = {m.get("field") for m in missing}
        assert "image_sources" not in missing_fields


# --------------------------------------------------------------------------- #
# F6: 성공 경로에 requirements 키가 항상 있다 (값은 None).
# --------------------------------------------------------------------------- #
class TestF6SuccessPathRequirementsKey:
    """성공 경로 반환에 ``requirements: None`` 이 항상 있다."""

    def test_success_path_has_requirements_none(self):
        """성공하는 입력을 주면 ok=True 이고 requirements=None.

        ``prepare_listing`` 을 직접 부르기 어려우므로(성공 조건 까다로움),
        ``mcp_server._safe_diagnose`` 의 결과에 키가 항상 있다는 것은
        ``mcp_server`` 의 성공 경로 dict 에 키가 들어있는 것으로 확인한다.
        """
        # mcp_server 의 _result (성공 경로) 에 requirements 키가 있어야 한다.
        # 정적 검사: 소스에서 "requirements": None 을 찾는다.
        import inspect

        from clossify import mcp_server

        src = inspect.getsource(mcp_server)
        # 성공 경로의 dict literal 에 "requirements": None 이 있어야 한다.
        assert (
            '"requirements": None' in src or "'requirements': None" in src
        ), "mcp_server 소스에 성공 경로 'requirements': None 이 없음"

    def test_failure_path_has_requirements_key(self):
        """실패 경로에도 requirements 키가 있다(diagnose 결과)."""
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        assert "requirements" in result, "requirements 키 자체가 없음"
        # 거부 경로이므로 None 이 아닌 dict 여야 한다.
        assert result["requirements"] is not None


# --------------------------------------------------------------------------- #
# F7: 해석 못 한 고시타입이 교집합을 비게 만든다.
# --------------------------------------------------------------------------- #
class TestF7UnknownNoticeType:
    """알 수 없는 고시타입이 후보에 섞이면 certain 이 빈다."""

    def test_unknown_type_makes_certain_empty(self):
        """``_infer_notice_type`` 를 몽키패치해서 존재하지 않는 타입을 섞는다."""
        original = requirements._qa_agents._infer_notice_type

        def patched(context):
            path = ""
            if isinstance(context, dict):
                path = str(context.get("category_path") or "")
            # 시계의 첫 후보만 UNKNOWN_TYPE 으로 바꾼다.
            if "FURNITURE" in path or "가구" in path:
                return "UNKNOWN_TYPE_XX"
            return original(context)

        with mock.patch.object(requirements._qa_agents, "_infer_notice_type", side_effect=patched):
            result = requirements.diagnose({"name": "시계", "salePrice": 10000})
        nrf = result.get("notice_required_fields") or {}
        certain = nrf.get("certain") or []
        assert len(certain) == 0, (
            f"UNKNOWN 타입이 섞여도 certain 이 비지 않음: {len(certain)} — "
            f"{json.dumps([f['field'] for f in certain], ensure_ascii=False)}"
        )
