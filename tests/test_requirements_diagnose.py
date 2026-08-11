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
        """``certain`` 은 WEAR ∩ ETC 교집합이어야 한다 (7개).

        합집합(13개) 이 아니라 교집합이다 — 안전한 필드만 요구한다.
        """
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain = nrf.get("certain") or []
        certain_count = len(certain)
        # WEAR 전체 필드 수보다 적어야 한다 (합집합이 아님을 증명).
        assert certain_count < 13, f"certain 이 13개 이상 — 합집합일 수 있음: {certain_count}"
        # 교집합이므로 7이어야 한다 (acceptance 표 기대값).
        assert certain_count == 7, (
            f"certain 개수가 7이 아님: {certain_count} — "
            f"{json.dumps([f['field'] for f in certain], ensure_ascii=False)}"
        )

    def test_certain_fields_in_both_wear_and_etc(self):
        """``certain`` 의 모든 필드가 WEAR 와 ETC 양쪽에 있어야 한다 (교집합 증명)."""
        from clossify import listing_templates

        wear_fields = set(listing_templates._notice_type_fields_for("WEAR"))
        etc_fields = set(listing_templates._notice_type_fields_for("ETC"))
        result = mcp_server.prepare_listing(
            {"name": "여성 반팔 티셔츠 면 100%", "salePrice": 19900}
        )
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain_fields = {f["field"] for f in nrf.get("certain") or []}
        assert (
            certain_fields <= wear_fields
        ), f"certain 에 WEAR 에 없는 필드가 있음: {certain_fields - wear_fields}"
        assert (
            certain_fields <= etc_fields
        ), f"certain 에 ETC 에 없는 필드가 있음: {certain_fields - etc_fields}"

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
        """certain 은 FOOD ∩ FASHION_ITEMS 교집합 (6개)."""
        result = mcp_server.prepare_listing({"name": "유기농 아몬드 500g", "salePrice": 12000})
        req = result.get("requirements") or {}
        nrf = req.get("notice_required_fields") or {}
        certain = nrf.get("certain") or []
        assert len(certain) == 6, (
            f"certain 개수가 6이 아님: {len(certain)} — "
            f"{json.dumps([f['field'] for f in certain], ensure_ascii=False)}"
        )

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
