# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""상품속성 슬라이스1 (N58) — 조회 배선 + 명시 ID 전송 + 수확 편승 검증.

본 파일은 워크오더 ``.local/wo-n58-slice1.md`` 의 인수조건 1-4 를 검증한다:

  1. 명시 attributes 입력 → payload 의 originProduct.detailAttribute
     .productAttributes 에 그대로(키 4종만) 실린다.
  2. 형태 위반(문자 ID·모르는 키) → ValueError. 미제공 → 키 부재.
  3. 조회 함수 mock 시험 통과 + 독스트링에 [미실측] 문구 존재.
  4. 수확 보존/통과 시험.

모든 테스트는 **mock 기반**으로 동작한다(네이버 라이브 호출 0회 — conftest
소켓 차단 아래). 값 창작·추론 금지(ID 참조 원칙) 를 지킨다.
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

from clossify import listing_templates, naver_client


# --------------------------------------------------------------------------- #
# 페이로드 빌드 헬퍼 (test_payload_shape.py 관례와 동일).
# --------------------------------------------------------------------------- #
def _make_product(**overrides) -> dict:
    """build_payload 에 넘길 최소 상품 dict."""
    base = {
        "name": "테스트상품",
        "categoryId": "50002366",
        "salePrice": 10000,
        "origin_code": "05",
        "made_in": "한국",
    }
    base.update(overrides)
    return base


def _build_payload(p: dict) -> dict:
    """내부 config 의존(config 로드/KC)을 끊고 build_payload 만 실행."""
    with mock.patch.object(
        naver_client, "_notice_config", return_value={"delivery_company": "HKSTRANS"}
    ):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            return naver_client.build_payload(p, "<html></html>", ["http://x/img.png"])


def _detail_attribute(payload: dict) -> dict:
    """payload 에서 originProduct.detailAttribute 를 꺼낸다."""
    return payload["originProduct"]["detailAttribute"]


# =========================================================================== #
# (1) 명시 attributes 입력 → productAttributes 에 그대로(키 4종만) 실림.
# =========================================================================== #
class TestExplicitAttributesToPayload:
    """명시적 ID 입력이 payload 의 productAttributes 에 정확히 실리는가."""

    def test_two_attributes_carried_with_only_four_keys(self):
        """두 속성(필수키만 / 선택키 포함) 이 키 4종만으로 실리는가."""
        attrs = [
            {"attributeSeq": 101, "attributeValueSeq": 2001},
            {
                "attributeSeq": 102,
                "attributeValueSeq": 2002,
                "attributeRealValue": "100",
                "attributeRealValueUnitCode": "MM",
            },
        ]
        payload = _build_payload(_make_product(attributes=attrs))
        da = _detail_attribute(payload)
        assert "productAttributes" in da, "명시 attributes 가 있는데 productAttributes 키가 없다."
        carried = da["productAttributes"]
        assert isinstance(carried, list)
        assert len(carried) == 2
        # 각 엔트리의 키가 허용 4종 이내인지.
        allowed = {
            "attributeSeq",
            "attributeValueSeq",
            "attributeRealValue",
            "attributeRealValueUnitCode",
        }
        for entry in carried:
            assert (
                set(entry.keys()) <= allowed
            ), f"허용 키 외 키가 실렸다: {set(entry.keys()) - allowed}"
        # 값이 그대로 전달됐는지(변환·추론 없음).
        assert carried[0]["attributeSeq"] == 101
        assert carried[0]["attributeValueSeq"] == 2001
        assert carried[1]["attributeRealValue"] == "100"
        assert carried[1]["attributeRealValueUnitCode"] == "MM"

    def test_single_attribute_required_keys_only(self):
        """필수 키만 있는 단일 속성이 정확히 실린다."""
        attrs = [{"attributeSeq": 1, "attributeValueSeq": 2}]
        payload = _build_payload(_make_product(attributes=attrs))
        carried = _detail_attribute(payload)["productAttributes"]
        assert len(carried) == 1
        assert set(carried[0].keys()) == {"attributeSeq", "attributeValueSeq"}
        assert carried[0]["attributeSeq"] == 1
        assert carried[0]["attributeValueSeq"] == 2

    def test_real_value_without_unit_code(self):
        """attributeRealValue 만 있고 unitCode 는 없어도 실린다 (선택 키)."""
        attrs = [
            {
                "attributeSeq": 10,
                "attributeValueSeq": 20,
                "attributeRealValue": "50",
            }
        ]
        payload = _build_payload(_make_product(attributes=attrs))
        carried = _detail_attribute(payload)["productAttributes"]
        assert carried[0].get("attributeRealValue") == "50"
        assert "attributeRealValueUnitCode" not in carried[0]


# =========================================================================== #
# (2) 형태 위반 → ValueError. 미제공 → 키 부재. 빈 리스트 → 키 부재.
# =========================================================================== #
class TestFormValidationFailClosed:
    """형태 위반은 ValueError 로 거부(fail-closed). 미제공/빈 리스트 → 키 부재."""

    def test_string_seq_rejected(self):
        """attributeSeq 가 문자열이면 ValueError (조용한 변환 금지)."""
        attrs = [{"attributeSeq": "101", "attributeValueSeq": 2001}]
        with pytest.raises(ValueError, match="정수가 아닙니다"):
            _build_payload(_make_product(attributes=attrs))

    def test_string_value_seq_rejected(self):
        """attributeValueSeq 가 문자열이면 ValueError."""
        attrs = [{"attributeSeq": 101, "attributeValueSeq": "2001"}]
        with pytest.raises(ValueError, match="정수가 아닙니다"):
            _build_payload(_make_product(attributes=attrs))

    def test_unknown_key_rejected(self):
        """허용 키 외의 키가 있으면 ValueError."""
        attrs = [{"attributeSeq": 1, "attributeValueSeq": 2, "unknownKey": "x"}]
        with pytest.raises(ValueError, match="허용되지 않은 키"):
            _build_payload(_make_product(attributes=attrs))

    def test_missing_required_seq_rejected(self):
        """attributeSeq 누락 → ValueError."""
        attrs = [{"attributeValueSeq": 2}]
        with pytest.raises(ValueError, match="필수 키.*attributeSeq"):
            _build_payload(_make_product(attributes=attrs))

    def test_missing_required_value_seq_rejected(self):
        """attributeValueSeq 누락 → ValueError."""
        attrs = [{"attributeSeq": 1}]
        with pytest.raises(ValueError, match="필수 키.*attributeValueSeq"):
            _build_payload(_make_product(attributes=attrs))

    def test_non_dict_item_rejected(self):
        """리스트 원소가 dict 가 아니면 ValueError."""
        attrs = [{"attributeSeq": 1, "attributeValueSeq": 2}, "not-a-dict"]
        with pytest.raises(ValueError, match="dict 가 아닙니다"):
            _build_payload(_make_product(attributes=attrs))

    def test_non_list_rejected(self):
        """attributes 가 리스트가 아니면 ValueError."""
        with pytest.raises(ValueError, match="리스트여야 합니다"):
            _build_payload(_make_product(attributes={"attributeSeq": 1}))

    def test_bool_seq_rejected(self):
        """bool 은 int 의 서브클래스지만 ID 로 허용하지 않는다."""
        attrs = [{"attributeSeq": True, "attributeValueSeq": 2}]
        with pytest.raises(ValueError, match="정수가 아닙니다"):
            _build_payload(_make_product(attributes=attrs))

    def test_no_attributes_input_means_key_absent(self):
        """attributes 입력이 없으면 productAttributes 키가 없다 (빈 배열 전송 금지)."""
        payload = _build_payload(_make_product())
        da = _detail_attribute(payload)
        assert (
            "productAttributes" not in da
        ), "attributes 입력이 없는데 productAttributes 키가 있다 — 빈 배열 전송 위험."

    def test_empty_list_means_key_absent(self):
        """빈 리스트 → productAttributes 키 부재 (빈 배열 전송 금지)."""
        payload = _build_payload(_make_product(attributes=[]))
        da = _detail_attribute(payload)
        assert "productAttributes" not in da


# =========================================================================== #
# (3) 조회 함수 mock 시험 + 독스트링 [미실측] grep.
# =========================================================================== #
class TestGetCategoryAttributes:
    """get_category_attributes mock 시험 + 독스트링 [미실측] 문구 확인."""

    def test_docstring_has_unmeasured_marker(self):
        """독스트링에 [미실측] 문구가 있어야 한다 (응답 스키마 미실측 표시)."""
        doc = naver_client.get_category_attributes.__doc__ or ""
        assert "[미실측]" in doc, (
            "get_category_attributes 독스트링에 [미실측] 문구가 없다 — "
            "실측 전 알 수 없다는 것을 명시해야 한다."
        )

    def test_returns_status_body_convention(self):
        """mock GET 요청 → (status_code, body) 관례를 따른다."""
        fake_body = {"attributes": [{"attributeSeq": 1, "attributeName": "색상"}]}
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = fake_body

        with mock.patch.object(naver_client, "get_token", lambda: "fake-tk"):
            with mock.patch.object(naver_client.requests, "get", return_value=fake_response):
                with mock.patch.object(
                    naver_client, "_json_or_text_response", return_value=fake_body
                ):
                    status, body = naver_client.get_category_attributes("50002366")

        assert status == 200
        assert body == fake_body

    def test_passes_category_id_as_param(self):
        """categoryId 가 쿼리 파라미터로 전달된다."""
        captured: dict = {}
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {}

        def _capture(url, **kwargs):
            captured["url"] = url
            captured["params"] = kwargs.get("params")
            return fake_response

        with mock.patch.object(naver_client, "get_token", lambda: "fake-tk"):
            with mock.patch.object(naver_client.requests, "get", side_effect=_capture):
                with mock.patch.object(naver_client, "_json_or_text_response", return_value={}):
                    naver_client.get_category_attributes("50002366")

        assert captured["params"] is not None
        assert captured["params"].get("categoryId") == "50002366"

    def test_uses_supplied_token(self):
        """tk 인자가 있으면 get_token 을 호출하지 않는다."""
        fake_response = mock.Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {}
        token_calls: list = []

        def _fail_token():
            token_calls.append(1)
            return "should-not-be-used"

        with mock.patch.object(naver_client.requests, "get", return_value=fake_response):
            with mock.patch.object(naver_client, "_json_or_text_response", return_value={}):
                naver_client.get_category_attributes("50002366", tk="supplied-tk")

        assert token_calls == [], "tk 를 줬으면 get_token 을 부르면 안 된다."

    def test_404_response_passed_through(self):
        """에러 응답도 (status, body) 관례로 반환한다."""
        fake_response = mock.Mock()
        fake_response.status_code = 404
        fake_response.json.return_value = {"error": "not found"}

        with mock.patch.object(naver_client, "get_token", lambda: "fake-tk"):
            with mock.patch.object(naver_client.requests, "get", return_value=fake_response):
                with mock.patch.object(
                    naver_client,
                    "_json_or_text_response",
                    return_value={"error": "not found"},
                ):
                    status, body = naver_client.get_category_attributes("99999")

        assert status == 404
        assert isinstance(body, dict)


# =========================================================================== #
# (4) 수확 편승 — 응답에 있으면 보존 / 없으면 통과.
# =========================================================================== #
class TestHarvestPassthrough:
    """transform_product_to_template_input 에서 productAttributes 편승 검증."""

    def _base_api_body(self) -> dict:
        """ETC 타입 최소 API 응답 (고시 필드 1개 이상)."""
        return {
            "originProduct": {
                "name": "속성-테스트-상품",
                "salePrice": 10000,
                "detailAttribute": {
                    "productInfoProvidedNotice": {
                        "productInfoProvidedNoticeType": "ETC",
                        "etc": {
                            "returnCostReason": "단순변심 반품비 구매자부담",
                        },
                    },
                },
            }
        }

    def test_attributes_preserved_when_present(self):
        """응답에 productAttributes 가 있으면 변환 결과에 그대로 보존된다."""
        body = self._base_api_body()
        attrs = [
            {"attributeSeq": 1, "attributeValueSeq": 10},
            {
                "attributeSeq": 2,
                "attributeValueSeq": 20,
                "attributeRealValue": "100",
                "attributeRealValueUnitCode": "MM",
            },
        ]
        body["originProduct"]["detailAttribute"]["productAttributes"] = attrs

        result = listing_templates.transform_product_to_template_input(body)
        assert result["ok"] is True
        product = result["product"]
        assert "attributes" in product, "응답에 있었는데 변환 결과에 없다 — 편승 누락."
        # 내용이 그대로 보존됐는지(변환·추론 없음).
        assert product["attributes"] == attrs

    def test_attributes_absent_means_no_key(self):
        """응답에 productAttributes 가 없으면 변환 결과에 attributes 키가 없다."""
        body = self._base_api_body()
        result = listing_templates.transform_product_to_template_input(body)
        assert result["ok"] is True
        assert (
            "attributes" not in result["product"]
        ), "응답에 없는데 attributes 키가 있다 — 지어낸 것이다."

    def test_empty_attributes_list_means_no_key(self):
        """빈 리스트 → attributes 키 부재 (조용한 통과)."""
        body = self._base_api_body()
        body["originProduct"]["detailAttribute"]["productAttributes"] = []
        result = listing_templates.transform_product_to_template_input(body)
        assert result["ok"] is True
        assert "attributes" not in result["product"]

    def test_attributes_not_mutated_from_api_shape(self):
        """속성 값이 API 응답 모양(camelCase) 그대로 보존된다 (재해석 없음)."""
        body = self._base_api_body()
        attrs = [{"attributeSeq": 99, "attributeValueSeq": 88}]
        body["originProduct"]["detailAttribute"]["productAttributes"] = attrs
        result = listing_templates.transform_product_to_template_input(body)
        # 키가 camelCase 그대로 보존됐는지(snake_case 로 변환하지 않음).
        assert result["product"]["attributes"][0] == {"attributeSeq": 99, "attributeValueSeq": 88}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
