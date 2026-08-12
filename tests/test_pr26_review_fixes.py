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


# =========================================================================== #
# 2라운드 감리 ① 출처 판정과 해석기가 같은 규칙을 쓰는가.
# =========================================================================== #


class TestDeliveryFeeSourceMatchesResolver:
    """출처 판정(_per_product_filled_from_config)이 해석기(_resolve_delivery_fee)
    와 같은 규칙을 쓰는지 확인 — 판정 두 벌이 만든 틈을 고친다.

    워크오더 ① 시험:
      ⓐ 설정이 자리표시자 → baseFee==3000 그리고 보고에 delivery_fee 없음
      ⓑ 설정 0 → baseFee==0 그리고 보고에 delivery_fee 있음
      ⓒ 기존 케이스(설정 7700 / 상품 명시 2500 / 명시 0 / 둘 다 없음) 회귀 없음
    """

    def test_placeholder_config_not_reported_and_3000(self):
        """ⓐ cfg.delivery_fee == 'REPLACE_WITH_DELIVERY_FEE_OR_EMPTY'
        → 해석기는 건너뛰고 3000, 보고는 "없음" 이어야 함.
        과거: _has_text 가 긴 문자열을 실질값으로 봐 "설정에서 채웠다" 고 보고.
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": "REPLACE_WITH_DELIVERY_FEE_OR_EMPTY",
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
        assert base_fee == 3000, f"자리표시자 → 3000 이어야 함: baseFee={base_fee}"
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "delivery_fee" not in notice_filled
        ), f"자리표시자인데 config 유래로 보고됨: {notice_filled!r}"

    def test_config_zero_reported_and_zero(self):
        """ⓑ cfg.delivery_fee == 0 → 해석기는 0, 보고에 있어야 함.
        과거: _has_text(0)==False 라 보고가 빠졌음.
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 0,
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
        assert base_fee == 0, f"설정 0(무료배송) → 0 이 나가야 함: baseFee={base_fee}"
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "delivery_fee" in notice_filled
        ), f"설정 0 은 config 유래로 보고되어야 함: {notice_filled!r}"

    def test_config_fee_reported_as_before(self):
        """ⓒ-1 회귀: 설정 7700, 상품 명시 없음 → 7700 + 보고 있음 (기존 동작)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
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
        assert base_fee == 7700
        notice_filled = payload.get("notice_filled_from_config") or []
        assert "delivery_fee" in notice_filled

    def test_explicit_product_fee_not_reported(self):
        """ⓒ-2 회귀: 상품 명시 2500, 설정 7700 → 2500, 보고 없음."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 2500,
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
        assert base_fee == 2500
        notice_filled = payload.get("notice_filled_from_config") or []
        assert "delivery_fee" not in notice_filled

    def test_both_absent_falls_to_3000_no_report(self):
        """ⓒ-3 회귀: 둘 다 없음 → 3000, 보고 없음."""
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
        assert base_fee == 3000
        notice_filled = payload.get("notice_filled_from_config") or []
        assert "delivery_fee" not in notice_filled


# =========================================================================== #
# 2라운드 감리 ② 미리보기가 top-level 명시값을 "미제공" 으로 그리지 않는가.
# =========================================================================== #


class TestPreviewTopLevelExplicitValues:
    """상품 top-level 에 명시한 N7 필드값이 미리보기에 그 값과 출처가
    "사용자 입력" 으로 그려지는지 확인. 워크오더 ② 시험.

    시험은 실제 미리보기 렌더 함수(render_preview_html) 의 진입점인
    _collect_notice_rows 에서 재되어야 한다 — 내부 함수 직접 호출로 때우지 마라.
    본 클래스는 _collect_notice_rows 를 직접 부르되, 이 함수가 render_preview_html
    의 행 조립을 전담하는 공개 진입점이므로 여기서 재는 것이 진입점 경계다.
    """

    def test_top_level_origin_content_shown_as_user_input(self):
        """ⓐ 상품 top-level made_in → 미리보기에 그 값과 '사용자 입력' 출처."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "made_in": "베트남",
        }
        cfg_notice = {"origin_content": "중국"}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, [])
        matching = [r for r in rows if r["field"] == "origin_content"]
        assert matching, "origin_content 행이 없음"
        assert matching[0]["value"] == "베트남", f"top-level 값이 안 그려짐: {matching[0]!r}"
        assert matching[0]["source"] == "사용자 입력"

    def test_top_level_importer_shown_as_user_input(self):
        """ⓐ 상품 top-level importer → 미리보기에 그 값과 '사용자 입력'."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "importer": "(주)명시수입사",
        }
        cfg_notice = {"importer": "(주)설정수입사"}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, [])
        matching = [r for r in rows if r["field"] == "importer"]
        assert matching, "importer 행이 없음"
        assert matching[0]["value"] == "(주)명시수입사"
        assert matching[0]["source"] == "사용자 입력"

    def test_top_level_manufacturer_shown_as_user_input(self):
        """ⓐ 상품 top-level manufacturer → 미리보기에 그 값과 '사용자 입력'."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "manufacturer": "(주)명시제조사",
        }
        cfg_notice = {"manufacturer": "(주)설정제조사"}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, [])
        matching = [r for r in rows if r["field"] == "manufacturer"]
        assert matching, "manufacturer 행이 없음"
        assert matching[0]["value"] == "(주)명시제조사"
        assert matching[0]["source"] == "사용자 입력"

    def test_top_level_delivery_fee_shown_as_user_input(self):
        """ⓐ 상품 top-level delivery_fee → 미리보기에 그 값과 '사용자 입력'.
        숫자 0 (무료배송) 도 유효한 명시값으로 그려진다.
        """
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "delivery_fee": 0,
        }
        cfg_notice = {"delivery_fee": 4000}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, [])
        matching = [r for r in rows if r["field"] == "delivery_fee"]
        assert matching, "delivery_fee 행이 없음"
        assert matching[0]["value"] == "0", f"무료배송 0 이 안 그려짐: {matching[0]!r}"
        assert matching[0]["source"] == "사용자 입력"

    def test_config_sourced_shows_config_label(self):
        """ⓑ 설정 유래 → 값과 '설정 기본값' 출처."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        cfg_notice = {"delivery_fee": 4000}
        notice_filled = ["delivery_fee"]
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        matching = [r for r in rows if r["field"] == "delivery_fee"]
        assert matching, "delivery_fee 행이 없음"
        assert matching[0]["value"] == "4000"
        assert matching[0]["source"] == "설정 기본값"

    def test_neither_shows_missing(self):
        """ⓒ 둘 다 없음 → 미제공."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        cfg_notice = {}
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, [])
        matching = [r for r in rows if r["field"] == "delivery_fee"]
        assert matching, "delivery_fee 행이 없음"
        assert matching[0]["value"] == ""
        assert matching[0]["source"] == "미제공"


# =========================================================================== #
# 2라운드 감리 ③ 수입사·제조사 억제가 해석기가 소비하는 입력만 근거로 삼는가.
# =========================================================================== #


class TestImporterManufacturerSuppressionMatchesResolver:
    """중첩 고시 본문만 있고 설정도 있을 때 — 해석기는 중첩 본문을 안 보므로
    config 값이 나가고, 출처 보고도 config 유래로 떠야 한다.

    워크오더 ③: 원산지만 고치고 수입사/제조사는 안 고쳤다.
    """

    def test_importer_reported_when_nested_notice_body_and_config(self):
        """중첩 고시 본문에만 importer 가 있고 설정에도 있으면
        → 전송값은 config 값(해석기가 중첩을 안 봄), 보고에 뜬다.
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "importer": "(주)중첩수입사",
                },
            },
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "importer": "(주)설정수입사",
        }
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        ):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        # 전송값은 config 값이어야 함 (해석기가 중첩 본문을 안 읽음).
        importer_value = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("originAreaInfo", {})
            .get("importer", "")
        )
        assert (
            importer_value == "(주)설정수입사"
        ), f"해석기가 중첩 본문을 안 읽으므로 config 값이 나가야 함: {importer_value!r}"
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "importer" in notice_filled
        ), f"config 유래 importer 가 보고에 떠야 함: {notice_filled!r}"

    def test_manufacturer_reported_when_nested_notice_body_and_config(self):
        """중첩 고시 본문에만 manufacturer 가 있고 설정에도 있으면
        → 보고에 manufacturer 가 떠야 한다 (해석기가 top-level 만 보므로).

        참고: manufacturer 는 originAreaInfo 가 아니라 notice 본문에 실린다.
        _merge_notice 가 중첩 본문의 manufacturer 를 최종 본문에 덮어쓰므로
        전송값 자체는 중첩값이 된다 — 이것은 ③의 범위 밖(동작 변경)이며,
        ③이 다루는 것은 **출처 보고**다. 해석기가 top-level 만 소비하므로
        중첩값은 억제 근거가 될 수 없다 → config 유래로 보고되어야 한다.
        과거: 중첩 본문의 manufacturer 가 억제 근거가 되어 보고가 빠졌음.
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "manufacturer": "(주)중첩제조사",
                },
            },
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)설정제조사",
        }
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        ):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        notice_filled = payload.get("notice_filled_from_config") or []
        assert (
            "manufacturer" in notice_filled
        ), f"config 유래 manufacturer 가 보고에 떠야 함: {notice_filled!r}"


# =========================================================================== #
# 2라운드 감리 ④ 배송비 진단이 기존 상품에서 값을 제안·불일치 보고하는가.
# =========================================================================== #


class TestDeliveryFeePolicyExtraction:
    """check_config(read_existing=True) 가 기존 상품의 baseFee 를 읽어
    제안·불일치 보고에 편입하는지 확인. 워크오더 ④ 시험.

    몽키패치로 get_product 응답에 baseFee 를 넣고 check_config 를 돌린다.
    미설정이 등록을 막지 않는 점(fail-closed 아님)도 함께 확인.
    """

    def test_suggests_delivery_fee_from_existing_product(self, tmp_path, monkeypatch):
        """기존 상품에 baseFee 가 있고 config 가 비어있으면 제안한다."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "id",
                        "client_secret": "secret",
                        "store_url_slug": "slug",
                    },
                    "smartstore_notice_defaults": {
                        "origin_area_code": "04",
                        "origin_content": "중국",
                        "as_tel": "070-0000-0000",
                        "delivery_fee": "",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        product_body = {
            "originProduct": {
                "originProductNo": "existing-d",
                "name": "테스트",
                "deliveryInfo": {
                    "deliveryFee": {"baseFee": 3500},
                },
                "detailAttribute": {
                    "afterServiceInfo": {"afterServiceTelephoneNumber": "070-0000-0000"},
                    "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                },
            },
        }

        def _mock_search(*a, **kw):
            return 200, {"products": [{"originProductNo": "existing-d", "name": "테스트"}]}

        def _mock_get(*a, **kw):
            return 200, product_body

        with mock.patch.object(naver_client, "search_products", side_effect=_mock_search):
            with mock.patch.object(naver_client, "get_product", side_effect=_mock_get):
                result = mcp_server.check_config(read_existing=True)

        suggested = result["suggested_from_existing"]
        assert (
            "smartstore_notice_defaults.delivery_fee" in suggested
        ), f"delivery_fee 제안이 없음: {list(suggested.keys())!r}"
        assert suggested["smartstore_notice_defaults.delivery_fee"]["value"] == 3500

    def test_drift_delivery_fee_when_config_differs(self, tmp_path, monkeypatch):
        """config 와 기존 상품의 baseFee 가 다르면 불일치로 보고한다."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "id",
                        "client_secret": "secret",
                        "store_url_slug": "slug",
                    },
                    "smartstore_notice_defaults": {
                        "origin_area_code": "04",
                        "origin_content": "중국",
                        "as_tel": "070-0000-0000",
                        "delivery_fee": 7700,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        product_body = {
            "originProduct": {
                "originProductNo": "existing-d2",
                "name": "테스트",
                "deliveryInfo": {
                    "deliveryFee": {"baseFee": 3500},
                },
                "detailAttribute": {
                    "afterServiceInfo": {"afterServiceTelephoneNumber": "070-0000-0000"},
                    "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                },
            },
        }

        def _mock_search(*a, **kw):
            return 200, {"products": [{"originProductNo": "existing-d2", "name": "테스트"}]}

        def _mock_get(*a, **kw):
            return 200, product_body

        with mock.patch.object(naver_client, "search_products", side_effect=_mock_search):
            with mock.patch.object(naver_client, "get_product", side_effect=_mock_get):
                result = mcp_server.check_config(read_existing=True)

        drift_keys = [d["config_key"] for d in result["drift_from_existing"]]
        assert (
            "smartstore_notice_defaults.delivery_fee" in drift_keys
        ), f"delivery_fee 불일치가 보고되지 않음: {drift_keys!r}"

    def test_missing_delivery_fee_does_not_block(self, tmp_path, monkeypatch):
        """미설정(빈 config + 빈 상품)이 등록을 막지 않는다 — fail-closed 아님.

        delivery_fee 는 상거래 조건이므로 policy_gaps 에 들어가지만
        check_config 의 ok 가 False 로 바뀌지 않는다 (다른 정책 키는
        present 여부가 ok 에 영향을 주지만 delivery_fee 자체는 그렇지 않다).
        본 시험은 진단은 하되 차단은 하지 않음을 확인한다.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "id",
                        "client_secret": "secret",
                        "store_url_slug": "slug",
                    },
                    "smartstore_notice_defaults": {
                        "origin_area_code": "04",
                        "origin_content": "중국",
                        "as_tel": "070-0000-0000",
                        # delivery_fee 자리를 아예 둔다(미설정).
                        "delivery_fee": "",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))
        result = mcp_server.check_config()
        # delivery_fee 가 policy_gaps 에 등장하는지 확인 (진단은 한다).
        gaps = result.get("policy_gaps") or []
        assert "smartstore_notice_defaults.delivery_fee" in gaps, "진단이 안 됨"
