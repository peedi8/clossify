# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""PR #26 감리 지적 6건 수리 시험.

① 설정 폴백이 실등록 경로에서 발동하는가 (진입점에서 재라).
② 명시적 무료배송(0)이 설정 유래로 잘못 보고되는가.
③ 고시 본문만 덮어썼을 때 원산지 출처 보고가 사라지는가.
④ 미리보기에 새 보고 필드가 보이는가.
⑤ 잘못된 배송비를 조용히 3000 으로 바꾸는가.
⑥ 설정 예시 파일에 delivery_fee 자리가 있는가.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client, preview

# 의류 카테고리 (KC 불필요, WEAR 고시 타입) — 컴플라이언스 게이트 통과용.
_CLOTHING_CATEGORY = "50021299"


def _wear_notice_override():
    """WEAR 필수 필드를 완비한 notice override 를 반환 (게이트 통과용)."""
    return {
        "productInfoProvidedNoticeType": "WEAR",
        "wear": {
            "material": "면 100%",
            "color": "블랙",
            "size": "FREE",
            "caution": "물 세탁 가능",
            "packDateText": "2024-01-01",
            "warrantyPolicy": "구매 후 7일 이내 교환 가능",
        },
    }


def _ctx_mocks(cfg_notice):
    """컴플라이언스 게이트가 WEAR 통과 판정을 내리도록 하는 context manager 스택.

    test_option_groups.py 의 동일 패턴 — common.cfg 와 _notice_config 양쪽을
    덮어쓴다 (게이트가 common.cfg 를 직접 읽기 때문).
    """
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common,
            "cfg",
            return_value={"smartstore_notice_defaults": cfg_notice},
        ),
    )


# =========================================================================== #
# ① 진입점 시험: register_product 를 fee 없이 호출 → 설정값이 실림.
# =========================================================================== #


class TestEntryDeliveryFeeFallback:
    """register_product 를 fee 인자 없이 호출했을 때 설정값이 baseFee 에 실리고
    notice_filled_from_config 에 delivery_fee 가 뜨는지 확인.

    워크오더 요구: "_notice_defaults 직접 호출로 때우지 마라 — 진입점에서 재라."
    """

    def test_register_product_no_fee_uses_config(self):
        """register_product(delivery_fee 생략) → config 값이 baseFee 에 실린다.

        캡처 mock 으로 송신 payload 를 잡는다 (test_option_groups 패턴).
        컴플라이언스 게이트 통과를 위해 WEAR notice 와 의류 카테고리를 쓴다.
        """
        captured_payload: dict = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-n1"})

        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
            "as_tel": "070-1234-5678",
            "manufacturer": "테스트제조사",
            "return_cost_reason": "단순변심 반품비용 구매자부담",
            "no_refund_reason": "주문제작 청약철회 제한",
            "quality_assurance_standard": "관련법에 따름",
            "compensation_procedure": "소비자분쟁해결기준",
            "trouble_shooting_contents": "고객센터 문의",
        }
        ctx1, ctx2, ctx3 = _ctx_mocks(cfg_notice)
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=capture):
                result = mcp_server.register_product(
                    name="테스트상품",
                    price=30000,
                    category_id=_CLOTHING_CATEGORY,
                    image_urls=["http://x.png"],
                    detail_html="<html></html>",
                    notice=_wear_notice_override(),
                    preview_confirmed=True,
                    # delivery_fee 를 주지 않는다 — config 폴백이 발동해야 함.
                )
        assert result.get("ok") is True, f"등록 실패: {result}"
        base_fee = (
            captured_payload.get("originProduct", {})
            .get("deliveryInfo", {})
            .get("deliveryFee", {})
            .get("baseFee")
        )
        assert (
            base_fee == 7700
        ), f"진입점에서 config 폴백이 발동해야 함: baseFee={base_fee} (expected 7700)"

    def test_register_product_no_fee_reports_config_source(self):
        """register_product(delivery_fee 생략) → 결과 메타에 delivery_fee 가 뜬다."""
        captured_payload: dict = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-n1-meta"})

        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
            "as_tel": "070-1234-5678",
            "manufacturer": "테스트제조사",
            "return_cost_reason": "단순변심 반품비용 구매자부담",
            "no_refund_reason": "주문제작 청약철회 제한",
            "quality_assurance_standard": "관련법에 따름",
            "compensation_procedure": "소비자분쟁해결기준",
            "trouble_shooting_contents": "고객센터 문의",
        }
        ctx1, ctx2, ctx3 = _ctx_mocks(cfg_notice)
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=capture):
                result = mcp_server.register_product(
                    name="테스트상품",
                    price=30000,
                    category_id=_CLOTHING_CATEGORY,
                    image_urls=["http://x.png"],
                    detail_html="<html></html>",
                    notice=_wear_notice_override(),
                    preview_confirmed=True,
                )
        notice_filled = (
            result.get("notice_filled_from_config")
            or captured_payload.get("notice_filled_from_config")
            or []
        )
        assert (
            "delivery_fee" in notice_filled
        ), f"진입점에서 config 유래 delivery_fee 가 보고에 없음: {notice_filled!r}"


# =========================================================================== #
# ② 명시적 무료배송(0)이 설정 유래로 잘못 보고되지 않는가.
# =========================================================================== #


class TestExplicitZeroNotConfigReported:
    """p.delivery_fee=0 + 설정 7700 → baseFee==0 그리고 보고에 없음."""

    def test_zero_not_reported_as_config(self):
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 0,
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        ):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        base_fee = (
            payload.get("originProduct", {})
            .get("deliveryInfo", {})
            .get("deliveryFee", {})
            .get("baseFee")
        )
        assert base_fee == 0, f"명시 0(무료배송) 이 나가야 함: baseFee={base_fee}"
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "delivery_fee" not in notice_filled
        ), f"명시 0 인데 config 유래로 보고됨: {notice_filled!r}"


# =========================================================================== #
# ③ 고시 본문만 덮어썼을 때 원산지 출처 보고가 사라지지 않는가.
# =========================================================================== #


class TestOriginReportWithNoticeBodyOnly:
    """상품에 top-level made_in/origin_content 가 없고, config 에 origin_content 가
    있고, 사용자가 notice.<node>.countryOfOrigin 을 준 경우 —
    originAreaInfo.content 는 설정값으로 나가면서 notice_filled_from_config 에
    origin_content 가 떠야 한다 (해석기가 중첩 본문을 안 읽으므로).
    """

    def test_origin_reported_when_only_notice_body_country(self):
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "countryOfOrigin": "베트남",
                },
            },
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
        }
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        ):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        # originAreaInfo.content 는 config 값(중국)이어야 함 — 해석기가 중첩 본문을
        # 읽지 않으므로 config 폴백이 적용됨.
        content = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("originAreaInfo", {})
            .get("content", "")
        )
        assert (
            content == "중국"
        ), f"해석기가 중첩 본문을 안 읽으므로 config 값이 나가야 함: content={content!r}"
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "origin_content" in notice_filled
        ), f"config 유래 origin_content 가 보고에 있어야 함: {notice_filled!r}"


# =========================================================================== #
# ④ 미리보기에 새 보고 필드가 보이는가.
# =========================================================================== #


class TestPreviewShowsN7Fields:
    """4 필드가 설정 유래일 때 미리보기 렌더 결과에 각각 한 줄씩 등장."""

    def test_all_four_fields_appear_in_preview(self):
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "베트남",
            "importer": "(주)수입사",
            "manufacturer": "(주)제조사",
            "delivery_fee": 4000,
        }
        notice_filled = ["origin_content", "importer", "manufacturer", "delivery_fee"]
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        row_fields = {r["field"] for r in rows}
        for field in ("origin_content", "importer", "manufacturer", "delivery_fee"):
            assert field in row_fields, f"{field} 가 미리보기 행에 없음: {row_fields!r}"
        # 각각 설정 기본값 출처로 표시되어야 함.
        for field in ("origin_content", "importer", "manufacturer", "delivery_fee"):
            matching = [r for r in rows if r["field"] == field]
            assert matching, f"{field} 행이 없음"
            assert (
                matching[0]["source"] == "설정 기본값"
            ), f"{field} 의 출처가 '설정 기본값' 이 아님: {matching[0]['source']!r}"


# =========================================================================== #
# ⑤ 잘못된 배송비를 조용히 3000 으로 바꾸지 않는가.
# =========================================================================== #


class TestInvalidDeliveryFeeRaises:
    """값이 있는데 숫자가 아님 → 오류. 값 없음 → 3000 (회귀 없음)."""

    def test_product_invalid_fee_raises(self):
        """p.delivery_fee='abc' → ValueError (상품 입력 자리 표시 포함)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": "abc",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="상품 입력"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_config_invalid_fee_raises(self):
        """cfg.delivery_fee='abc' → ValueError (설정 자리 표시 포함)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": "abc"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="설정"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_missing_fee_still_3000(self):
        """값 없음 → 3000 (회귀 없음)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        ):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        base_fee = (
            payload.get("originProduct", {})
            .get("deliveryInfo", {})
            .get("deliveryFee", {})
            .get("baseFee")
        )
        assert base_fee == 3000, f"값 없음 → 3000 이어야 함: baseFee={base_fee}"


# =========================================================================== #
# ⑥ 설정 예시 파일에 delivery_fee 자리가 있는가.
# =========================================================================== #


class TestConfigExampleHasDeliveryFee:
    """config.example.json 의 smartstore_notice_defaults 에 delivery_fee 가 있고,
    check_config 의 정책 키 목록에도 편입되어 있는지 확인.
    """

    def test_config_example_has_delivery_fee_key(self):
        config_path = _PROJECT_ROOT / "config.example.json"
        with open(config_path, encoding="utf-8") as f:
            doc = json.load(f)
        notice_defaults = doc.get("smartstore_notice_defaults") or {}
        assert (
            "delivery_fee" in notice_defaults
        ), "config.example.json 의 smartstore_notice_defaults 에 delivery_fee 키가 없음"
        # 값이 자리표시자여야 함 (실제 금액을 지어내지 마라).
        value = notice_defaults["delivery_fee"]
        assert isinstance(value, str), f"delivery_fee 값이 문자열 자리표시자여야 함: {value!r}"
        assert (
            "REPLACE" in value or value == ""
        ), f"delivery_fee 값이 자리표시자여야 함 (실제 금액 금지): {value!r}"

    def test_delivery_fee_in_policy_config_keys(self):
        """check_config 의 _POLICY_CONFIG_KEYS 에 delivery_fee 가 포함되어야 함."""
        policy_keys = mcp_server._POLICY_CONFIG_KEYS
        flattened = [".".join(path) for path in policy_keys]
        assert (
            "smartstore_notice_defaults.delivery_fee" in flattened
        ), f"delivery_fee 가 정책 키 인벤토리에 없음: {flattened!r}"
