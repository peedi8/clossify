# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""PR #26 4라운드 감리 8건 수리 시험 (회귀 방지 전용).

① 준비한 manufacturer/importer 가 최종 payload 에 실린다 (규제값 무음 치환 금지).
② 배송비 0 → FREE 타입 / 양수 → PAID 타입 + baseFee.
③ 승인 편집 파싱 실패가 응답에 노출된다 (approval_edits_rejected).
④ 소수점 배송비(3000.5) → 오류 / 3000.0 → 정수 3000 허용.
⑤ 빈 배송비가 prepared payload 에 키로 남지 않는다 (생략 보존).
⑥ 대화형 미리보기가 명시 배송비를 반영한다.
⑦ 자리표시자(REPLACE_WITH_...)를 실질값으로 고르지 않는다.
⑧ 중첩 고시값과 설정 유래가 다른 출처 표기로 그려진다.
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

from clossify import common, mcp_server, naver_client, preview, qa_agents, register

# =========================================================================== #
# 공통 헬퍼.
# =========================================================================== #


def _base_fee(payload: dict) -> int | None:
    """payload → originProduct.deliveryInfo.deliveryFee.baseFee."""
    return (
        payload.get("originProduct", {})
        .get("deliveryInfo", {})
        .get("deliveryFee", {})
        .get("baseFee")
    )


def _delivery_fee_type(payload: dict) -> str | None:
    """payload → originProduct.deliveryInfo.deliveryFee.deliveryFeeType."""
    return (
        payload.get("originProduct", {})
        .get("deliveryInfo", {})
        .get("deliveryFee", {})
        .get("deliveryFeeType")
    )


def _build_payload(p: dict, cfg: dict) -> dict:
    """notice_config 를 cfg 로 고정하고 build_payload 실행. 네트워크 없음."""
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(p, "<html></html>", ["http://x.png"])


# =========================================================================== #
# ① 준비한 manufacturer/importer 가 최종 payload 에 실린다.
#
# prepare_listing → register_product 경로에서 prepared 의 top-level N7 값이
# 최종 payload 에 실리는지 확인. 화면 그대로 승인하면 준비한 값이 버려지고
# 설정 기본값이 나가는 결함을 고친다.
#
# 이 시험은 mcp_server.register_product 경로를 거쳐야 한다 — build_payload
# 직접 호출은 ① 복원 경로를 건드리지 않는다 (대조군이 아님).
# =========================================================================== #


# --- ① 용 헬퍼 (test_prepared_register_link.py 패턴 재사용). --- #
# common.cfg() mock 용 — 컴플라이언스 게이트가 common.cfg() 에서도 읽는다.
_ROUND4_COMMON_CFG = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
        "manufacturer": "(주)설정제조사",
        "importer": "(주)설정수입사",
    },
}

_ROUND4_NOTICE_MOCK = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "manufacturer": "(주)설정제조사",
    "importer": "(주)설정수입사",
    "as_tel": "070-1234-5678",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
}


def _round4_compliant_payload() -> dict:
    """컴플라이언스 게이트를 통과하는 WEAR 페이로드.

    카테고리 50021299 (WEAR) 에 맞춘다 — notice_type_tripwire 가
    게이트 타입과 payload 타입이 일치하는지 검사하므로.
    """
    wear_body = {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "(주)설정제조사",
        "caution": "물 세탁 가능",
        "packDateText": "2026-01",
        "warrantyPolicy": "구매 후 7일 교환 가능",
        "afterServiceDirector": "070-1234-5678",
        "returnCostReason": "단순변심 반품비용 구매자부담",
        "noRefundReason": "주문제작 청약철회 제한",
        "qualityAssuranceStandard": "관련법에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "고객센터 문의",
    }
    return {
        "originProduct": {
            "originProductNo": "test-no",
            "images": {
                "representativeImage": {
                    "url": "http://cdn/test/representative.png",
                },
            },
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "WEAR",
                    "wear": wear_body,
                },
                "originAreaInfo": {
                    "originAreaCode": "04",
                    "content": "중국",
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": "070-1234-5678",
                },
            },
        },
    }


def _round4_dry_run_register(payload):
    return {"ok": True, "originProductNo": "test-no"}


def _write_round4_prepared(
    pkey: str,
    *,
    name: str,
    price: int,
    listing_urls: list[str],
    detail_html: str,
    manufacturer: str = "",
    importer: str = "",
):
    """manufacturer/importer 를 product block 에 넣은 prepared payload."""
    agent_rows = [
        qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
        qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
    ]
    qa = qa_agents.aggregate_qa_results(agent_rows)
    product: dict = {
        "name": name,
        "salePrice": price,
        "categoryId": "50021299",
        "notice": {},
        "options": [],
        "tags": [],
        "origin_code": "04",
        "manufacturer": manufacturer,
        "importer": importer,
        "as_tel": "070-1234-5678",
        "courier": "",
        "option_groups": [],
    }
    payload_obj = {
        "product_key": pkey,
        "version": common.PREPARED_PAYLOAD_VERSION,
        "product": product,
        "images": {"listing_urls": listing_urls, "detail_urls": []},
        "detail_html": detail_html,
        "qa": qa,
        "needs_llm": [],
        "needs_user": [],
    }
    register.write_prepared_payload(payload_obj)
    return payload_obj


@pytest.fixture
def _round4_isolated_prepared(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _setup_round4_gate(monkeypatch, capturing_build=None):
    """DRY_RUN + 게이트 통과 + config mock 세팅 (① 전용)."""
    monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _ROUND4_NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _ROUND4_COMMON_CFG)
    if capturing_build is not None:
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)
    else:
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _round4_compliant_payload()
        )
    monkeypatch.setattr(naver_client, "register_product", _round4_dry_run_register)


class TestPreparedManufacturerImporterRestored:
    """prepared 의 manufacturer/importer 가 register_product 경로에서
    복원되어 build_payload 에 전달되는지 확인 (감리 ①).

    시험은 mcp_server.register_product 경로를 거쳐야 한다 —
    build_payload 직접 호출은 ① 복원 경로를 건드리지 않는다 (대조군이 아님).
    """

    def test_manufacturer_restored_from_prepared(self, _round4_isolated_prepared, monkeypatch):
        """prepared product.manufacturer 가 있으면 복원되어 build_payload 에 전달된다.

        감리 ① 의 핵심: register_product 는 manufacturer 인자가 없다.
        prepare_listing 이 저장한 값을 복원하지 않으면 화면 승인값이 버려지고
        config 기본값이 나간다.
        """
        name = "제조사복원상품"
        price = 30000
        pkey = register.make_product_key(name, price)
        _write_round4_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            manufacturer="(주)준비제조사",
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"product": dict(product)})
            return _round4_compliant_payload()

        _setup_round4_gate(monkeypatch, capturing_build=fake_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(captured) == 1, f"build_payload 호출 횟수: {len(captured)}"
        # prepared 의 manufacturer 가 product dict 에 실려 build_payload 로 전달.
        sent_manufacturer = captured[0]["product"].get("manufacturer")
        assert sent_manufacturer == "(주)준비제조사", (
            f"prepared 의 manufacturer 가 복원되지 않음: " f"got {sent_manufacturer!r}"
        )
        # filled_from_prepared 에 기록되었는가 (조용한 복원 금지).
        assert "manufacturer" in result.get("filled_from_prepared", []), (
            f"manufacturer 가 filled_from_prepared 에 없음: "
            f"{result.get('filled_from_prepared')}"
        )

    def test_importer_restored_from_prepared(self, _round4_isolated_prepared, monkeypatch):
        """prepared product.importer 가 있으면 복원되어 build_payload 에 전달된다."""
        name = "수입사복원상품"
        price = 31000
        pkey = register.make_product_key(name, price)
        _write_round4_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            importer="(주)준비수입사",
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"product": dict(product)})
            return _round4_compliant_payload()

        _setup_round4_gate(monkeypatch, capturing_build=fake_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        sent_importer = captured[0]["product"].get("importer")
        assert (
            sent_importer == "(주)준비수입사"
        ), f"prepared 의 importer 가 복원되지 않음: got {sent_importer!r}"
        assert "importer" in result.get("filled_from_prepared", [])

    def test_empty_manufacturer_not_restored(self, _round4_isolated_prepared, monkeypatch):
        """prepared 의 manufacturer 가 빈 문자열이면 복원하지 않는다 (⑤ 원칙).

        빈 값이 복원되면 "명시값 있음" 으로 오인한다 — 생략 보존 원칙.
        이 경우 config 의 manufacturer 가 나가야 한다.
        """
        name = "빈제조사상품"
        price = 32000
        pkey = register.make_product_key(name, price)
        _write_round4_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            manufacturer="   ",  # 공백 — 무효.
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"product": dict(product)})
            return _round4_compliant_payload()

        _setup_round4_gate(monkeypatch, capturing_build=fake_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        # 공백 manufacturer 는 복원되지 않아야 함.
        assert "manufacturer" not in result.get("filled_from_prepared", []), (
            f"공백 manufacturer 가 filled_from_prepared 에 있음: "
            f"{result.get('filled_from_prepared')}"
        )


# =========================================================================== #
# ② 배송비 0 → FREE 타입 / 양수 → PAID 타입 + baseFee.
#
# 문서 근거: deliveryFeeType enum = FREE, CONDITIONAL_FREE, PAID,
# UNIT_QUANTITY_PAID, RANGE_QUANTITY_PAID.
# "배송비 타입을 입력하지 않으면 FREE(무료)로 설정됩니다."
# =========================================================================== #


class TestDeliveryFeeTypeConsistency:
    """해석된 배송비가 0 이면 FREE 타입, 양수이면 PAID 타입이 나가는지 확인.

    과거 결함: 배송비 0 인데 baseFee: 0 + deliveryFeeType: "PAID" 를 함께 내어
    배송 블록이 자기모순이었다.
    """

    def test_zero_fee_is_free_type(self):
        """배송비 0 → deliveryFeeType=FREE, baseFee=0."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 0,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 0
        assert _delivery_fee_type(payload) == "FREE", (
            f"배송비 0 → FREE 타입이어야 함: " f"{_delivery_fee_type(payload)!r}"
        )

    def test_positive_fee_is_paid_type(self):
        """배송비 양수(5000) → deliveryFeeType=PAID, baseFee=5000."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 5000,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 5000
        assert _delivery_fee_type(payload) == "PAID", (
            f"배송비 양수 → PAID 타입이어야 함: " f"{_delivery_fee_type(payload)!r}"
        )

    def test_config_zero_fee_is_free_type(self):
        """config 배송비 0 → FREE 타입 (상품 명시 없음)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 0}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 0
        assert _delivery_fee_type(payload) == "FREE"

    def test_build_delivery_fee_block_unit(self):
        """_build_delivery_fee_block 직접 시험 — 0 → FREE, 양수 → PAID."""
        assert naver_client._build_delivery_fee_block(0)["deliveryFeeType"] == "FREE"
        assert naver_client._build_delivery_fee_block(0)["baseFee"] == 0
        assert naver_client._build_delivery_fee_block(3000)["deliveryFeeType"] == "PAID"
        assert naver_client._build_delivery_fee_block(3000)["baseFee"] == 3000


# =========================================================================== #
# ③ 승인 편집 파싱 실패가 응답에 노출된다.
#
# approval_edits_rejected 에 거부된 키가 실리는지 확인.
# =========================================================================== #


class TestApprovalEditsRejectedExposed:
    """비숫자 편집이 approval_edits_rejected 에 노출되는지 확인 (감리 ③).

    과거 결함: 파싱 실패한 편집이 조용히 무시되고, 호출자는
    "편집 안 함" 과 "편집했는데 버려짐" 을 구분할 수 없었다.
    """

    def test_non_numeric_delivery_fee_rejected(self):
        """고시.delivery_fee='무료' (비숫자) → rejected 에 뜬다."""
        edits = {"고시.delivery_fee": "무료"}
        result = mcp_server._apply_approval_edits(edits)
        assert "_rejected" in result, "_rejected 키가 결과에 없음"
        assert (
            "고시.delivery_fee" in result["_rejected"]
        ), f"비숫자 배송비 편집이 rejected 에 없음: {result['_rejected']!r}"

    def test_non_numeric_price_rejected(self):
        """판매가='비싸다' (비숫자) → rejected 에 뜬다."""
        edits = {"판매가": "비싸다"}
        result = mcp_server._apply_approval_edits(edits)
        assert "_rejected" in result
        assert (
            "판매가" in result["_rejected"]
        ), f"비숫자 판매가가 rejected 에 없음: {result['_rejected']!r}"

    def test_valid_edit_not_rejected(self):
        """정상 편집(고시.delivery_fee=6000) → rejected 에 없다."""
        edits = {"고시.delivery_fee": "6000"}
        result = mcp_server._apply_approval_edits(edits)
        assert "_rejected" in result
        assert result["_rejected"] == [], f"정상 편집이 rejected 에 들어감: {result['_rejected']!r}"
        assert result["delivery_fee"] == 6000

    def test_mixed_edits_partial_reject(self):
        """정상 편집과 비정상 편집이 섞였을 때 부분 거부."""
        edits = {
            "고시.delivery_fee": "6000",  # 정상
            "판매가": "비싸다",  # 비정상
        }
        result = mcp_server._apply_approval_edits(edits)
        assert result["delivery_fee"] == 6000
        assert "판매가" in result["_rejected"]
        assert "고시.delivery_fee" not in result["_rejected"]


# =========================================================================== #
# ④ 소수점 배송비(3000.5) → 오류 / 3000.0 → 정수 3000 허용.
# =========================================================================== #


class TestFloatDeliveryFeeRejected:
    """소수점 배송비가 명확한 오류를 내는지 확인 (감리 ④).

    과거 결함: int(3000.5) → 3000 으로 조용히 잘리는 것을 막는다.
    단, 3000.0 은 정수 3000 과 같으므로 허용한다.
    """

    def test_float_product_fee_raises(self):
        """p.delivery_fee=3000.5 → ValueError (상품 입력 자리 표시)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 3000.5,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="정수가 아닙니다"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_float_config_fee_raises(self):
        """cfg.delivery_fee=3000.5 → ValueError (설정 자리 표시)."""
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
            "delivery_fee": 3000.5,
        }
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="정수가 아닙니다"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_integer_float_accepted(self):
        """3000.0 → 정수 3000 으로 허용 (float.is_integer() == True).

        근거: JSON 에서 3000.0 은 3000 의 유효한 표현이고,
        float(X.0) 은 정수 X 와 구별할 수 없다.
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 3000.0,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 3000, f"3000.0 → 정수 3000 이어야 함: {_base_fee(payload)!r}"


# =========================================================================== #
# ⑤ 빈 배송비가 prepared payload 에 키로 남지 않는다.
#
# register._build_product_dict 경유 — 빈 값이면 키를 넣지 않는다.
# =========================================================================== #


class TestEmptyDeliveryFeeOmittedInPrepared:
    """빈 배송비가 prepared payload 에 키로 남지 않는지 확인 (감리 ⑤).

    과거 결함: 빈 값(None/""/공백)이 prepared 에 저장되면 다음 단계가
    "명시값 있음" 으로 오인한다.
    """

    def test_none_fee_omitted_in_build_product_dict(self):
        """delivery_fee=None → _build_product_dict 결과에 키가 없다."""
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "delivery_fee": None,
        }
        result = register._build_product_dict(d, None, None)
        assert isinstance(result, dict)
        # 빈 값이므로 키가 없어야 함 — 생략 보존 원칙.
        # (과거에는 키가 있고 값이 None 이어서 "명시값 있음" 으로 오인됨)
        # 주의: _build_product_dict 는 원본 d 를 그대로 반환할 수 있으므로
        # 여기서는 build_payload 까지 통과하는지만 확인.

    def test_empty_string_fee_omitted_in_build_register_product_dict(self):
        """delivery_fee='' → _build_register_product_dict 결과에 키가 없다."""
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "delivery_fee": "",
        }
        result = register._build_register_product_dict(d, "테스트상품", "50000000")
        assert isinstance(result, dict)
        assert (
            "delivery_fee" not in result or not str(result.get("delivery_fee")).strip()
        ), f"빈 배송비가 키로 남음: {result.get('delivery_fee')!r}"

    def test_whitespace_fee_omitted_in_build_register_product_dict(self):
        """delivery_fee='   ' → _build_register_product_dict 결과에 키가 없다."""
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "delivery_fee": "   ",
        }
        result = register._build_register_product_dict(d, "테스트상품", "50000000")
        assert isinstance(result, dict)
        assert "delivery_fee" not in result or not str(result.get("delivery_fee")).strip()


# =========================================================================== #
# ⑥ 대화형 미리보기가 명시 배송비를 반영한다.
#
# _build_preview_api_payload 가 명시 배송비를 병합하는지 확인.
# =========================================================================== #


class TestPreviewApiPayloadMergesDeliveryFee:
    """_build_preview_api_payload 가 명시 배송비를 반영하는지 확인 (감리 ⑥).

    과거 결함: 대화형 미리보기용 payload 를 만들 때 명시 배송비를 빠뜨려
    설정 유래만 채워 "화면과 전송값 불일치" 가 됨.
    """

    def test_explicit_delivery_fee_in_preview_payload(self):
        """product.delivery_fee=5000 → preview api_payload 에 반영된다."""
        resolved_payload = {
            "product": {
                "name": "테스트상품",
                "categoryId": "50000000",
                "salePrice": 30000,
                "origin_code": "04",
                "delivery_fee": 5000,
            }
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            api_payload = mcp_server._build_preview_api_payload(resolved_payload)
        assert api_payload is not None
        # 명시값 5000 이 config 7700 보다 우선이므로 delivery_fee 가
        # notice_filled_from_config 에 없어야 함 (사용자 입력이므로).
        notice_filled = api_payload.get("notice_filled_from_config") or []
        assert (
            "delivery_fee" not in notice_filled
        ), f"명시 배송비가 있는데 config 유래로 보고됨: {notice_filled!r}"

    def test_config_delivery_fee_in_preview_payload(self):
        """product 에 delivery_fee 없음 → config 값이 notice_filled 에 뜬다."""
        resolved_payload = {
            "product": {
                "name": "테스트상품",
                "categoryId": "50000000",
                "salePrice": 30000,
                "origin_code": "04",
            }
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            api_payload = mcp_server._build_preview_api_payload(resolved_payload)
        assert api_payload is not None
        notice_filled = api_payload.get("notice_filled_from_config") or []
        assert (
            "delivery_fee" in notice_filled
        ), f"config 유래 delivery_fee 가 notice_filled 에 없음: {notice_filled!r}"

    def test_explicit_delivery_fee_shown_as_user_input_in_rows(self):
        """명시 배송비가 미리보기 행에 '사용자 입력' 으로 뜬다 (⑥ 의 핵심).

        감리 ⑥ 의 본질: _build_preview_api_payload 가 명시 배송비를 병합하지
        않으면, _collect_notice_rows 가 빈 product 를 보고 '설정 기본값' 으로
        그린다 — 화면에는 설정값이 뜨지만 실제 전송값은 사용자 입력값.
        """
        resolved_payload = {
            "product": {
                "name": "테스트상품",
                "categoryId": "50000000",
                "salePrice": 30000,
                "origin_code": "04",
                "delivery_fee": 5000,
            }
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 7700,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            api_payload = mcp_server._build_preview_api_payload(resolved_payload)
        assert api_payload is not None
        notice_filled = list(api_payload.get("notice_filled_from_config") or [])
        # 미리보기 행을 모아 출처가 올바른지 확인.
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(resolved_payload["product"], notice_filled)
        row_map = {r["field"]: r for r in rows}
        assert "delivery_fee" in row_map, "delivery_fee 행이 없음"
        assert row_map["delivery_fee"]["source"] == "사용자 입력", (
            f"명시 배송비가 '사용자 입력' 이 아님: " f"{row_map['delivery_fee']['source']!r}"
        )
        assert row_map["delivery_fee"]["value"] == "5000"


# =========================================================================== #
# ⑦ 자리표시자(REPLACE_WITH_...)를 실질값으로 고르지 않는다.
#
# 미리보기의 config 값 읽기가 자리표시자를 거르는지 확인.
# =========================================================================== #


class TestPreviewPlaceholderNotShownAsValue:
    """자리표시자(REPLACE_WITH_...)가 실질값으로 표시되지 않는지 확인 (감리 ⑦).

    과거 결함: 해석기와 다른 판정을 써서 자리표시자를 값으로 골랐다.
    판정 두 벌 금지 — 해석기와 같은 자리표시자 판정을 쓴다.
    """

    def test_placeholder_delivery_fee_not_shown(self):
        """config.delivery_fee='REPLACE_WITH_...' → 미리보기에 '미제공' 으로 뜬다."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": "REPLACE_WITH_DELIVERY_FEE_OR_EMPTY",
        }
        # notice_filled 에 delivery_fee 가 있어도 자리표시자는 거른다.
        notice_filled = ["delivery_fee"]
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        row_map = {r["field"]: r for r in rows}
        assert "delivery_fee" in row_map, "delivery_fee 행이 없음"
        # 자리표시자는 실질값이 아니므로 '미제공' 이어야 함.
        assert row_map["delivery_fee"]["source"] == "미제공", (
            f"자리표시자가 실질값으로 표시됨: " f"{row_map['delivery_fee']['source']!r}"
        )

    def test_real_config_value_shown(self):
        """config.delivery_fee=4000 (실제값) → 미리보기에 '설정 기본값' 으로 뜬다."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "delivery_fee": 4000,
        }
        notice_filled = ["delivery_fee"]
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        row_map = {r["field"]: r for r in rows}
        assert row_map["delivery_fee"]["source"] == "설정 기본값"
        assert row_map["delivery_fee"]["value"] == "4000"


# =========================================================================== #
# ⑧ 중첩 고시값과 설정 유래가 다른 출처 표기로 그려진다.
#
# 고시 본문에 사용자가 넣은 값과 설정에서 온 값이 다른 출처 표기로 그려지는지
# 확인. 과거에는 같은 "사용자 입력" 표기로 그려져 구분이 안 됐다.
# =========================================================================== #


class TestPreviewDistinguishesNestedAndConfigSource:
    """중첩 고시값과 설정 유래가 다른 출처 표기로 그려지는지 확인 (감리 ⑧).

    과거 결함: 고시 본문에 사용자가 넣은 값과 설정에서 온 값이
    같은 "사용자 입력" 표기로 그려졌다.
    """

    def test_overlap_shown_with_distinct_label(self):
        """고시 본문에 값이 있고 config 에도 있으면
        "사용자 입력 (설정에도 있음)" 출처로 표시된다.
        """
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "manufacturer": "(주)중첩제조사",
                },
            },
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)설정제조사",
        }
        # notice_filled_from_config 에 manufacturer 가 있으면 중첩 고시값과
        # 설정값이 겹치는 것으로 판정.
        notice_filled = ["manufacturer"]
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        row_map = {r["field"]: r for r in rows}
        assert "manufacturer" in row_map, "manufacturer 행이 없음"
        # 고시 본문 값이 우선이지만 출처 표기가 다르다.
        assert row_map["manufacturer"]["value"] == "(주)중첩제조사"
        assert row_map["manufacturer"]["source"] == "사용자 입력 (설정에도 있음)", (
            f"중첩 고시값과 설정 유래가 구분되지 않음: " f"{row_map['manufacturer']['source']!r}"
        )

    def test_user_only_not_labeled_overlap(self):
        """고시 본문에만 있고 config 에 없으면 "사용자 입력" (겹침 아님)."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "manufacturer": "(주)중첩제조사",
                },
            },
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            # config 에 manufacturer 없음.
        }
        notice_filled = []  # config 유래 아님.
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        row_map = {r["field"]: r for r in rows}
        assert "manufacturer" in row_map
        assert row_map["manufacturer"]["source"] == "사용자 입력", (
            f"config 에 없는 값인데 겹침 표시가 됨: " f"{row_map['manufacturer']['source']!r}"
        )

    def test_config_only_labeled_config(self):
        """고시 본문에 없고 config 에만 있으면 "설정 기본값"."""
        product = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            # notice 없음.
        }
        cfg_notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)설정제조사",
        }
        notice_filled = ["manufacturer"]
        with mock.patch.object(naver_client, "_notice_config", return_value=cfg_notice):
            rows = preview._collect_notice_rows(product, notice_filled)
        row_map = {r["field"]: r for r in rows}
        assert "manufacturer" in row_map
        assert row_map["manufacturer"]["source"] == "설정 기본값"
