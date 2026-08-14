"""``requirements.diagnose`` 회귀 테스트 — 거부 정보량 증대 (v2).

본 테스트는 ``prepare_listing`` 거부 시점에 ``diagnose`` 가 반환하는 정보를
고정한다. acceptance 2·3·4·6 (이미지만 빠진 경우 · 이름도 없는 경우 · 갈리는
경우 · 진단이 본체를 죽이지 않는다) 을 회귀로 잡는다.

v2 변경: ``category.status`` 에 ``likely`` 가 추가되고,
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
# v2: status 는 "likely", likely_notice_type 은 "WEAR",
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

        v2: 티셔츠 케이스는 상위 후보가 WEAR·WEAR·ETC 이므로
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
        """``certain`` + ``certain_one_of`` 평탄화 합은 14 이어야 한다 (F1).

        리뷰 수정: 이제 **최고점 동점자 전부** 의 고시타입으로 교집합을 구한다.
        티셔츠 케이스는 최고점 동점자가 WEAR·WEAR (2개) 이므로 교집합 = WEAR 전체.
        정본 API의 packDate/packDateText 쌍은 one_of 로 편입되어 certain = 12,
        one_of = 2, 합계 = 14다.
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
        # 정본 API 갱신으로 WEAR의 날짜/직접입력 XOR 쌍이 one_of 에 편입돼 합계가 1 늘었다.
        assert total == 14, (
            f"certain+one_of 합이 14가 아님: {total} — "
            f"certain={json.dumps([f['field'] for f in certain], ensure_ascii=False)}, "
            f"one_of={one_of_flat}"
        )

    def test_certain_fields_in_top_tie_types(self):
        """``certain`` 의 모든 필드가 최고점 동점자 고시타입 전체에 있어야 한다.

        리뷰 수정: 최고점 동점자가 WEAR·WEAR 이므로 certain 은 WEAR 필드
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

        T2: ``certain``·``certain_one_of`` 빈 리스트, ``scope`` 는 ``universal_only``,
        ``candidate_groups`` 는 빈 리스트, ``unresolved_notice_types`` 도 빈 리스트.
        """
        result = mcp_server.prepare_listing({})
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields")
        assert isinstance(nrf, dict), f"notice_required_fields 가 dict 가 아님: {type(nrf)}"
        assert nrf.get("certain") == [], f"certain 이 빈 리스트가 아님: {nrf.get('certain')}"
        assert (
            nrf.get("certain_one_of") == []
        ), f"certain_one_of 가 빈 리스트가 아님: {nrf.get('certain_one_of')}"
        assert (
            nrf.get("scope") == "universal_only"
        ), f"scope 가 universal_only 가 아님: {nrf.get('scope')}"
        assert (
            nrf.get("is_complete") is False
        ), f"is_complete 가 False 가 아님: {nrf.get('is_complete')}"
        assert (
            nrf.get("candidate_groups") == []
        ), f"candidate_groups 가 빈 리스트가 아님: {nrf.get('candidate_groups')}"

    def test_full_response_json(self):
        result = mcp_server.prepare_listing({})
        print("\n=== Acceptance 3 JSON ===")
        print(json.dumps(result, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# Acceptance 4: 갈리는 경우 — 유기농 아몬드.
# v2: status 는 "likely" (최고점 후보들이 FOOD 로 같으므로).
#         likely_notice_type 은 "FOOD", certain 은 FOOD∩FASHION_ITEMS 교집합.
# --------------------------------------------------------------------------- #
class TestAmbiguousCategory:
    """``유기농 아몬드 500g`` 은 status 가 likely 여야 한다 (v2)."""

    def test_status_likely(self):
        """v2: 최고점 후보들이 FOOD 로 같다 → likely."""
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        assert (
            category.get("status") == "likely"
        ), f"status 가 likely 가 아님: {category.get('status')}"

    def test_notice_types_seen_contains_only_top_tied_food(self):
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        category = req.get("category") or {}
        types_seen = category.get("notice_types_seen") or []
        assert types_seen == ["FOOD"], f"최고점 동점자 외 타입이 섞임: {types_seen}"

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
        """certain + certain_one_of 평탄화 합은 21 이어야 한다 (F1/F4).

        리뷰 수정: 최고점 동점자가 FOOD 1개이므로 교집합 = FOOD 전체.
        FOOD 의 XOR 그룹(packDate/packDateText, expirationDate/expirationDateText,
        consumptionDate/consumptionDateText)은 certain_one_of 로 옮겨간다. 정본 API
        갱신으로 expirationDate 쌍이 더해져 certain=15, one_of 평탄화=6, 합계=21이다.
        """
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain = nrf.get("certain") or []
        one_of = nrf.get("certain_one_of") or []
        one_of_flat = [f["field"] for g in one_of for f in g]
        total = len(certain) + len(one_of_flat)
        # 정본 API 갱신으로 FOOD의 expirationDate XOR 쌍이 one_of 에 추가돼 합계가 2 늘었다.
        assert total == 21, (
            f"certain+one_of 합이 21이 아님: {total} — "
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
# 리뷰 수정 회귀 — F1 ~ F7 (wo-n19-review-fixes.md)
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
        ("여성 반팔 티셔츠 면 100%", {"likely", "confident"}, 14),  # WEAR packDate XOR 쌍 추가.
        ("유기농 아몬드 500g", {"likely"}, 21),  # FOOD expirationDate XOR 쌍 추가.
        ("남성 캐주얼 셔츠", {"ambiguous", "likely"}, 11),
        ("수분 크림 50ml", {"likely"}, 19),
        ("스테인리스 텀블러 500ml", {"likely"}, 17),
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


class TestN82CandidateRequirementContract:
    """카테고리 후보군과 고시 요구필드가 같은 우주를 쓰는지 고정한다."""

    CASES: ClassVar[list[str]] = [case[0] for case in TestF1TopTieAll.CASES]

    def test_returned_candidates_equal_all_max_score_ties(self):
        for name in self.CASES:
            raw, _ = requirements._candidates_from_title(name)
            max_score = max(c["score"] for c in raw)
            expected = [c for c in raw if c["score"] == max_score]
            result = requirements.diagnose({"name": name, "salePrice": 10000})
            # M1: candidates 순서가 고시타입 대표 우선으로 바뀌었으므로
            # 집합 비교로 전부 포함됨을 확인한다 (순서 무시).
            actual = result["category"]["candidates"]
            assert len(actual) == len(expected), name
            # 다중집합 비교: 같은 원소가 같은 개수만큼.
            expected_sorted = sorted(
                expected, key=lambda c: (c["category_id"], c["path"], c["score"])
            )
            actual_sorted = sorted(actual, key=lambda c: (c["category_id"], c["path"], c["score"]))
            assert actual_sorted == expected_sorted, name
            assert all(c["score"] == max_score for c in actual), name

    def test_watch_keeps_all_sixteen_ties_and_blocks_completion(self):
        result = requirements.diagnose({"name": "시계", "salePrice": 10000})
        category = result["category"]
        required = result["notice_required_fields"]
        assert category["status"] == "ambiguous"
        assert len(category["candidates"]) == 16
        assert category["notice_types_seen"] == ["FURNITURE", "JEWELLERY"]
        assert required["scope"] == "top_tied_candidates"
        assert required["is_complete"] is False
        assert required["completion_blocked_by"] == ["category_choice"]

    def test_tshirt_keeps_only_two_wear_ties(self):
        result = requirements.diagnose({"name": "여성 반팔 티셔츠 면 100%", "salePrice": 10000})
        category = result["category"]
        assert len(category["candidates"]) == 2
        assert category["notice_types_seen"] == ["WEAR"]
        assert "ETC" not in category["notice_types_seen"]

    def test_candidate_groups_reconstruct_every_notice_type(self):
        from clossify import listing_templates

        for name in self.CASES:
            result = requirements.diagnose({"name": name, "salePrice": 10000})
            required = result["notice_required_fields"]
            common_regular = {item["field"] for item in required["certain"]}
            common_xor = {item["field"] for clause in required["certain_one_of"] for item in clause}
            seen_types = []
            for group in required["candidate_groups"]:
                assert set(group) == {
                    "notice_type",
                    "candidates",
                    "additional",
                    "additional_one_of",
                }
                notice_type = group["notice_type"]
                seen_types.append(notice_type)
                regular = common_regular | {item["field"] for item in group["additional"]}
                xor = common_xor | {
                    item["field"] for clause in group["additional_one_of"] for item in clause
                }
                expected = set(listing_templates._notice_type_fields_for(notice_type))
                assert regular.isdisjoint(xor), (name, notice_type)
                assert regular | xor == expected, (name, notice_type)
            assert seen_types == list(dict.fromkeys(result["category"]["notice_types_seen"]))

    def test_explicit_inputs_are_complete_and_have_no_group_candidates(self):
        products = [
            {
                "name": "아무거나",
                "salePrice": 1,
                "notice": {"productInfoProvidedNoticeType": "JEWELLERY"},
            },
            {"name": "아무거나", "salePrice": 1, "categoryId": "50000803"},
        ]
        for product in products:
            required = requirements.diagnose(product)["notice_required_fields"]
            assert required["scope"] == "confirmed_category"
            assert required["is_complete"] is True
            assert required["completion_blocked_by"] == []
            assert all(group["candidates"] == [] for group in required["candidate_groups"])

    def test_strong_title_candidate_still_requires_user_choice(self):
        result = requirements.diagnose({"name": "전신거울", "salePrice": 1})
        category = result["category"]
        required = result["notice_required_fields"]
        assert category["status"] == "likely"
        assert category["notice_type"] is None
        assert category["needs_category_choice"] is True
        assert required["scope"] == "top_tied_candidates"
        assert required["is_complete"] is False
        assert required["completion_blocked_by"] == ["category_choice"]

    def test_unknown_notice_type_fails_closed_for_both_scopes(self):
        candidate = {"category_id": "x", "path": "x", "score": 1}
        for explicit, scope, blockers in [
            (False, "top_tied_candidates", ["category_choice", "unknown_notice_type"]),
            (True, "confirmed_category", ["unknown_notice_type"]),
        ]:
            required = requirements._build_notice_required_fields_block(
                [candidate],
                ["NO_SUCH_NOTICE_TYPE"],
                None,
                None,
                is_explicit_confirmed=explicit,
            )
            assert required["certain"] == []
            assert required["certain_one_of"] == []
            assert required["scope"] == scope
            assert required["is_complete"] is False
            assert required["completion_blocked_by"] == blockers
            assert required["unresolved_notice_types"] == ["NO_SUCH_NOTICE_TYPE"]

    def test_legacy_keys_and_docstring_remain(self):
        """T2: 삭제된 키는 없고, 남은 7키와 독스트링 안내가 살아있어야 한다."""
        required = requirements.diagnose({})["notice_required_fields"]
        # 삭제된 키가 더 이상 없어야 한다.
        removed = {
            "likely_type",
            "likely_extra",
            "likely_extra_one_of",
            "complete",
            "by_notice_type",
        }
        assert not (removed & set(required)), f"삭제돼야 할 키가 남음: {removed & set(required)}"
        doc = requirements.diagnose.__doc__ or ""
        assert "사용자에게" in doc and "고르게" in doc
        assert "is_complete=false" in doc and "완전한 요구목록이 아니" in doc
        mcp_doc = mcp_server.prepare_listing.__doc__ or ""
        assert "공통 안전 부분집합" in mcp_doc
        assert "is_complete=false" in mcp_doc
        assert "completion_blocked_by" in mcp_doc
        assert "카테고리 확정이 먼저" in mcp_doc


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


# =========================================================================== #
# T2 — 진단 계약의 중복 키를 하나로 정리한다 (wo-contract-keys.md)
# =========================================================================== #


class TestT2ContractKeysFixed:
    """``notice_required_fields`` 키가 정확히 7개임을 고정한다.

    하나 늘어도, 줄어도 실패한다. 다음에 또 두 벌이 되는 걸 막는 시험이다.
    """

    EXPECTED_KEYS: ClassVar[set[str]] = {
        "scope",
        "is_complete",
        "completion_blocked_by",
        "certain",
        "certain_one_of",
        "candidate_groups",
        "unresolved_notice_types",
    }

    def _nrf_keys(self, product):
        return set(requirements.diagnose(product)["notice_required_fields"].keys())

    def test_empty_product_keys_exactly_seven(self):
        keys = self._nrf_keys({})
        assert keys == self.EXPECTED_KEYS, (
            f"빈 입력 키가 7개가 아님: {sorted(keys)}\n"
            f"extra={sorted(keys - self.EXPECTED_KEYS)} "
            f"missing={sorted(self.EXPECTED_KEYS - keys)}"
        )

    def test_watch_product_keys_exactly_seven(self):
        keys = self._nrf_keys({"name": "시계", "salePrice": 1})
        assert keys == self.EXPECTED_KEYS, (
            f"시계 입력 키가 7개가 아님: {sorted(keys)}\n"
            f"extra={sorted(keys - self.EXPECTED_KEYS)} "
            f"missing={sorted(self.EXPECTED_KEYS - keys)}"
        )

    def test_explicit_category_id_keys_exactly_seven(self):
        keys = self._nrf_keys({"name": "x", "salePrice": 1, "categoryId": "50000803"})
        assert keys == self.EXPECTED_KEYS, (
            f"categoryId 입력 키가 7개가 아님: {sorted(keys)}\n"
            f"extra={sorted(keys - self.EXPECTED_KEYS)} "
            f"missing={sorted(self.EXPECTED_KEYS - keys)}"
        )

    def test_key_count_is_exactly_seven(self):
        """키 개수 자체가 7 이어야 한다 (하나 늘어도 줄어도 실패)."""
        keys = self._nrf_keys({"name": "시계", "salePrice": 1})
        assert len(keys) == 7, f"키 개수가 7이 아님: {len(keys)} ({sorted(keys)})"


class TestT2DocstringMatchesActualKeys:
    """``mcp_server.prepare_listing.__doc__`` 에서 언급된
    ``notice_required_fields.<키>`` 이름이 실제 반환 키 집합의 부분집합임을 단언.

    독스트링이 존재하지 않는 키를 설명하면 실패한다.
    """

    def _doc_mentioned_keys(self):
        """``prepare_listing`` 독스트링에서
        ``notice_required_fields.<키>`` 형태로 언급된 이름을 추출한다.
        """
        import re

        doc = mcp_server.prepare_listing.__doc__ or ""
        # ``notice_required_fields.scope`` 형태 매칭.
        pattern = re.compile(r"notice_required_fields\.([a-zA-Z_][a-zA-Z0-9_]*)")
        return set(pattern.findall(doc))

    def test_doc_keys_subset_of_actual(self):
        actual = set(
            requirements.diagnose({"name": "시계", "salePrice": 1})["notice_required_fields"].keys()
        )
        doc_keys = self._doc_mentioned_keys()
        # 독스트링이 최소 한 개 키를 언급해야 한다 (아예 없으면 시험 의미 없음).
        assert doc_keys, "prepare_listing 독스트링에 notice_required_fields.<키> 언급이 없음"
        extra = doc_keys - actual
        assert not extra, (
            f"독스트링이 실제 없는 키를 언급함: {sorted(extra)}\n" f"actual={sorted(actual)}"
        )


# =========================================================================== #
# 거부 진단에 컴플라이언스(KC · 원산지 · A/S) 를 포함한다 (wo-n76-compliance.md)
# =========================================================================== #


class TestN76KcStatus:
    """``compliance.kc.status`` 가 카테고리 상황별로 올바른 값을 반환한다."""

    def test_kc_required_for_laptop(self):
        """acceptance 1: ``categoryId=50000151`` (노트북) → ``"required"``."""
        r = requirements.diagnose({"name": "x", "salePrice": 1, "categoryId": "50000151"})
        assert (
            r["compliance"]["kc"]["status"] == "required"
        ), f"kc.status: {r['compliance']['kc']['status']}"

    def test_kc_not_required_for_tshirt(self):
        """acceptance 2: ``categoryId=50000803`` (티셔츠) → ``"not_required"``."""
        r = requirements.diagnose({"name": "x", "salePrice": 1, "categoryId": "50000803"})
        assert (
            r["compliance"]["kc"]["status"] == "not_required"
        ), f"kc.status: {r['compliance']['kc']['status']}"

    def test_kc_depends_on_category_for_watch(self):
        """acceptance 3: ``시계`` → ``"depends_on_category"``, 후보 합 16."""
        r = requirements.diagnose({"name": "시계", "salePrice": 1})
        kc = r["compliance"]["kc"]
        assert kc["status"] == "depends_on_category", f"kc.status: {kc['status']}"
        total = kc["kc_required_candidates"] + kc["kc_free_candidates"]
        assert total == 16, (
            f"kc_required + kc_free = {total} (expected 16) — "
            f"required={kc['kc_required_candidates']}, free={kc['kc_free_candidates']}"
        )

    def test_kc_unknown_for_empty(self):
        """acceptance 4: ``{}`` → ``"unknown"``."""
        r = requirements.diagnose({})
        assert (
            r["compliance"]["kc"]["status"] == "unknown"
        ), f"kc.status: {r['compliance']['kc']['status']}"

    def test_kc_note_has_do_not_fabricate_warning(self):
        """KC note 에 "지어내지 마라" 안내가 포함되어야 한다."""
        r = requirements.diagnose({"name": "x", "salePrice": 1, "categoryId": "50000151"})
        note = r["compliance"]["kc"]["note"]
        assert "지어내지 마라" in note, f"note 에 지어내지 마라 안내 없음: {note}"


class TestN76KcStatusUnknownCategoryId:
    """F1: 모르는 categoryId 를 "KC 면제"로 오판하지 않는다."""

    def test_unknown_category_id_is_unknown(self):
        """``categoryId="99999999"`` → ``kc.status="unknown"`` (not_required 아님)."""
        r = requirements.diagnose({"name": "x", "salePrice": 1, "categoryId": "99999999"})
        assert (
            r["compliance"]["kc"]["status"] == "unknown"
        ), f"kc.status: {r['compliance']['kc']['status']} (F1: unknown 이어야 함)"

    def test_known_kc_free_category_id_is_not_required(self):
        """``categoryId="50000803"`` (티셔츠, 알려진 ID) → ``not_required``."""
        r = requirements.diagnose({"name": "x", "salePrice": 1, "categoryId": "50000803"})
        assert r["compliance"]["kc"]["status"] == "not_required"


class TestN76KcUnresolvedCandidates:
    """F2: ``kc_unresolved_candidates`` 카운트 + 불명 후보만 있을 때 unknown."""

    def test_watch_has_kc_unresolved_candidates_key(self):
        """``시계`` 결과에 ``kc_unresolved_candidates`` 키가 있어야 한다."""
        r = requirements.diagnose({"name": "시계", "salePrice": 1})
        kc = r["compliance"]["kc"]
        assert "kc_unresolved_candidates" in kc, f"kc_unresolved_candidates 키 없음: {kc}"

    def test_watch_three_counts_sum_equals_judged_candidates(self):
        """세 카운트(required+free+unresolved) 합 == 판정에 쓴 후보 수(16)."""
        r = requirements.diagnose({"name": "시계", "salePrice": 1})
        kc = r["compliance"]["kc"]
        total = (
            kc["kc_required_candidates"] + kc["kc_free_candidates"] + kc["kc_unresolved_candidates"]
        )
        # 시계의 최고점 동점자는 16개
        assert total == 16, (
            f"세 카운트 합={total} (expected 16) — "
            f"required={kc['kc_required_candidates']}, "
            f"free={kc['kc_free_candidates']}, "
            f"unresolved={kc['kc_unresolved_candidates']}"
        )

    def test_all_unresolved_candidates_gives_unknown(self):
        """F2: 분류 가능한 후보가 0이면 ``status="unknown"``.

        남성장갑이 재현 안 되면 ``_build_compliance_block`` 을 직접 불러
        unresolved 만 있는 케이스를 재현한다.
        """
        # 남성장갑으로 먼저 시도한다.
        r = requirements.diagnose({"name": "남성장갑", "salePrice": 1})
        kc = r["compliance"]["kc"]
        classifiable = kc["kc_required_candidates"] + kc["kc_free_candidates"]
        if classifiable == 0 and kc["kc_unresolved_candidates"] >= 1:
            assert kc["status"] == "unknown", f"status: {kc['status']}"
            return
        # 재현이 안 되면 직접 unresolved 만 있는 후보로 _build_compliance_block 호출.
        # 모르는 category_id 를 가진 가짜 후보를 만든다.
        fake_candidates = [
            {"category_id": "99999999", "path": "", "score": 1},
            {"category_id": "99999998", "path": "", "score": 1},
        ]
        r2 = requirements._build_compliance_block(
            {"name": "x", "salePrice": 1}, {}, None, fake_candidates
        )
        kc2 = r2["kc"]
        assert kc2["status"] == "unknown", f"status: {kc2['status']}"
        assert (
            kc2["kc_unresolved_candidates"] >= 1
        ), f"kc_unresolved_candidates: {kc2['kc_unresolved_candidates']}"
        total2 = (
            kc2["kc_required_candidates"]
            + kc2["kc_free_candidates"]
            + kc2["kc_unresolved_candidates"]
        )
        assert total2 == 2, f"세 카운트 합={total2} (expected 2)"


class TestN76KcNoteFromCounts:
    """F3: note 가 실제 개수와 일치하는 문구를 만든다."""

    def test_watch_note_has_no_mixed_when_all_free_or_required(self):
        """시계(0/16/0) note 에 "섞"이라는 말이 없고 "대상이 아니다" 취지.

        시계 후보가 전부 free 이거나 전부 required 이면 "섞"이라는 말이 없어야 한다.
        (실제 시계 후보는 KC 대상이 아니므로 "대상이 아니다" 취지여야 한다.)
        """
        r = requirements.diagnose({"name": "시계", "salePrice": 1})
        kc = r["compliance"]["kc"]
        note = kc["note"]
        if kc["kc_required_candidates"] == 0 and kc["kc_free_candidates"] > 0:
            # 전부 free → "대상이 아니다" 취지
            assert "섞" not in note, f'전부 free 인데 "섞"이 note 에 있음: {note}'

    def test_laptop_note_says_required(self):
        """노트북ID note 는 required 확답."""
        r = requirements.diagnose({"name": "x", "salePrice": 1, "categoryId": "50000151"})
        note = r["compliance"]["kc"]["note"]
        assert "대상이다" in note, f"note 에 '대상이다' 없음: {note}"


class TestN76OriginQuadrants:
    """원산지 4분면 — (provided T/F x configured T/F/None) missing 포함 여부.

    F4: 원산지는 code_provided + content_provided 두 부분으로 분해된다.
    missing 에는 ``origin_code`` 와 ``origin_content`` 가 별도로 들어간다.
    """

    def _origin_fields_in_missing(self, product, flags):
        r = requirements.diagnose(product, config_flags=flags)
        return {m.get("field") for m in r.get("missing") or []} & {
            "origin_code",
            "origin_content",
        }

    def test_provided_t_configured_t(self):
        """상품에 code+content 모두 있으면 missing 에 없다."""
        assert not self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1, "origin_code": "KR", "made_in": "한국"},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": True,
            },
        )

    def test_provided_t_configured_f(self):
        """상품에 code+content 모두 있으면 config 미설정이어도 missing 에 없다."""
        assert not self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1, "origin_code": "KR", "made_in": "한국"},
            {
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )

    def test_provided_f_configured_t(self):
        """상품에 없고 config 에 있으면 missing 에 없다."""
        assert not self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": True,
            },
        )

    def test_provided_f_configured_f(self):
        """상품에 없고 config 에도 없으면 missing 에 **둘 다** 있다 (명시적 False)."""
        fields = self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert "origin_code" in fields, f"origin_code 가 missing 에 없음: {fields}"
        assert "origin_content" in fields, f"origin_content 가 missing 에 없음: {fields}"

    def test_provided_f_configured_none(self):
        """상품에 없고 configured=None(모름)이면 missing 에 없다."""
        assert not self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": None,
                "origin_content_configured": None,
                "as_configured": True,
            },
        )

    def test_provided_f_config_flags_none(self):
        """config_flags=None (전체 모름) 이면 missing 에 없다."""
        assert not self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1},
            None,
        )

    def test_code_only_configured_f_leaves_content_missing(self):
        """F4/R2 acceptance: ``origin_code`` 만 준 입력 + content_configured=False →
        missing 에 **content 쪽**이 남는다."""
        fields = self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1, "origin_code": "KR"},
            {
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert (
            "origin_content" in fields
        ), f"origin_code 만 주면 content 가 missing 에 남아야 함: {fields}"
        assert (
            "origin_code" not in fields
        ), f"origin_code 를 줬는데 code 가 missing 에 있음: {fields}"

    def test_code_plus_made_in_clears_both_from_missing(self):
        """F4 acceptance: ``made_in`` 까지 주면 missing 에서 사라진다."""
        fields = self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1, "origin_code": "KR", "made_in": "한국"},
            {
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert not fields, f"code+made_in 를 줘도 missing 에 원산지가 있음: {fields}"

    # R2 acceptance: (config 에 content 만, 상품에 code 만) → missing 없음.
    def test_r2_content_configured_product_code_only_no_missing(self):
        """R2: config 에 content 만 있고 상품에 code 만 있으면 missing 이 없다."""
        fields = self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1, "origin_code": "KR"},
            {
                "origin_code_configured": False,
                "origin_content_configured": True,
                "as_configured": True,
            },
        )
        assert not fields, f"R2: config content + product code 조합인데 missing 에 남음: {fields}"

    # R2 acceptance: (config 에 code 만, 상품 아무것도 없음) → content 쪽만 missing.
    def test_r2_code_configured_only_product_empty_content_missing(self):
        """R2: config 에 code 만 있고 상품에 아무것도 없으면 content 쪽만 missing."""
        fields = self._origin_fields_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": True,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert (
            "origin_content" in fields
        ), f"R2: code_configured + product empty → content 가 missing 에 있어야 함: {fields}"
        assert (
            "origin_code" not in fields
        ), f"R2: code_configured 인데 code 가 missing 에 있음: {fields}"


class TestN76AfterServiceQuadrants:
    """A/S 4분면 — (provided T/F x configured T/F/None) missing 포함 여부."""

    def _as_in_missing(self, product, flags):
        r = requirements.diagnose(product, config_flags=flags)
        return "as_tel" in {m.get("field") for m in r.get("missing") or []}

    def test_provided_t_configured_t(self):
        assert not self._as_in_missing(
            {"name": "x", "salePrice": 1, "as_tel": "02-123-4567"},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": True,
            },
        )

    def test_provided_t_configured_f(self):
        assert not self._as_in_missing(
            {"name": "x", "salePrice": 1, "as_tel": "02-123-4567"},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": False,
            },
        )

    def test_provided_f_configured_t(self):
        assert not self._as_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": True,
            },
        )

    def test_provided_f_configured_f(self):
        assert self._as_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": False,
            },
        )

    def test_provided_f_configured_none(self):
        """configured=None(모름)이면 missing 에 없다."""
        assert not self._as_in_missing(
            {"name": "x", "salePrice": 1},
            {
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": None,
            },
        )

    def test_seller_tel_only_is_provided(self):
        """F5: ``seller_tel`` 만 준 입력 → ``provided_in_product=True``, missing 에 없음."""
        r = requirements.diagnose(
            {"name": "x", "salePrice": 1, "seller_tel": "02-987-6543"},
            config_flags={
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": False,
            },
        )
        assert (
            r["compliance"]["after_service"]["provided_in_product"] is True
        ), f"seller_tel 이 있는데 provided_in_product=False: {r['compliance']['after_service']}"
        missing_fields = {m.get("field") for m in r.get("missing") or []}
        assert (
            "as_tel" not in missing_fields
        ), f"seller_tel 이 있는데 as_tel 이 missing 에 있음: {missing_fields}"


class TestN76Purity:
    """``diagnose`` 가 ``config_flags`` 없이 불러도 동작한다 (전부 None 취급)."""

    def test_works_without_config_flags(self):
        r = requirements.diagnose({"name": "x", "salePrice": 1})
        assert "compliance" in r
        # R2 — origin 은 code_configured 와 content_configured 로 분리됨.
        assert r["compliance"]["origin"]["code_configured"] is None
        assert r["compliance"]["origin"]["content_configured"] is None
        assert r["compliance"]["after_service"]["configured"] is None

    def test_compliance_block_structure(self):
        r = requirements.diagnose({"name": "x", "salePrice": 1})
        compliance = r["compliance"]
        assert set(compliance.keys()) == {"kc", "origin", "after_service"}
        assert set(compliance["kc"].keys()) == {
            "status",
            "kc_required_candidates",
            "kc_free_candidates",
            "kc_unresolved_candidates",
            "note",
        }
        # R2 — origin 은 code_configured/content_configured (단일 configured 폐기).
        assert set(compliance["origin"].keys()) == {
            "code_provided",
            "content_provided",
            "code_configured",
            "content_configured",
        }
        assert set(compliance["after_service"].keys()) == {
            "provided_in_product",
            "configured",
        }


class TestN76McpIntegration:
    """MCP 도구 실경로: ``prepare_listing`` 거부 응답에 ``compliance`` 가 실린다."""

    def test_rejection_response_has_compliance(self):
        """acceptance 7: 노트북 상품으로 거부 응답에 compliance 블록이 있어야 한다."""
        result = mcp_server.prepare_listing(product={"name": "노트북 15인치", "salePrice": 1})
        assert result["ok"] is False
        req = result.get("requirements") or {}
        compliance = req.get("compliance")
        assert compliance is not None, "compliance 키가 없음"
        assert "kc" in compliance
        assert "origin" in compliance
        assert "after_service" in compliance
        print("\n=== Acceptance 7: prepare_listing compliance ===")
        print(json.dumps(compliance, ensure_ascii=False, indent=2))


# =========================================================================== #
# 컴플라이언스 리뷰 수정 — F6/F7 (wo-n76-review-fixes.md)
# =========================================================================== #


class TestN76F6ConfigAbsentIsFalse:
    """F6: config 파일이 확실히 없으면 플래그가 False (None 이 아니다)."""

    def test_absent_config_returns_false_not_none(self):
        """``_build_config_flags`` — 파일 부재 → 성분별 False."""
        from clossify import naver_client

        fake_path = str(Path(_PROJECT_ROOT) / "nonexistent_test_config_zzz.json")
        with mock.patch.object(naver_client, "resolve_config_path", return_value=fake_path):
            flags = mcp_server._build_config_flags()
        assert flags is not None, "F6: 파일 부재인데 None (모름) 반환"
        assert flags == {
            "origin_code_configured": False,
            "origin_content_configured": False,
            "as_configured": False,
            "common_notice_configured": {
                "returnCostReason": False,
                "noRefundReason": False,
                "qualityAssuranceStandard": False,
                "compensationProcedure": False,
                "troubleShootingContents": False,
            },
        }, f"F6: 파일 부재 시 예상값과 다름: {flags}"
        print(f"\n=== F6: config absent → {flags} ===")


class TestN76F7ConfigSectionAliases:
    """F7: config 섹션 별칭 3종을 naver_client 와 같은 우선순위로 인식한다."""

    def test_notice_defaults_only_section_is_configured(self):
        """``notice_defaults`` 섹션만 있어도 configured=True (별칭 인식).

        R3 — 섹션 해석을 naver_client._notice_config() 호출로 공유한다.
        이 픽스처(notice_defaults 만 있음) 가 여전히 True 를 주는지 확인한다.
        """
        import tempfile

        from clossify import naver_client

        # notice_defaults 별칭만 있는 config 를 임시 파일로 만든다.
        cfg_content = {
            "notice_defaults": {
                "origin_area_code": "82",
                "origin_content": "한국",
                "as_tel": "02-123-4567",
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(cfg_content, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        try:
            with mock.patch.object(naver_client, "resolve_config_path", return_value=tmp_path):
                flags = mcp_server._build_config_flags()
            assert flags is not None, "F7: config 있는데 None 반환"
            # R2 — 성분별 플래그 확인.
            assert (
                flags["origin_code_configured"] is True
            ), f"F7: notice_defaults 별칭인데 origin_code_configured=False: {flags}"
            assert (
                flags["origin_content_configured"] is True
            ), f"F7: notice_defaults 별칭인데 origin_content_configured=False: {flags}"
            assert (
                flags["as_configured"] is True
            ), f"F7: notice_defaults 별칭인데 as_configured=False: {flags}"
            print(f"\n=== F7: notice_defaults alias → {flags} ===")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_product_notice_defaults_alias_also_works(self):
        """``product_notice_defaults`` (3순위 별칭) 도 인식된다."""
        import tempfile

        from clossify import naver_client

        cfg_content = {
            "product_notice_defaults": {
                "origin_area_code": "82",
                "origin_content": "한국",
                "as_tel": "02-999-8888",
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(cfg_content, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        try:
            with mock.patch.object(naver_client, "resolve_config_path", return_value=tmp_path):
                flags = mcp_server._build_config_flags()
            assert flags is not None, "F7: config 있는데 None 반환"
            assert (
                flags["origin_code_configured"] is True
            ), f"F7: product_notice_defaults 별칭 인식 실패: {flags}"
            assert (
                flags["origin_content_configured"] is True
            ), f"F7: product_notice_defaults content 별칭 인식 실패: {flags}"
            print(f"\n=== F7: product_notice_defaults alias → {flags} ===")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # R4 — config 루트가 dict 가 아니면 플래그 전부 None, 진단은 살아있어야 한다.
    def test_r4_non_dict_root_config_returns_none_flags(self):
        """R4: config 루트가 ``[]`` (비-dict) → 플래그 전부 None."""
        import tempfile

        from clossify import naver_client

        cfg_content = []  # list — not a dict
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(cfg_content, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        try:
            with mock.patch.object(naver_client, "resolve_config_path", return_value=tmp_path):
                flags = mcp_server._build_config_flags()
            assert flags is not None, "R4: 비-dict 루트인데 None 반환(진단 사망)"
            assert (
                flags["origin_code_configured"] is None
            ), f"R4: 비-dict 루트인데 origin_code_configured 가 None 이 아님: {flags}"
            assert (
                flags["origin_content_configured"] is None
            ), f"R4: 비-dict 루트인데 origin_content_configured 가 None 이 아님: {flags}"
            assert (
                flags["as_configured"] is None
            ), f"R4: 비-dict 루트인데 as_configured 가 None 이 아님: {flags}"
            print(f"\n=== R4: non-dict root → {flags} ===")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_r4_non_dict_root_diagnosis_stays_alive(self):
        """R4: 비-dict 루트 config 에서 _safe_diagnose 가 살아있어야 한다."""
        import tempfile

        from clossify import naver_client

        cfg_content = []
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(cfg_content, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        try:
            with mock.patch.object(naver_client, "resolve_config_path", return_value=tmp_path):
                result = mcp_server._safe_diagnose({"name": "x", "salePrice": 1})
            assert result is not None, "R4: 비-dict 루트에서 진단이 사망 (_safe_diagnose=None)"
            assert "missing" in result, f"R4: 진단에 missing 키가 없음: {result}"
            assert "compliance" in result, f"R4: 진단에 compliance 키가 없음: {result}"
            print(
                f"\n=== R4: non-dict root diagnosis alive: compliance keys={list(result['compliance'].keys())} ==="
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # R3 — 섹션 해석이 naver_client._notice_config() 호출을 사용하는지 확인.
    def test_r3_uses_naver_client_notice_config(self):
        """R3: _build_config_flags 가 naver_client._notice_config() 를 호출한다."""
        import tempfile

        from clossify import naver_client

        cfg_content = {
            "notice_defaults": {
                "origin_area_code": "82",
                "origin_content": "한국",
                "as_tel": "02-123-4567",
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(cfg_content, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        try:
            with mock.patch.object(naver_client, "resolve_config_path", return_value=tmp_path):
                with mock.patch.object(
                    naver_client,
                    "_notice_config",
                    wraps=naver_client._notice_config,
                    return_value={
                        "origin_area_code": "82",
                        "origin_content": "한국",
                        "as_tel": "02-123-4567",
                    },
                ) as spy:
                    flags = mcp_server._build_config_flags()
                    assert (
                        spy.called
                    ), "R3: _build_config_flags 가 naver_client._notice_config() 를 호출하지 않음"
                assert flags["origin_code_configured"] is True
                print(f"\n=== R3: _notice_config() called, flags={flags} ===")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # 환경 격리 반례 (wo-fix-config-seam.md acceptance 2).
    # 픽스처가 실제로 읽히는지 증명: 같은 패치를 없는 경로로 바꾸면
    # 결과가 반대(False) 가 되어야 한다. 이전 config_path 패치는
    # _notice_config() 가 load_config → resolve_config_path 를 직접
    # 부르므로 패치가 무효했고, 있든 없든 실제 설치 config 를 읽어
    # 우연히 통과했다. resolve_config_path 패치로 바꾼 뒤에는
    # 픽스처 경로가 실제로 열린다.
    def test_env_isolation_counterexample_fixture_vs_nonexistent(self):
        """tmp 픽스처 경로만 읽는지 반례로 증명한다.

        (1) notice_defaults 섹션이 있는 tmp 픽스처 → flags 전부 True.
        (2) 같은 패치를 없는 경로로 바꾸면 → flags 전부 False (반대 결과).
        두 결과가 달라야 패치가 실제로 경로를 바꿨다고 할 수 있다
        (환경 의존 통과가 아니다).
        """
        import tempfile

        from clossify import naver_client

        cfg_content = {
            "notice_defaults": {
                "origin_area_code": "82",
                "origin_content": "한국",
                "as_tel": "02-123-4567",
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(cfg_content, tmp, ensure_ascii=False)
            tmp_path = tmp.name

        nonexistent = str(Path(_PROJECT_ROOT) / "nonexistent_test_config_env_iso.json")
        try:
            # (1) 픽스처 경로 → True.
            with mock.patch.object(naver_client, "resolve_config_path", return_value=tmp_path):
                flags_fixture = mcp_server._build_config_flags()
            assert flags_fixture is not None, "픽스처 경로인데 None 반환"
            assert flags_fixture["origin_code_configured"] is True, flags_fixture
            assert flags_fixture["origin_content_configured"] is True, flags_fixture
            assert flags_fixture["as_configured"] is True, flags_fixture

            # (2) 없는 경로 → False (반대).
            with mock.patch.object(naver_client, "resolve_config_path", return_value=nonexistent):
                flags_absent = mcp_server._build_config_flags()
            assert flags_absent is not None, "없는 경로인데 None 반환"
            assert flags_absent == {
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": False,
                "common_notice_configured": {
                    "returnCostReason": False,
                    "noRefundReason": False,
                    "qualityAssuranceStandard": False,
                    "compensationProcedure": False,
                    "troubleShootingContents": False,
                },
            }, f"반례: 없는 경로인데 False 가 아님: {flags_absent}"

            # 두 결과가 달라야 패치가 실제로 경로를 바꾼 것이다.
            assert flags_fixture != flags_absent, (
                "환경 격리 실패: 픽스처와 없는 경로가 같은 결과 → "
                "패치가 무효하고 실제 config 를 읽고 있음"
            )
            print(f"\n=== env isolation: fixture={flags_fixture} absent={flags_absent} ===")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # R5 — 플레이스홀더 값을 "제공됨"으로 세지 않는다.
    def test_r5_placeholder_origin_content_stays_missing(self):
        """R5: ``origin_content="상세페이지 참조"`` → content_provided=False, missing 에 남음."""
        r = requirements.diagnose(
            {"name": "x", "salePrice": 1, "origin_content": "상세페이지 참조"},
            config_flags={
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert (
            r["compliance"]["origin"]["content_provided"] is False
        ), f"R5: placeholder content 가 content_provided=True 로 취급됨: {r['compliance']['origin']}"
        missing_fields = {m.get("field") for m in r.get("missing") or []}
        assert (
            "origin_content" in missing_fields
        ), f"R5: placeholder content 가 missing 에 없음: {missing_fields}"

    def test_r5_normal_origin_content_is_provided(self):
        """R5: ``origin_content="중국 OEM"`` → content_provided=True (정상 값)."""
        r = requirements.diagnose(
            {"name": "x", "salePrice": 1, "origin_content": "중국 OEM"},
            config_flags={
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert (
            r["compliance"]["origin"]["content_provided"] is True
        ), f"R5: 정상 content 가 content_provided=False 로 취급됨: {r['compliance']['origin']}"
        missing_fields = {m.get("field") for m in r.get("missing") or []}
        assert "origin_content" not in missing_fields

    # R1 — 조립기가 읽지 않는 키를 "제공됨"으로 세지 않는다.
    def test_r1_origin_area_code_only_in_product_not_provided(self):
        """R1: 상품에 ``origin_area_code`` 만 있으면 code_provided=False (조립기가 안 읽음)."""
        r = requirements.diagnose(
            {"name": "x", "salePrice": 1, "origin_area_code": "KR"},
            config_flags={
                "origin_code_configured": False,
                "origin_content_configured": False,
                "as_configured": True,
            },
        )
        assert (
            r["compliance"]["origin"]["code_provided"] is False
        ), f"R1: origin_area_code (config 측 키) 가 code_provided=True 로 취급됨: {r['compliance']['origin']}"

    def test_r1_customer_service_phone_number_only_in_product_not_provided(self):
        """R1: 상품에 ``customerServicePhoneNumber`` 만 있으면 as provided=False."""
        r = requirements.diagnose(
            {"name": "x", "salePrice": 1, "customerServicePhoneNumber": "02-111-2222"},
            config_flags={
                "origin_code_configured": True,
                "origin_content_configured": True,
                "as_configured": False,
            },
        )
        assert (
            r["compliance"]["after_service"]["provided_in_product"] is False
        ), f"R1: customerServicePhoneNumber (config 측 키) 가 provided=True 로 취급됨: {r['compliance']['after_service']}"
