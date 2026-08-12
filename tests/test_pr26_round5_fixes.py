# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""PR #26 5라운드 감리 6건 수리 시험 (회귀 방지 전용).

① 자리표시자(REPLACE_WITH_...)가 규제 신고값으로 전송되는 것을 막는다.
   - 설정 origin_content 가 자리표시자 → 전송 안 됨(거부) · 보고에도 안 뜸
   - 상품 쪽이 자리표시자 + 설정에 진짜 값 → 설정값이 전송되고 출처도 설정
   - importer·manufacturer 동일 확인
   - 정상값 회귀 없음
② 준비한 manufacturer/importer 가 다른 입력을 전부 명시해도 복원된다.
③ 빈 top-level 편집이 "보이게 거부" 된다.
④ 진입점(mcp_server.register_product)에서 소수점 배송비(3000.5)가 거부된다.
⑤ 진입점(register._build_register_product_dict)에서 소수점 배송비가 거부된다.
⑥ 불리언(True/False) 배송비가 정수로 강제 변환되지 않고 거부된다.
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

from clossify import common, mcp_server, naver_client, qa_agents, register

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


def _origin_content(payload: dict) -> str | None:
    """payload → originProduct.detailAttribute.originAreaInfo.content."""
    return (
        payload.get("originProduct", {})
        .get("detailAttribute", {})
        .get("originAreaInfo", {})
        .get("content")
    )


def _build_payload(p: dict, cfg: dict) -> dict:
    """notice_config 를 cfg 로 고정하고 build_payload 실행. 네트워크 없음."""
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(p, "<html></html>", ["http://x.png"])


# =========================================================================== #
# ① 자리표시자(REPLACE_WITH_...)가 규제 신고값으로 전송되는 것을 막는다.
#
# _first_value 와 _has_text 가 같은 자리표시자 판정을 쓰는지 확인.
# 값을 고르는 쪽과 출처를 말하는 쪽이 어긋나면 자리표시자가 규제값으로 나간다.
# =========================================================================== #


class TestPlaceholderNotSentAsRegulatoryValue:
    """자리표시자(REPLACE_WITH_...)가 규제 신고값으로 전송되지 않는지 확인 (감리 ①).

    과거 결함: _first_value 는 자리표시자를 빈 문자열이 아니므로 "실질값" 으로
    골랐지만, _has_text 는 자리표시자를 "미설정" 으로 봐서 보고가 빠졌다.
    """

    def test_a_config_origin_placeholder_rejected(self):
        """ⓐ 설정 origin_content 가 자리표시자 → 전송 안 됨(거부).

        _first_value 가 REPLACE_WITH_ORIGIN_CONTENT 를 골르지 않아야 한다.
        made_in 이 빈 문자열이 되면 _notice_defaults 가 ValueError 를 던진다
        (fail-closed 규율).
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "REPLACE_WITH_ORIGIN_CONTENT",
        }
        with pytest.raises(ValueError, match="원산지 설정이 필요합니다"):
            _build_payload(p, cfg)

    def test_a_config_origin_placeholder_not_reported(self):
        """ⓐ 출처 보고에도 자리표시자가 "config 값" 으로 뜨지 않는다."""
        p = {
            "name": "테스트상품",
        }
        cfg_notice = {"origin_content": "REPLACE_WITH_ORIGIN_CONTENT"}
        filled = naver_client._per_product_filled_from_config(p, cfg_notice, [])
        assert "origin_content" not in filled, f"자리표시자가 config 유래로 보고됨: {filled!r}"

    def test_b_product_placeholder_config_real_value_selected(self):
        """ⓑ 상품 쪽이 자리표시자 + 설정에 진짜 값 → 설정값이 전송되고 출처도 설정.

        역방향: 상품의 origin_content 가 자리표시자면 _first_value 가 건너뛰고
        config 의 진짜 값을 골라야 한다. 그리고 출처 보고도 "config" 라고 해야
        한다 (판정 두 벌 금지).
        """
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "origin_content": "REPLACE_WITH_ORIGIN_CONTENT",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "한국산",
        }
        payload = _build_payload(p, cfg)
        assert _origin_content(payload) == "한국산", (
            f"상품 자리표시자를 건너뛰고 config 값을 골라야 함: " f"{_origin_content(payload)!r}"
        )

    def test_b_product_placeholder_config_real_value_reported_as_config(self):
        """ⓑ 상품 쪽이 자리표시자일 때 config 값이 출처 보고에 "config" 로 뜬다."""
        p = {
            "name": "테스트상품",
            "origin_content": "REPLACE_WITH_ORIGIN_CONTENT",
        }
        cfg_notice = {"origin_content": "한국산"}
        filled = naver_client._per_product_filled_from_config(p, cfg_notice, [])
        assert "origin_content" in filled, f"config 의 진짜 값이 출처 보고에 없음: {filled!r}"

    def test_c_importer_placeholder_same_behavior(self):
        """ⓒ importer 도 같은 구조 — 자리표시자가 규제값으로 나가지 않는다."""
        # 설정 importer 가 자리표시자 → 빈 문자열이 선택됨 (전송 안 됨)
        p = {"name": "테스트상품", "importer": ""}
        cfg_notice = {"importer": "REPLACE_WITH_IMPORTER"}
        importer_val = naver_client._first_value(
            p.get("importer"), cfg_notice.get("importer"), default=""
        )
        assert importer_val == "", f"importer 자리표시자가 선택됨: {importer_val!r}"
        # 출처 보고에서도 제외
        filled = naver_client._per_product_filled_from_config(p, cfg_notice, [])
        assert "importer" not in filled

        # 역방향: 상품 자리표시자 + config 진짜 값 → config 값 선택
        p2 = {"name": "테스트상품", "importer": "REPLACE_WITH_IMPORTER"}
        cfg2 = {"importer": "(주)진짜수입사"}
        importer_val2 = naver_client._first_value(
            p2.get("importer"), cfg2.get("importer"), default=""
        )
        assert (
            importer_val2 == "(주)진짜수입사"
        ), f"importer 역방향: config 값을 안 고름: {importer_val2!r}"
        filled2 = naver_client._per_product_filled_from_config(p2, cfg2, [])
        assert "importer" in filled2

    def test_c_manufacturer_placeholder_same_behavior(self):
        """ⓒ manufacturer 도 같은 구조."""
        # 설정 manufacturer 가 자리표시자 → 빈 문자열
        p = {"name": "테스트상품", "manufacturer": ""}
        cfg_notice = {"manufacturer": "REPLACE_WITH_MANUFACTURER"}
        mfr = naver_client._seller_manufacturer_default(p, cfg_notice)
        assert mfr == "", f"manufacturer 자리표시자가 선택됨: {mfr!r}"
        filled = naver_client._per_product_filled_from_config(p, cfg_notice, [])
        assert "manufacturer" not in filled

    def test_d_normal_values_no_regression(self):
        """ⓓ 정상값 회귀 없음 — 자리표시자가 아닌 진짜 값은 그대로 전송된다."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "한국산",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(p, cfg)
        assert (
            _origin_content(payload) == "한국산"
        ), f"상품 명시값이 config 보다 우선해야 함: {_origin_content(payload)!r}"

    def test_first_value_uses_same_placeholder_judgment_as_has_text(self):
        """단일 진실 공급원 확인 — _first_value 와 _has_text 가 같은 판정을 쓴다."""
        placeholder = "REPLACE_WITH_ANYTHING"
        # _has_text 는 자리표시자를 False 로 본다.
        assert naver_client._has_text(placeholder) is False
        # _first_value 도 자리표시자를 건너뛴다.
        assert naver_client._first_value(placeholder, "fallback") == "fallback"


# =========================================================================== #
# ② 준비한 manufacturer/importer 가 다른 입력을 전부 명시해도 복원된다.
#
# N7 복원이 _needs_any 블록 밖에서 수행되는지 확인.
# =========================================================================== #


_ROUND5_COMMON_CFG = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
        "manufacturer": "(주)설정제조사",
        "importer": "(주)설정수입사",
    },
}

_ROUND5_NOTICE_MOCK = {
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


def _round5_compliant_payload() -> dict:
    """컴플라이언스 게이트를 통과하는 WEAR 페이로드."""
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


def _round5_dry_run_register(payload):
    return {"ok": True, "originProductNo": "test-no"}


def _write_round5_prepared(
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
def _round5_isolated_prepared(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _setup_round5_gate(monkeypatch, capturing_build=None):
    """DRY_RUN + 게이트 통과 + config mock 세팅 (② 전용)."""
    monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _ROUND5_NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _ROUND5_COMMON_CFG)
    if capturing_build is not None:
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)
    else:
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _round5_compliant_payload()
        )
    monkeypatch.setattr(naver_client, "register_product", _round5_dry_run_register)


class TestN7RestoredIndependentOfNeedsAny:
    """N7 복원이 _needs_any 와 무관하게 수행되는지 확인 (감리 ②).

    과거 결함: 호출자가 다른 입력을 전부 명시하면 _needs_any 가 거짓이 되어
    블록이 스킵되고, 복원 루프가 아예 안 돌았다.
    """

    def test_manufacturer_restored_when_all_inputs_explicit(
        self, _round5_isolated_prepared, monkeypatch
    ):
        """다른 입력을 전부 명시해도 prepared 의 manufacturer 가 복원된다.

        핵심: image_urls, detail_html, notice, tags, options, option_groups,
        deferred_notice_fields, delivery_fee 를 전부 명시하면 _needs_any 가
        거짓이 된다. 과거에는 이 경우 N7 복원이 스킵되었다.
        """
        name = "전부명시상품"
        price = 50000
        pkey = register.make_product_key(name, price)
        _write_round5_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            manufacturer="(주)준비제조사",
            importer="(주)준비수입사",
        )
        captured: list[dict] = []

        def fake_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"product": dict(product)})
            return _round5_compliant_payload()

        _setup_round5_gate(monkeypatch, capturing_build=fake_build)

        # 모든 optional 입력을 명시적으로 준다 — _needs_any = False.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            notice={},
            tags=[],
            options=[],
            option_groups=["사이즈"],
            delivery_fee=3000,
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(captured) == 1
        sent_manufacturer = captured[0]["product"].get("manufacturer")
        assert (
            sent_manufacturer == "(주)준비제조사"
        ), f"전부 명시해도 N7 복원이 되어야 함: got {sent_manufacturer!r}"

    def test_importer_restored_when_all_inputs_explicit(
        self, _round5_isolated_prepared, monkeypatch
    ):
        """다른 입력을 전부 명시해도 prepared 의 importer 가 복원된다."""
        name = "수입사전부명시"
        price = 51000
        pkey = register.make_product_key(name, price)
        _write_round5_prepared(
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
            return _round5_compliant_payload()

        _setup_round5_gate(monkeypatch, capturing_build=fake_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            notice={},
            tags=[],
            options=[],
            option_groups=["사이즈"],
            delivery_fee=3000,
            preview_confirmed=True,
        )
        assert result["ok"] is True
        sent_importer = captured[0]["product"].get("importer")
        assert (
            sent_importer == "(주)준비수입사"
        ), f"전부 명시해도 importer 복원이 되어야 함: got {sent_importer!r}"


# =========================================================================== #
# ③ 빈 top-level 편집이 "보이게 거부" 된다.
#
# 사용자가 값을 빈 문자열로 지우면 _rejected 에 뜬다.
# =========================================================================== #


class TestEmptyTopLevelEditRejected:
    """빈 top-level 편집(지우기)이 보이게 거부되는지 확인 (감리 ③).

    과거 결함: 빈 값 편집이 조용히 무시되어 이전 설정값이 그대로 신고되었다.
    """

    def test_empty_importer_edit_rejected(self):
        """고시.importer='' (지우기) → _rejected 에 '고시.importer' 가 뜬다."""
        edits = {"고시.importer": ""}
        result = mcp_server._apply_approval_edits(edits)
        assert "_rejected" in result
        assert (
            "고시.importer" in result["_rejected"]
        ), f"빈 importer 편집이 rejected 에 없음: {result['_rejected']!r}"

    def test_empty_manufacturer_edit_rejected(self):
        """고시.manufacturer='' (지우기) → _rejected 에 뜬다."""
        edits = {"고시.manufacturer": ""}
        result = mcp_server._apply_approval_edits(edits)
        assert "고시.manufacturer" in result["_rejected"]

    def test_empty_origin_content_edit_rejected(self):
        """고시.origin_content='' (지우기) → _rejected 에 뜬다."""
        edits = {"고시.origin_content": ""}
        result = mcp_server._apply_approval_edits(edits)
        assert "고시.origin_content" in result["_rejected"]

    def test_whitespace_edit_rejected(self):
        """공백만 있는 편집도 거부된다."""
        edits = {"고시.importer": "   "}
        result = mcp_server._apply_approval_edits(edits)
        assert "고시.importer" in result["_rejected"]

    def test_non_empty_edit_not_rejected(self):
        """정상값(비어있지 않은 값)은 거부되지 않는다."""
        edits = {"고시.importer": "(주)정상수입사"}
        result = mcp_server._apply_approval_edits(edits)
        assert "고시.importer" not in result.get(
            "_rejected", []
        ), f"정상값이 rejected 에 들어감: {result.get('_rejected')!r}"
        assert result.get("importer") == "(주)정상수입사"


# =========================================================================== #
# ④ 진입점(mcp_server.register_product)에서 소수점 배송비(3000.5)가 거부된다.
#
# 진입점에서 int() 로 깎지 않고 원값을 넘겨 정본 해석기가 거부하게 한다.
# =========================================================================== #


class TestEntryPointFloatDeliveryFeeRejected:
    """진입점 경로에서 소수점 배송비가 거부되는지 확인 (감리 ④).

    과거 결함: mcp_server.register_product 가 int(delivery_fee) 로 미리 깎아서
    _resolve_delivery_fee_with_slot 의 소수점 거부 가드가 볼 게 없었다.
    """

    def test_float_fee_rejected_at_register_product_entry(
        self, _round5_isolated_prepared, monkeypatch
    ):
        """register_product(delivery_fee=3000.5) → 거부 (진입점 경로).

        내부 함수 직접 호출로 때우지 않는다 — register_product 진입점을 거친다.
        build_payload mock 을 쓰지 않는다 — 정본 해석기의 검증이 일어나야 한다.
        """
        name = "소수배송비상품"
        price = 30000
        pkey = register.make_product_key(name, price)
        _write_round5_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
        )

        # build_payload 를 mock 하지 않음 — 정본 해석기가 float 를 잡는다.
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _ROUND5_NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _ROUND5_COMMON_CFG)
        monkeypatch.setattr(naver_client, "register_product", _round5_dry_run_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            delivery_fee=3000.5,
            preview_confirmed=True,
        )
        assert result["ok"] is False, f"소수 배송비가 통과함: {result}"
        assert "정수가 아닙니다" in result.get(
            "error", ""
        ), f"소수 배송비 오류 메시지가 없음: {result.get('error')!r}"

    def test_integer_fee_accepted_at_register_product_entry(
        self, _round5_isolated_prepared, monkeypatch
    ):
        """register_product(delivery_fee=3000) → 정상 통과 (회귀 없음)."""
        name = "정상배송비상품"
        price = 31000
        pkey = register.make_product_key(name, price)
        _write_round5_prepared(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
        )

        _setup_round5_gate(monkeypatch)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            delivery_fee=3000,
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"정상 배송비가 거부됨: {result}"


# =========================================================================== #
# ⑤ 진입점(register._build_register_product_dict)에서 소수점 배송비가 거부된다.
# =========================================================================== #


class TestRegisterEntryPointFloatDeliveryFeeRejected:
    """register._build_register_product_dict → build_payload 경로에서
    소수점 배송비가 거부되는지 확인 (감리 ⑤).

    과거 결함: register.py 가 int(raw_fee) 로 미리 깎아서 정본 가드가 무력화.
    """

    def test_float_fee_rejected_through_build_register_product_dict(self):
        """_build_register_product_dict 가 float 배송비를 원값 그대로 넘긴다.

        이후 build_payload → _resolve_delivery_fee_with_slot 이 거부한다.
        """
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 3000.5,
        }
        result = register._build_register_product_dict(d, "테스트상품", "50000000")
        # 원값이 그대로 전달되어야 함 (int() 로 깎이지 않음).
        assert (
            result.get("delivery_fee") == 3000.5
        ), f"float 배송비가 int 로 깎임: {result.get('delivery_fee')!r}"

    def test_float_fee_rejected_in_build_payload_via_register(self):
        """register 경유 → build_payload → ValueError (정본 해석기가 거부)."""
        d = {
            "name": "테스트상품",
            "salePrice": 30000,
            "categoryId": "50000000",
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": 3000.5,
        }
        product = register._build_register_product_dict(d, "테스트상품", "50000000")
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="정수가 아닙니다"),
        ):
            naver_client.build_payload(product, "<html></html>", ["http://x.png"])


# =========================================================================== #
# ⑥ 불리언(True/False) 배송비가 정수로 강제 변환되지 않고 거부된다.
# =========================================================================== #


class TestBooleanDeliveryFeeRejected:
    """불리언 배송비가 정수로 변환되지 않고 거부되는지 확인 (감리 ⑥).

    과거 결함: True 가 int(True)=1 로 조용히 변환되었다.
    bool 은 int 의 서브클래스라 isinstance(raw, int) 가 통과하지만,
    배송비로 불리언을 받는 것은 입력 오류다.
    """

    @pytest.mark.parametrize("val", [True, False])
    def test_boolean_product_fee_raises(self, val):
        """p.delivery_fee=True/False → ValueError (상품 입력 자리)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": val,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="숫자가 아닙니다"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    @pytest.mark.parametrize("val", [True, False])
    def test_boolean_config_fee_raises(self, val):
        """cfg.delivery_fee=True/False → ValueError (설정 자리)."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": val}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="숫자가 아닙니다"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_boolean_error_message_mentions_boolean(self):
        """불리언 오류 메시지에 '불리언' 이 포함되어 진단 정보가 보존된다."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            "made_in": "중국",
            "delivery_fee": True,
        }
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=cfg),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            pytest.raises(ValueError, match="불리언"),
        ):
            naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_integer_zero_still_accepted(self):
        """불리언이 아닌 정수 0 은 여전히 유효 (무료배송). 회귀 없음."""
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
