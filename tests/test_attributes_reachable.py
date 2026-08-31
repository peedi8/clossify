# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""상품속성을 실제로 실을 수 있게 한다 (속성 도달 슬라이스2).

본 파일은 워크오더 ``.local/wo-n58-slice2.md`` 의 인수조건 1-5 를 검증한다:

  1. ``register_product`` 를 ``attributes`` 없이 호출 → payload 에
     ``productAttributes`` 키 부재.
  2. 명시 ID 리스트로 호출 → payload 에 그대로 실림. 잘못된 형태(문자 ID) 는
     거부(fail-closed).
  3. prepared 에 저장된 속성이 편집 없이 승인해도 최종 payload 에 실린다.
  4. 미리보기 행과 payload 가 같음을 확인.
  5. ``get_category_attributes`` MCP 도구가 실측 최상위 리스트 응답을 원형으로
     돌려주고, 확인 범위를 표시함(픽스처 기반).

슬라이스1 (``test_product_attributes.py``) 은 ``build_payload`` 단위였다.
슬라이스2는 MCP 진입점(``register_product`` · ``get_category_attributes``) 과
미리보기(``preview``) 를 잇는다 — 닿을 수 없는 배선을 실제로 닿게 만든다.

모든 테스트는 **mock 기반**으로 동작한다(네이버 라이브 호출 0회 — conftest
소켓 차단 아래). 값 창작·추론 금지(ID 참조 원칙) 를 지킨다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import (
    common,
    mcp_server,
    naver_client,
    preview,
    qa_agents,
    register,
)


# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


_NOTICE_MOCK = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
}

_COMMON_CFG_MOCK = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}


def _make_compliant_payload(extra: dict | None = None) -> dict:
    """컴플라이언스 게이트를 통과하는 WEAR 페이로드를 반환(DRY_RUN 용)."""
    wear_body = {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "테스트제조사",
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
    payload = {
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
    if extra:
        payload["originProduct"].update(extra)
    return payload


def _dry_run_naver_register(payload):
    """DRY_RUN 모드의 naver_client.register_product 대체."""
    return {"ok": True, "originProductNo": "test-no"}


def _setup_dry_run_gate(monkeypatch):
    """DRY_RUN + 게이트 통과 + config mock 세팅."""
    monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
    monkeypatch.setattr(naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload())
    monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)


def _write_prepared_with_attributes(
    pkey: str,
    *,
    name: str,
    price: int,
    listing_urls: list[str],
    detail_html: str,
    attributes: list[dict],
):
    """attributes 를 포함한 prepared payload 를 디스크에 저장한다."""
    agent_rows = [
        qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
        qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
    ]
    qa = qa_agents.aggregate_qa_results(agent_rows)
    product: dict = {
        "name": name,
        "salePrice": price,
        "attributes": list(attributes),
    }
    payload_obj: dict = {
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


# =========================================================================== #
# 인수조건 1: register_product 를 attributes 없이 호출 → payload 에
#              productAttributes 키 부재.
# =========================================================================== #
class TestNoAttributesMeansKeyAbsent:
    """attributes 인자 없이 등록 → build_payload 에 전달되는 product 에
    attributes 키가 없다 → productAttributes 키 부재(빈 배열 전송 금지)."""

    def test_attributes_key_absent_when_omitted(self, isolated_prepared_dir, monkeypatch):
        name = "속성미제공상품"
        price = 61000
        captured: list[dict] = []

        def capturing_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"product": dict(product)})
            return _make_compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(captured) == 1, f"build_payload 호출 횟수: {len(captured)}"
        product = captured[0]["product"]
        assert (
            "attributes" not in product
        ), f"attributes 를 주지 않았는데 product 에 키가 있다: {product.keys()}"


# =========================================================================== #
# 인수조건 2: 명시 ID 리스트로 호출 → payload 에 그대로 실림.
#              잘못된 형태(문자 ID·모르는 키)는 거부.
# =========================================================================== #
class TestExplicitAttributesCarriedToPayload:
    """명시 ID 리스트가 build_payload 에 전달되는 product.attributes 로 흐른다."""

    def test_explicit_attributes_carried(self, isolated_prepared_dir, monkeypatch):
        name = "속성명시상품"
        price = 62000
        attrs = [
            {"attributeSeq": 101, "attributeValueSeq": 2001},
            {
                "attributeSeq": 102,
                "attributeValueSeq": 2002,
                "attributeRealValue": "100",
                "attributeRealValueUnitCode": "MM",
            },
        ]
        captured: list[dict] = []

        def capturing_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"attributes": product.get("attributes")})
            return _make_compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            attributes=attrs,
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(captured) == 1
        carried = captured[0]["attributes"]
        assert carried is not None, "attributes 를 줬는데 product 에 없다"
        assert len(carried) == 2, f"속성 2개가 아니다: {len(carried)}"
        assert carried[0]["attributeSeq"] == 101
        assert carried[0]["attributeValueSeq"] == 2001
        assert carried[1]["attributeRealValue"] == "100"
        # filled_from_prepared 에 attributes 가 없어야 한다(명시값이므로).
        filled = result.get("filled_from_prepared", [])
        assert "attributes" not in filled, f"명시값인데 filled_from_prepared 에 있음: {filled}"

    def test_bad_format_string_seq_rejected(self, isolated_prepared_dir, monkeypatch):
        """문자 ID 는 거부된다 (fail-closed — ValueError).

        실제 build_payload → _validate_product_attributes 가 검증한다.
        _setup_dry_run_gate 가 build_payload 를 덮어쓰므로, 검증이 실제로
        일어나도록 진짜 build_payload 를 쓴다(config·KC 만 모킹).
        """
        name = "속성문자ID상품"
        price = 63000
        bad_attrs = [{"attributeSeq": "101", "attributeValueSeq": 2001}]

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            attributes=bad_attrs,
            preview_confirmed=True,
        )
        # ValueError 가 mcp_server 에서 잡혀 ok=False 로 반환된다.
        assert result["ok"] is False, f"문자 ID 속성인데 통과함 (fail-closed 위반): {result}"
        assert result.get("error"), "거부 사유가 없음"

    def test_bad_format_unknown_key_rejected(self, isolated_prepared_dir, monkeypatch):
        """모르는 키가 있으면 거부된다 (fail-closed — 실제 build_payload 경유)."""
        name = "속성알수없는키상품"
        price = 64000
        bad_attrs = [{"attributeSeq": 1, "attributeValueSeq": 2, "unknownKey": "x"}]

        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            attributes=bad_attrs,
            preview_confirmed=True,
        )
        assert result["ok"] is False, f"알 수 없는 키인데 통과함: {result}"
        assert result.get("error"), "거부 사유가 없음"


# =========================================================================== #
# 인수조건 3: prepared 에 저장된 속성이 편집 없이 승인해도 최종 payload 에 실린다.
# =========================================================================== #
class TestPreparedAttributesRestored:
    """prepared.product.attributes 가 register 까지 흐른다 (오늘 규제값에서
    겪은 것과 같은 자리)."""

    def test_prepared_attributes_restored_without_explicit(
        self, isolated_prepared_dir, monkeypatch
    ):
        name = "속성복원상품"
        price = 65000
        pkey = register.make_product_key(name, price)
        prepared_attrs = [
            {"attributeSeq": 201, "attributeValueSeq": 3001},
            {
                "attributeSeq": 202,
                "attributeValueSeq": 3002,
                "attributeRealValue": "50",
                "attributeRealValueUnitCode": "CM",
            },
        ]
        _write_prepared_with_attributes(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            attributes=prepared_attrs,
        )
        captured: list[dict] = []

        def capturing_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"attributes": product.get("attributes")})
            return _make_compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        # attributes 생략 → prepared 에서 복원되어야 함.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        filled = result.get("filled_from_prepared", [])
        assert (
            "attributes" in filled
        ), f"attributes 가 filled_from_prepared 에 있어야 한다: {filled}"
        assert len(captured) == 1
        carried = captured[0]["attributes"]
        assert carried is not None, "복원되었어야 하는데 product 에 없다"
        assert len(carried) == 2, f"속성 2개가 아니다: {len(carried)}"
        # 값이 그대로 보존됐는지(변환·추론 없음).
        assert carried[0]["attributeSeq"] == 201
        assert carried[0]["attributeValueSeq"] == 3001
        assert carried[1]["attributeRealValue"] == "50"

    def test_explicit_attributes_override_prepared(self, isolated_prepared_dir, monkeypatch):
        """명시 attributes 가 prepared 보다 우선한다."""
        name = "속성명시우선상품"
        price = 66000
        pkey = register.make_product_key(name, price)
        _write_prepared_with_attributes(
            pkey,
            name=name,
            price=price,
            listing_urls=["http://cdn/a.png"],
            detail_html="<html><body>detail</body></html>",
            attributes=[{"attributeSeq": 999, "attributeValueSeq": 888}],
        )
        captured: list[dict] = []

        def capturing_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"attributes": product.get("attributes")})
            return _make_compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        explicit_attrs = [{"attributeSeq": 10, "attributeValueSeq": 20}]
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            attributes=explicit_attrs,
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        filled = result.get("filled_from_prepared", [])
        assert "attributes" not in filled, f"명시값인데 filled_from_prepared 에 있음: {filled}"
        carried = captured[0]["attributes"]
        assert carried is not None
        assert carried[0]["attributeSeq"] == 10, f"명시값이 prepared 에게 짐: {carried[0]}"

    def test_prepared_without_attributes_does_not_fabricate(
        self, isolated_prepared_dir, monkeypatch
    ):
        """prepared 에 attributes 가 없으면 복원하지 않는다(창작 금지)."""
        name = "속성없는prepared"
        price = 67000
        pkey = register.make_product_key(name, price)
        # attributes 없이 prepared 저장.
        agent_rows = [
            qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
            qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
        ]
        qa = qa_agents.aggregate_qa_results(agent_rows)
        payload_obj = {
            "product_key": pkey,
            "version": common.PREPARED_PAYLOAD_VERSION,
            "product": {"name": name, "salePrice": price},
            "images": {"listing_urls": ["http://cdn/a.png"], "detail_urls": []},
            "detail_html": "<html><body>detail</body></html>",
            "qa": qa,
            "needs_llm": [],
            "needs_user": [],
        }
        register.write_prepared_payload(payload_obj)
        captured: list[dict] = []

        def capturing_build(product, detail_html_arg, image_urls_arg, status="SALE", **kwargs):
            captured.append({"has_attributes": "attributes" in product})
            return _make_compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert (
            captured[0]["has_attributes"] is False
        ), "prepared 에 없는데 attributes 가 지어냐짐 (창작 금지 위반)"
        filled = result.get("filled_from_prepared", [])
        assert "attributes" not in filled


# =========================================================================== #
# 인수조건 4: 미리보기 행과 payload 가 같음을 나란히 출력.
# =========================================================================== #
class TestPreviewRowsMatchPayload:
    """preview 의 속성 행이 build_payload 에 실리는 값과 같은가."""

    def test_rows_match_payload_for_two_attributes(self):
        """두 속성(필수키만 / 선택키 포함) 에 대해 미리보기 행이 payload 와
        같은 값을 가리킨다."""
        attrs = [
            {"attributeSeq": 101, "attributeValueSeq": 2001},
            {
                "attributeSeq": 102,
                "attributeValueSeq": 2002,
                "attributeRealValue": "100",
                "attributeRealValueUnitCode": "MM",
            },
        ]
        product = {
            "name": "테스트상품",
            "categoryId": "50002366",
            "salePrice": 10000,
            "attributes": attrs,
        }
        # preview 행 수집.
        rows = preview._collect_attribute_rows(product)
        assert len(rows) == 2, f"행이 2개여야 함: {len(rows)}"
        # 행 1: 필수키만.
        assert "attributeSeq=101" in rows[0]["value"]
        assert "attributeValueSeq=2001" in rows[0]["value"]
        assert rows[0]["source"] == "사용자 입력"
        # 행 2: 선택키 포함.
        assert "attributeSeq=102" in rows[1]["value"]
        assert "attributeValueSeq=2002" in rows[1]["value"]
        assert "attributeRealValue=100 MM" in rows[1]["value"]
        assert rows[1]["source"] == "사용자 입력"

    def test_no_attributes_means_no_rows(self):
        """속성이 없으면 행을 억지로 만들지 않는다(미제공 한 줄)."""
        product = {"name": "x", "salePrice": 1}
        rows = preview._collect_attribute_rows(product)
        assert rows == [], "속성이 없는데 행을 만들었다"

    def test_empty_attributes_means_no_rows(self):
        """빈 리스트 → 행 없음."""
        product = {"name": "x", "salePrice": 1, "attributes": []}
        rows = preview._collect_attribute_rows(product)
        assert rows == []

    def test_invalid_attribute_skipped_in_preview(self):
        """필수 키가 없거나 문자열이면 행에서 제외된다 — payload 에도 실리지
        않을 값이므로 화면에도 그리지 않는다 (불일치 금지)."""
        product = {
            "name": "x",
            "salePrice": 1,
            "attributes": [
                {"attributeSeq": 1, "attributeValueSeq": 2},
                {"attributeSeq": "bad", "attributeValueSeq": 3},  # 문자 → 제외
                {"attributeValueSeq": 4},  # attributeSeq 누락 → 제외
            ],
        }
        rows = preview._collect_attribute_rows(product)
        assert len(rows) == 1, f"유효하지 않은 속성이 행에 들어감: {len(rows)}"

    def test_preview_html_contains_attribute_section(self):
        """render_preview_html 이 상품속성 섹션을 포함하는가."""
        attrs = [{"attributeSeq": 1, "attributeValueSeq": 2}]
        # render_preview_html 은 prepared payload 형태를 받는다.
        payload = {
            "product": {
                "name": "테스트",
                "salePrice": 10000,
                "categoryId": "50002366",
                "attributes": attrs,
            },
            "images": {"listing_urls": ["http://cdn/a.png"], "detail_urls": []},
            "detail_html": "<html><body>detail</body></html>",
        }
        html = preview.render_preview_html(payload)
        assert "상품속성" in html, "상품속성 섹션이 없다"
        assert "attributeSeq=1" in html, "속성 값이 HTML 에 없다"
        assert "attributeValueSeq=2" in html

    def test_preview_html_shows_missing_when_no_attributes(self):
        """속성이 없으면 '미제공' 이 HTML 에 나타난다."""
        payload = {
            "product": {"name": "x", "salePrice": 10000, "categoryId": "50002366"},
            "images": {"listing_urls": ["http://cdn/a.png"], "detail_urls": []},
            "detail_html": "<html><body>detail</body></html>",
        }
        html = preview.render_preview_html(payload)
        assert "상품속성" in html
        assert "미제공" in html, "속성 미제공 표시가 없다"


# =========================================================================== #
# 인수조건 5: get_category_attributes MCP 도구가 실측 최상위 리스트 응답을
#              원형 + 확인 범위 표시로 돌려줌(픽스처 기반).
# =========================================================================== #
class TestGetCategoryAttributesMcpTool:
    """MCP 도구 get_category_attributes 의 동작 검증."""

    def test_parses_measured_top_level_attribute_list_fixture(self, monkeypatch):
        """실측 픽스처의 최상위 리스트를 속성 목록으로 그대로 반환한다."""
        fixture_path = _PROJECT_ROOT / "tests" / "fixtures" / "category_attributes_50000830.json"
        fixture_body = json.loads(fixture_path.read_text(encoding="utf-8"))

        def fake_api(category_id, tk=None):
            return 200, fixture_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50000830")

        expected_keys = {
            "attributeSeq",
            "attributeName",
            "attributeClassificationType",
            "attributeClassificationCodeName",
            "attributeType",
            "attributeTypeCodeName",
            "unitUsable",
            "attributeValueMaxMatchingCount",
        }
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["category_id"] == "50000830"
        assert result["schema_verified"] is True
        assert result["note"] == (
            "2026-08-12 카테고리 50000830 1건으로 확인. 다른 카테고리 형태는 미확인."
        )
        assert len(result["attributes"]) == 10
        assert all(set(attribute) == expected_keys for attribute in result["attributes"])
        assert any(
            attribute["attributeSeq"] == 10011015 and attribute["attributeName"] == "핏"
            for attribute in result["attributes"]
        )
        assert result["raw_body"] == fixture_body
        assert isinstance(result["raw_body"], list)
        assert result["raw_body_truncated"] is False

    def test_returns_raw_response_without_dict_key_assumption(self, monkeypatch):
        """200 dict 응답은 원형 보존하되 attributes 키를 탐색하지 않는다."""
        fake_body = {
            "attributes": [
                {"attributeSeq": 1, "attributeName": "색상"},
                {"attributeSeq": 2, "attributeName": "사이즈"},
            ]
        }

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        assert result["status_code"] == 200
        assert result["category_id"] == "50002366"
        assert result["schema_verified"] is False
        # 핵심: raw_body 원형 반환(재매핑 금지).
        assert result["raw_body"] == fake_body, "raw_body 가 원형이 아니다 — 재매핑하면 안 된다"
        assert result["attributes"] is None
        assert "raw_body" in result["note"]

    def test_empty_category_id_rejected(self):
        """빈 category_id → API 호출 없이 거부."""
        result = mcp_server.get_category_attributes("")
        assert result["ok"] is False
        assert result["status_code"] is None
        assert result["schema_verified"] is False
        assert result["error"], "거부 사유가 없음"

    def test_non_200_response_kept_as_is(self, monkeypatch):
        """비 200 응답도 조용히 빈 목록으로 바꾸지 않는다 — 원형 보고."""
        fake_body = {"error": "not found", "detail": "category does not exist"}

        def fake_api(category_id, tk=None):
            return 404, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("99999")
        assert result["ok"] is False
        assert result["status_code"] == 404
        assert result["schema_verified"] is False
        # 비 200 이면 attributes 는 None (raw_body 에 본문이 실림).
        assert result["attributes"] is None
        # raw_body 에 본문이 살아있다(버리지 않는다).
        assert result["raw_body"] is not None, "비 200 응답 본문이 raw_body 에 없다"
        assert result["error"], "에러 사유가 없음"

    def test_api_exception_returns_error(self, monkeypatch):
        """API 호출 자체가 예외를 던지면 ok=False + 사유."""

        def fake_api(category_id, tk=None):
            raise RuntimeError("연결 실패")

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is False
        assert result["status_code"] is None
        assert result["schema_verified"] is False
        assert "연결 실패" in result["error"] or "오류" in result["error"]

    def test_docstring_discloses_measured_scope(self):
        """도구 설명(docstring) 에 실측 일자와 범위를 명시."""
        doc = mcp_server.get_category_attributes.__doc__ or ""
        assert "2026-08-12" in doc
        assert "50000830" in doc
        assert "다른 카테고리 형태는 미확인" in doc

    def test_unexpected_shape_not_silently_emptied(self, monkeypatch):
        """응답이 예상과 달라도 빈 목록으로 바꾸지 않는다 — 원형 보고.

        가정한 키(attributes) 가 없으면 attributes=None 이되,
        raw_body 에 원형이 살아 있다(버리지 않는다).
        """
        unexpected_body = {"unexpected": "shape", "code": "WEIRD"}

        def fake_api(category_id, tk=None):
            return 200, unexpected_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        # attributes 는 가정한 키가 없으므로 None.
        assert result["attributes"] is None, "가정한 키가 없는데 attributes 가 있다"
        # 핵심: raw_body 에 원형이 살아 있다(조용히 버리지 않는다).
        assert (
            result["raw_body"] == unexpected_body
        ), "예상과 다른 응답을 raw_body 에서 버렸다 — 원형을 그대로 돌려줘야 한다"
        # note 에 원형 본문을 보라는 안내가 있어야 한다.
        assert "raw_body" in result["note"], "note 에 raw_body 를 보라는 안내가 없다"


# =========================================================================== #
# 인수조건 5b: raw_body 원형 보존 — 4가지 body 형태 (속성 도달 슬라이스2 보완)
# 워크오더 wo-n58-raw-body 의 실측 표 4종.
# =========================================================================== #
class TestRawBodyPreservedFourShapes:
    """워크오더 실측 표의 4가지 body 형태에 대해 raw_body 원형 보존을 검증.

    워크오더 지시: "받은 것을 버리지 않게 한다".
    각 행에 대해 raw_body 에 원본 흔적(unexpectedKey·totalCount·seq·{})
    이 살아 있음을 보인다.
    """

    def test_shape1_dict_with_unexpected_key(self, monkeypatch):
        """body={"attributes":[...], "unexpectedKey":"..."} → raw_body 에 둘 다."""
        fake_body = {
            "attributes": [{"attributeSeq": 1, "attributeName": "색상"}],
            "unexpectedKey": "보너스값",
        }

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        # dict의 attributes 키는 탐색하지 않는다.
        assert result["attributes"] is None
        # 핵심: unexpectedKey 가 raw_body 에 살아있다.
        rb = result["raw_body"]
        assert isinstance(rb, dict), f"raw_body 가 dict 가 아님: {type(rb)}"
        assert (
            rb.get("unexpectedKey") == "보너스값"
        ), "unexpectedKey 가 raw_body 에 없다 — 원본 흔적이 사라졌다"
        assert rb.get("attributes") == fake_body["attributes"]

    def test_shape2_data_array_with_total_count(self, monkeypatch):
        """body={"data":[...], "totalCount":1} → attributes=None, raw_body 보존."""
        fake_body = {"data": [{"seq": 1}], "totalCount": 1}

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        # 최상위 리스트가 아니면 attributes=None 이되 원형은 살아있다.
        assert result["attributes"] is None, "attributes 키가 없는데 None 이 아니다"
        rb = result["raw_body"]
        assert isinstance(rb, dict), f"raw_body 가 dict 가 아님: {type(rb)}"
        assert rb.get("totalCount") == 1, "totalCount 가 raw_body 에 없다 — 원본 흔적이 사라졌다"
        assert rb.get("data") == [{"seq": 1}]
        # note 에 원형 본문을 보라는 안내.
        assert "raw_body" in result["note"]

    def test_shape3_list_top_level(self, monkeypatch):
        """body=[{"seq":1}] (리스트 최상위) → raw_body 에 리스트 원형 보존."""
        fake_body = [{"seq": 1, "attributeName": "색상"}]

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        # 최상위 리스트가 곧 속성 목록이다.
        assert result["attributes"] == fake_body
        # 핵심: 리스트 원형이 raw_body 에 살아있다.
        rb = result["raw_body"]
        assert rb == fake_body, "리스트 원형이 raw_body 에 없다 — 받은 것을 버렸다"
        assert isinstance(rb, list), f"raw_body 가 list 가 아님: {type(rb)}"
        assert rb[0]["seq"] == 1, "seq 가 raw_body 에 없다"

    def test_shape4_empty_dict(self, monkeypatch):
        """body={} → ok=True 이되 raw_body={} 살아있음."""
        fake_body = {}

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        assert result["attributes"] is None
        # 빈 dict 도 버리지 않는다.
        assert result["raw_body"] == {}, "빈 dict 도 raw_body 에 살아있어야 한다"

    def test_raw_body_truncation_flag(self, monkeypatch):
        """raw_body 가 상한을 초과하면 자르되 raw_body_truncated=True."""
        # 상한(8192)을 넘는 큰 본문.
        big_attrs = [
            {"attributeSeq": i, "attributeName": f"속성{i}_" + "x" * 200} for i in range(200)
        ]
        fake_body = {"attributes": big_attrs, "extra": "y" * 5000}

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        assert (
            result["raw_body_truncated"] is True
        ), "상한 초과인데 raw_body_truncated=False 다 — 조용한 절삭"
        assert isinstance(result["raw_body"], str), "잘린 raw_body 는 문자열이어야 한다"
        assert "truncated" in result["raw_body"], "잘린 표시가 raw_body 에 없다"

    def test_raw_body_not_truncated_when_small(self, monkeypatch):
        """작은 본문은 잘리지 않는다 — raw_body_truncated=False."""
        fake_body = {"attributes": [{"attributeSeq": 1}]}

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["raw_body_truncated"] is False

    def test_secret_keys_masked_in_raw_body(self, monkeypatch):
        """응답에 토큰·비밀 키가 있으면 마스킹한다(방어적)."""
        fake_body = {
            "attributes": [{"attributeSeq": 1}],
            "accessToken": "비밀토큰값",
            "clientSecret": "비밀키",
        }

        def fake_api(category_id, tk=None):
            return 200, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("50002366")
        assert result["ok"] is True
        rb = result["raw_body"]
        assert rb["accessToken"] == "***", "accessToken 이 마스킹되지 않았다"
        assert rb["clientSecret"] == "***", "clientSecret 이 마스킹되지 않았다"
        # attributes 는 마스킹 영향 없음.
        assert rb["attributes"] == [{"attributeSeq": 1}]

    def test_non_200_raw_body_preserved(self, monkeypatch):
        """비 200 응답도 raw_body 에 본문을 살린다(버리지 않는다)."""
        fake_body = {"error": "not found", "detail": "category does not exist"}

        def fake_api(category_id, tk=None):
            return 404, fake_body

        monkeypatch.setattr(naver_client, "get_category_attributes", fake_api)

        result = mcp_server.get_category_attributes("99999")
        assert result["ok"] is False
        assert result["attributes"] is None
        # 핵심: 비 200 이어도 본문이 raw_body 에 있다.
        assert result["raw_body"] is not None
        assert "not found" in str(result["raw_body"]), "에러 본문이 raw_body 에 없다"


# =========================================================================== #
# 인수조건 6: prepare_listing 이 attributes 를 저장한다.
# (슬라이스2 의 register.py 변경이 prepare → register 왕복을 완성하는지 검증)
# =========================================================================== #
class TestPrepareListingStoresAttributes:
    """prepare_listing 이 상품 입력의 attributes 를 prepared payload 에
    저장하는가."""

    def test_prepare_stores_attributes(self, isolated_prepared_dir, monkeypatch):
        """prepare_listing(d, ...) 가 d['attributes'] 를 payload 에 저장한다."""
        d = {
            "name": "속성저장상품",
            "salePrice": 68000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
            "attributes": [
                {"attributeSeq": 1, "attributeValueSeq": 2},
                {
                    "attributeSeq": 3,
                    "attributeValueSeq": 4,
                    "attributeRealValue": "10",
                    "attributeRealValueUnitCode": "KG",
                },
            ],
        }
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda sources: {
                "urls": [f"http://cdn/test/img{i}.png" for i in range(len(sources))],
                "rejected": [],
                "notes": [],
            },
        )
        result = mcp_server.prepare_listing(d)
        assert result["ok"] is True, f"prepare_listing 실패: {result}"
        # 디스크에서 다시 읽어 product.attributes 가 저장됐는지 확인.
        # prepare_listing 은 category_id·image_sources 를 키 유도에 포함한다.
        pkey = register.make_product_key(
            d["name"],
            d["salePrice"],
            category_id=d.get("category_id"),
            image_sources=d.get("image_sources"),
        )
        loaded = register.load_prepared_payload(product_key=pkey)
        product = loaded.get("product", {})
        assert "attributes" in product, "prepared 에 attributes 키가 없다"
        stored = product["attributes"]
        assert len(stored) == 2, f"속성 2개가 저장되어야 함: {len(stored)}"
        assert stored[0]["attributeSeq"] == 1
        assert stored[1]["attributeRealValue"] == "10"

    def test_prepare_omits_attributes_when_absent(self, isolated_prepared_dir, monkeypatch):
        """attributes 가 없으면 prepared payload 에 키를 넣지 않는다."""
        d = {
            "name": "속성미포함상품",
            "salePrice": 69000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
        }
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda sources: {
                "urls": [f"http://cdn/test/img{i}.png" for i in range(len(sources))],
                "rejected": [],
                "notes": [],
            },
        )
        result = mcp_server.prepare_listing(d)
        assert result["ok"] is True, f"prepare_listing 실패: {result}"
        pkey = register.make_product_key(
            d["name"],
            d["salePrice"],
            category_id=d.get("category_id"),
            image_sources=d.get("image_sources"),
        )
        loaded = register.load_prepared_payload(product_key=pkey)
        product = loaded.get("product", {})
        assert "attributes" not in product, "attributes 를 주지 않았는데 prepared 에 키가 있다"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
