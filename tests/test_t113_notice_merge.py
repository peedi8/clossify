# -*- coding: utf-8 -*-
"""T-113 — 사용자 고시 입력값 무음 폐기 제거 검증 테스트.

작업지시(T-113)가 요구하는 시나리오:
  1. ``wear.packDateText = "상세페이지 참조"`` → payload 의 ``wear`` 노드에
     그 값이 **그대로 존재** (결함의 직접 반례).
  2. 다른 임의 문자열(``"2026-01"`` 등)도 그대로 존재.
  3. 빈 문자열·공백만·None → 싣지 않음 (기존 동작 유지, 값이 없는 것과 있는 것 구분).
  4. 동일 결함이 같은 함수에 더 없는지 확인 (문자열 목록·길이 컷 등 조용한 폐기).
  5. E2E: 의류 카테고리 + WEAR 필수 필드 전부 제공(일부는 "상세페이지 참조") +
     config 원산지 일치 → ``register_product`` 가 **차단하지 않고** 등록 경로 진입
     (네트워크 monkeypatch 로 호출 사실 확인).

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로 네트워크 차단.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client  # noqa: E402


# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

_NOTICE_CFG_WITH_ORIGIN = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
    "importer": "테스트수입사",
}

# 의류 카테고리 (E2E 반례용).
_CLOTHING_CATEGORY = "50021299"


def _make_product(notice_type=None, node_key=None, body=None,
                  extra_product=None):
    """테스트용 상품 dict 를 만든다."""
    p = {
        "name": "테스트상품",
        "categoryId": "50000000",
        "salePrice": 30000,
        "origin_code": "04",
        "made_in": "중국",
    }
    if extra_product:
        p.update(extra_product)
    if notice_type:
        notice = {"productInfoProvidedNoticeType": notice_type}
        if node_key and body is not None:
            notice[node_key] = body
        p["notice"] = notice
    return p


def _build_notice(p):
    """naver_client._notice_defaults + _product_info_notice 를 호출."""
    with mock.patch.object(naver_client, "_notice_config",
                           return_value=_NOTICE_CFG_WITH_ORIGIN):
        with mock.patch.object(naver_client, "_kc_config",
                               return_value=({}, "")):
            defaults = naver_client._notice_defaults(p)
            return naver_client._product_info_notice(p, defaults)


# --------------------------------------------------------------------------- #
# 1. 오케 반례: "상세페이지 참조" 가 무음 폐기되지 않고 그대로 실린다.
# --------------------------------------------------------------------------- #
class TestSilentDiscardRemoved:
    """특정 문자열이라고 조용히 버리는 필터가 제거되었는가."""

    def test_packdatetext_detail_reference_is_kept(self):
        """``wear.packDateText = "상세페이지 참조"`` → wear 노드에 그대로 존재."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁",
                "packDateText": "상세페이지 참조",
                "warrantyPolicy": "구매 후 7일 교환",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert wear.get("packDateText") == "상세페이지 참조", (
            f"packDateText 가 무음 폐기됨: {wear.get('packDateText')!r}"
        )

    def test_detail_reference_no_space_variant_kept(self):
        """공백 없는 변형 ``상세페이지참조`` 도 그대로 실리는가."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁",
                "packDateText": "상세페이지참조",
                "warrantyPolicy": "구매 후 7일 교환",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert wear.get("packDateText") == "상세페이지참조", (
            f"packDateText(공백없음) 가 무음 폐기됨: {wear.get('packDateText')!r}"
        )

    def test_arbitrary_string_is_kept(self):
        """다른 임의 문자열(``"2026-01"``)도 그대로 존재하는가."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁",
                "packDateText": "2026-01",
                "warrantyPolicy": "구매 후 7일 교환",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert wear.get("packDateText") == "2026-01"

    def test_multiple_detail_reference_fields_kept(self):
        """여러 필드가 동시에 "상세페이지 참조" 여도 모두 보존되는가."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "상세페이지 참조",
                "color": "상세페이지 참조",
                "size": "상세페이지 참조",
                "caution": "상세페이지 참조",
                "packDateText": "상세페이지 참조",
                "warrantyPolicy": "상세페이지 참조",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        for field in ("material", "color", "size", "caution",
                      "packDateText", "warrantyPolicy"):
            assert wear.get(field) == "상세페이지 참조", (
                f"{field} 가 무음 폐기됨: {wear.get(field)!r}"
            )


# --------------------------------------------------------------------------- #
# 2. 빈 값·공백·None 은 싣지 않는다 (기존 동작 유지, 값 구분).
# --------------------------------------------------------------------------- #
class TestEmptyValuesStillOmitted:
    """빈 문자열·공백만·None 은 싣지 않는가 (기존 동작 유지)."""

    def test_empty_string_not_loaded(self):
        """빈 문자열은 싣지 않음."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
                "color": "",
                "size": "FREE",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert "color" not in wear, (
            f"빈 문자열 color 가 실림: {wear.get('color')!r}"
        )
        assert wear.get("material") == "면"

    def test_whitespace_only_not_loaded(self):
        """공백만 있는 값은 싣지 않음."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
                "color": "   ",
                "size": "FREE",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert "color" not in wear, (
            f"공백만 있는 color 가 실림: {wear.get('color')!r}"
        )

    def test_none_not_loaded(self):
        """None 은 싣지 않음."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
                "color": None,
                "size": "FREE",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert "color" not in wear, (
            f"None color 가 실림: {wear.get('color')!r}"
        )

    def test_value_present_vs_absent_distinction(self):
        """값이 있는 것과 없는 것을 구분하는가 (임의 문자열 vs 빈 문자열)."""
        p_present = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={"material": "면", "color": "블랙", "size": "FREE",
                  "caution": "물 세탁", "packDateText": "상세페이지 참조",
                  "warrantyPolicy": "보증"},
        )
        p_absent = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={"material": "면", "color": "블랙", "size": "FREE",
                  "caution": "물 세탁", "packDateText": "",
                  "warrantyPolicy": "보증"},
        )
        n_present = _build_notice(p_present)
        n_absent = _build_notice(p_absent)
        # 값이 있으면 실리고, 빈 문자열이면 실리지 않는다.
        assert n_present["wear"].get("packDateText") == "상세페이지 참조"
        assert "packDateText" not in n_absent["wear"]


# --------------------------------------------------------------------------- #
# 3. 같은 함수에 다른 무음 폐기가 없는지 확인.
# --------------------------------------------------------------------------- #
class TestNoOtherSilentDrops:
    """_merge_notice 에 다른 무음 폐기 경로가 없는지 확인."""

    def test_long_string_is_not_truncated(self):
        """길이 제한으로 조용히 자르거나 버리지 않는가."""
        long_text = "x" * 5000  # 비정상적으로 긴 문자열
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
                "color": "블랙",
                "size": "FREE",
                "caution": long_text,
                "packDateText": "2026-01",
                "warrantyPolicy": "보증",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        # 길이로 인해 버려지거나 잘리지 않고 그대로 실려야 함.
        assert wear.get("caution") == long_text

    def test_special_string_values_not_filtered(self):
        """특정 토큰(``N/A``, ``-`` 등)이라고 조용히 버리지 않는가.

        주의: ``_notice_field_missing`` (QA 게이트) 은 ``-``/``N/A`` 를
        "누락" 으로 간주하지만, 이 테스트가 검증하는 것은 **_merge_notice
        자체가 값을 버리지 않는가** 이다. 값이 들어가야 QA 가 검사할 수 있다.
        """
        for token in ("N/A", "해당없음", "-", "null", "None"):
            p = _make_product(
                notice_type="WEAR",
                node_key="wear",
                body={
                    "material": "면",
                    "color": "블랙",
                    "size": "FREE",
                    "caution": "물 세탁",
                    "packDateText": token,
                    "warrantyPolicy": "보증",
                },
            )
            notice = _build_notice(p)
            wear = notice["wear"]
            assert wear.get("packDateText") == token, (
                f"토큰 {token!r} 이 _merge_notice 에서 무음 폐기됨"
            )


# --------------------------------------------------------------------------- #
# 4. E2E: register_product 가 "상세페이지 참조" 포함 WEAR 완비를 차단하지 않는다.
# --------------------------------------------------------------------------- #
class TestRegisterProductE2E:
    """MCP register_product 가 "상세페이지 참조" 포함 WEAR 완비 시
    컴플라이언스 게이트를 통과하고 네이버 API 를 호출하는가."""

    def test_wear_with_detail_reference_passes_gate_and_calls_naver(self):
        """WEAR 필수 필드 전부 제공(일부 "상세페이지 참조") → 등록 경로 진입.

        작업지시 반례: 이전에는 ``packDateText="상세페이지 참조"`` 가
        _merge_notice 에서 버려져 컴플라이언스 검사가 "필수 필드 누락:
        packDateText" 로 등록을 차단했다. 본 테스트는 그 회귀가 없는지 확인.
        """
        naver_calls = []
        # WEAR 필수 필드 13종 전부 제공. 일부는 "상세페이지 참조".
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "상세페이지 참조",  # ← 결함의 직접 반례
                "warrantyPolicy": "구매 후 7일 교환 가능",
                "manufacturer": "테스트제조사",
                # 공통 5필드는 config 기본값으로 채워짐.
            },
        }
        with mock.patch.object(naver_client, "_notice_config",
                               return_value=_NOTICE_CFG_WITH_ORIGIN):
            with mock.patch.object(naver_client, "_kc_config",
                                   return_value=({}, "")):
                with mock.patch.object(
                    naver_client, "register_product",
                    side_effect=lambda *a, **k: (
                        naver_calls.append({"args": a, "kwargs": k})
                        or (200, {"originProductNo": "test-t113-1"})
                    )):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                        notice=notice_override,
                    )
        assert result["ok"] is True, (
            f"등록 실패(컴플라이언스 차단?): {result}"
        )
        assert result.get("blocked_by") is None, (
            f"컴플라이언스 게이트가 차단함: {result.get('violations')}"
        )
        assert len(naver_calls) == 1, (
            f"네이버 API 호출 횟수가 예상과 다름: {len(naver_calls)}"
        )

    def test_payload_carries_detail_reference_to_naver(self):
        """최종 payload 의 wear 노드에 "상세페이지 참조" 가 그대로 실리는가."""
        captured_payload = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-t113-2"})

        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "상세페이지 참조",
                "warrantyPolicy": "구매 후 7일 교환 가능",
            },
        }
        with mock.patch.object(naver_client, "_notice_config",
                               return_value=_NOTICE_CFG_WITH_ORIGIN):
            with mock.patch.object(naver_client, "_kc_config",
                                   return_value=({}, "")):
                with mock.patch.object(
                    naver_client, "register_product",
                    side_effect=capture):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                        notice=notice_override,
                    )
        assert result["ok"] is True
        notice = (
            captured_payload
            .get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        assert notice.get("productInfoProvidedNoticeType") == "WEAR"
        wear = notice.get("wear", {})
        assert wear.get("packDateText") == "상세페이지 참조", (
            f"최종 payload 의 wear.packDateText 가 누락/변경됨: "
            f"{wear.get('packDateText')!r}"
        )

    def test_blocked_when_required_field_truly_missing(self):
        """필수 필드가 진짜로 누락된 경우(빈 문자열)에는 여전히 차단하는가.

        이 테스트는 T-113 변경이 "필수 필드 누락 검사 자체를 무력화"하지
        않았음을 보장한다. 빈 문자열은 여전히 누락으로 처리되어야 한다.
        """
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                # packDateText 자체를 빈 문자열로 제공 → 누락 처리 → 차단.
                "packDateText": "",
                "warrantyPolicy": "구매 후 7일 교환 가능",
            },
        }
        with mock.patch.object(naver_client, "_notice_config",
                               return_value=_NOTICE_CFG_WITH_ORIGIN):
            with mock.patch.object(naver_client, "_kc_config",
                                   return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product"):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                        notice=notice_override,
                    )
        # 빈 문자열은 누락 → 컴플라이언스 차단.
        assert result["ok"] is False
        assert result.get("blocked_by") == "compliance", (
            f"빈 packDateText 가 컴플라이언스에 걸리지 않음: {result}"
        )


# --------------------------------------------------------------------------- #
# 5. 무동작·identity 금지 검증.
# --------------------------------------------------------------------------- #
class TestNoNoOp:
    """변경이 실제로 효과를 발휘하는가 (무동작/identity 아님)."""

    def test_merge_notice_runs_user_body_loop(self):
        """user_body 의 각 필드가 실제로 처리되는가."""
        # 직접 _merge_notice 를 호출해 사용자 입력 처리를 검증.
        default_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {"material": "기본소재", "returnCostReason": "기본사유"},
        }
        user_notice = {
            "wear": {
                "material": "사용자소재",
                "packDateText": "상세페이지 참조",
            },
        }
        merged = naver_client._merge_notice(default_notice, user_notice)
        wear = merged["wear"]
        # 사용자 입력이 기본값을 덮어쓰고, "상세페이지 참조" 도 실린다.
        assert wear.get("material") == "사용자소재"
        assert wear.get("packDateText") == "상세페이지 참조"
        # 기본값도 보존.
        assert wear.get("returnCostReason") == "기본사유"
