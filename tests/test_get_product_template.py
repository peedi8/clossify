# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""등록된 상품에서 고시를 읽어 템플릿으로 만드는 경로 검증.

본 파일은 티켓 "등록된 상품에서 고시를 읽어 템플릿으로 만든다" 의 과업 (a)-(h)
를 검증한다. 핵심 위험은 **이음매** — ``get_product`` 가 돌려주는 **네이버 API
모양** 과 ``save_template`` 이 받는 **우리 입력 모양** 이 달라, 응답을 그대로
넘기면 **조용히 빈 템플릿**이 만들어진다는 것이다.

모든 테스트는 **``mcp_server.get_product`` 진입점**을 통해 검증한다(D41 요건).
네이버 목은 **실제 API 모양**(originProduct.detailAttribute.productInfoProvidedNotice)
을 그대로 흉내낸다 — 손으로 단 "다루기 쉬운" 모양이 아니다.

과업 매항:
  (a) API 모양 응답 → 저장 → 고시 필드가 실제로 채워진다 (0 이 아님).
  (b) N-of-M 완전성이 결과에 드러난다 (M 은 정본 해당 타입 필드 수).
  (c) 저장 엔트리에 출처(origin_product_no, read_at) 가 있다.
  (d) 고시 타입은 응답에서 읽는다 (호출자가 안 줘도 됨); 빈 타입 → 사유.
  (e) originProduct/detailAttribute/productInfoProvidedNotice 누락 → 구별되는 사유.
  (f) 이름 안 주면 → 저장 안 함 (기존 조회 동작 회귀 없음).
  (g) 서로 다른 상품군 2종이 각각 별도 템플릿으로 저장된다.
  (h) 비밀값/상품명/가격/이미지는 여전히 제외된다 (회귀).

모든 테스트는 ``common.STATE_DIR`` 을 ``tmp_path`` 로 격리한다. 네이버 라이브
호출은 0회(목으로 대체).
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

from clossify import common, listing_templates, mcp_server, naver_client


# --------------------------------------------------------------------------- #
# 실제 API 모양 헬퍼 — 네이버 응답 그대로 흉내.
#
# 핵심: ``originProduct.detailAttribute.productInfoProvidedNotice.<node>.<field>``
# 구조. ``<node>`` 는 고시 타입마다 다름(etc/wear/furniture/food/...). 우리 입력
# 모양(product.notice.<node>) 과 경로 앞부분이 다르다 — 이것이 이음매다.
# --------------------------------------------------------------------------- #
def _api_body_etc(
    *,
    notice_fields: dict[str, object] | None = None,
    as_tel: str = "1588-0000",
    origin_area_code: str = "05",
    content: str = "한국",
    importer: str = "테스트수입사",
    return_delivery_fee: str = "3000",
    exchange_delivery_fee: str = "6000",
    name: str = "실제상품명-이건-템플릿에-없어야함",
    sale_price: int = 99000,
    stock: int = 50,
    image_url: str = "http://cdn.example/photo.png",
) -> dict:
    """ETC 타입 실제 API 응답 모양을 만든다."""
    base_fields = {
        "returnCostReason": "단순변심 반품 배송비 구매자부담",
        "noRefundReason": "주문제작 상품 청약철회 제한",
        "qualityAssuranceStandard": "관련 법령에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "A/S 책임자: 070-0000-0000",
    }
    if notice_fields:
        base_fields.update(notice_fields)
    return {
        "originProduct": {
            "name": name,
            "salePrice": sale_price,
            "stockQuantity": stock,
            "representativeImage": {"url": image_url},
            "deliveryInfo": {
                "claimDeliveryInfo": {
                    "returnDeliveryFee": return_delivery_fee,
                    "exchangeDeliveryFee": exchange_delivery_fee,
                }
            },
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": dict(base_fields),
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": as_tel,
                    "afterServiceGuideContent": "A/S 안내: 평일 09-18시",
                },
                "originAreaInfo": {
                    "originAreaCode": origin_area_code,
                    "content": content,
                    "importer": importer,
                },
            },
        }
    }


def _api_body_wear(notice_overrides: dict[str, object] | None = None) -> dict:
    """WEAR 타입 API 응답 — WEAR 고유 13필드 채움."""
    fields = {
        "returnCostReason": "단순변심 반품비 구매자부담",
        "noRefundReason": "주문제작 청약철회 제한",
        "qualityAssuranceStandard": "관련법에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "고객센터 문의",
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "테스트제조사",
        "caution": "물 세탁 가능",
        "packDateText": "2026-01",
        "warrantyPolicy": "구매 후 7일 교환",
        "afterServiceDirector": "테스트제조사 070-0000-0000",
    }
    if notice_overrides:
        fields.update(notice_overrides)
    return {
        "originProduct": {
            "name": "WEAR-상품-이름",
            "salePrice": 29000,
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "WEAR",
                    "wear": fields,
                },
            },
        }
    }


# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """``common.STATE_DIR`` 을 tmp_path 로 격리."""
    fake_state = tmp_path / ".local"
    fake_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "STATE_DIR", fake_state)
    monkeypatch.setattr(common, "LOCAL_DIR", fake_state)
    return fake_state


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    """각 테스트마다 고시 후보·스펙 캐시를 리셋 — 테스트 격리."""
    monkeypatch.setattr(listing_templates, "_NOTICE_BODY_FIELD_CANDIDATES_CACHE", None)
    monkeypatch.setattr(naver_client, "_NOTICE_TYPES_CACHE", None)
    monkeypatch.setattr(naver_client, "_NOTICE_TYPE_INDEX", None)
    yield


def _patch_get_product(monkeypatch, body, status_code=200):
    """``naver_client.get_product`` 을 목으로 대체 — 라이브 호출 0회."""

    def _fake(origin_product_no, tk=None):
        return status_code, body

    monkeypatch.setattr(naver_client, "get_product", _fake)
    # 호출 카운터 — 테스트가 "네이버 라이브 0회" 를 증명.
    return mock.Mock(side_effect=_fake)


def _patch_get_product_counting(monkeypatch, body, status_code=200):
    """``naver_client.get_product`` 를 카운팅 목으로 대체."""
    calls = {"count": 0}

    def _fake(origin_product_no, tk=None):
        calls["count"] += 1
        return status_code, body

    monkeypatch.setattr(naver_client, "get_product", _fake)
    return calls


# =========================================================================== #
# (a) API 모양 응답 → save_as_template → 고시 필드가 실제로 채워진다.
# =========================================================================== #
class TestApiShapeToTemplateFields:
    """이음매 검증 — API 모양 응답을 그대로 save_template 에 넘기면 안 된다.

    본 테스트는 ``mcp_server.get_product(save_as_template=...)`` 가 내부적으로
    모양 변환을 거쳐 고시 필드가 실제로 저장되는지 확인한다(0 이 아님).
    """

    def test_etc_fields_actually_stored(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("12345", save_as_template="ETC-기본")
        assert result["ok"] is True
        saved = result["template_saved"]
        assert saved is not None
        assert saved["ok"] is True
        # 고시 본문이 실제로 채워졌는지 — 0 이면 이음매 결함 재현.
        summary = saved.get("notice_field_summary") or {}
        assert (
            summary.get("filled_count", 0) > 0
        ), f"고시 본문 필드 수가 0 — API→입력 모양 변환 결함 재현: {summary}"
        # ETC 공통 5필드가 채워졌는지.
        filled = summary.get("filled_fields") or []
        assert "returnCostReason" in filled
        assert "noRefundReason" in filled

    def test_as_origin_delivery_extracted(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("12345", save_as_template="ETC-부가")
        saved = result["template_saved"]
        # AS·원산지·배송비 가 저장 엔트리에 있다(top-level common 키로 옮겨짐).
        assert "afterServiceInfo" in saved.get("saved_keys") or any(
            "afterServiceInfo" in k for k in saved.get("saved_keys") or []
        )
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "1588-0000" in raw  # as_tel
        assert "한국" in raw  # origin_content
        assert "3000" in raw  # return_delivery_fee


# =========================================================================== #
# (b) N-of-M 완전성이 결과에 드러난다.
# =========================================================================== #
class TestCompletenessReporting:
    """완전성(N of M, M 은 정본 해당 타입 필드 수) 이 결과에 있다."""

    def test_completeness_in_template_saved(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()  # ETC 공통 5필드만 채움
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("67890", save_as_template="완전성-테스트")
        saved = result["template_saved"]
        assert saved["ok"] is True
        # source.completeness 가 entry 에 기록됐는지.
        assert saved.get("completeness") is not None
        comp = saved["completeness"]
        assert "filled_count" in comp
        assert "type_field_total" in comp
        assert "missing_fields" in comp
        # ETC 공통 5필드 → filled_count >= 5
        assert comp["filled_count"] >= 5
        # type_field_total 은 ETC 정본 필드 수(ETC 는 공통5 + customerServicePhoneNumber 등)
        assert comp["type_field_total"] > 0
        # missing_fields 는 리스트(빈 리스트일 수도 있으나 필드명만)
        assert isinstance(comp["missing_fields"], list)

    def test_completeness_records_missing(self, isolated_state_dir, monkeypatch):
        # ETC 의 필드 일부만 채운 응답 → 누락 필드가 missing_fields 에 드러남.
        # base 가 아닌 부분집합을 명시적으로 구성(merge 가 아님).
        body = {
            "originProduct": {
                "name": "X",
                "salePrice": 1000,
                "detailAttribute": {
                    "productInfoProvidedNotice": {
                        "productInfoProvidedNoticeType": "ETC",
                        "etc": {
                            "returnCostReason": "반품비 구매자부담",
                            "noRefundReason": "청약철회 제한",
                        },
                    }
                },
            }
        }
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("11111", save_as_template="부분-채움")
        saved = result["template_saved"]
        comp = saved["completeness"]
        assert comp["filled_count"] == 2
        assert len(comp["missing_fields"]) > 0
        # qualityAssuranceStandard 등 채우지 않은 필드가 missing 에 있다.
        assert "qualityAssuranceStandard" in comp["missing_fields"]


# =========================================================================== #
# (c) 저장 엔트리에 출처(origin_product_no, read_at) 가 있다.
# =========================================================================== #
class TestSourceProvenanceRecorded:
    """규제값이므로 *어느 상품에서 읽었는지* 출처가 저장 엔트리에 기록된다."""

    def test_source_block_in_stored_entry(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("99999", save_as_template="출처-증명")
        saved = result["template_saved"]
        assert saved["ok"] is True
        assert saved.get("source_recorded") is True
        # 저장 엔트리 파일에 source 블록이 있다.
        store = json.loads(listing_templates.templates_path().read_text(encoding="utf-8"))
        entry = next(
            (t for t in store["templates"] if t["name"] == "출처-증명"),
            None,
        )
        assert entry is not None
        source = entry.get("source")
        assert source is not None
        assert source["origin_product_no"] == "99999"
        assert source["read_at"]  # 비어있지 않은 ISO 문자열
        # completeness 도 출처 증거로 같이 있어야 한다.
        assert isinstance(source.get("completeness"), dict)


# =========================================================================== #
# (d) 고시 타입은 응답에서 읽는다; 빈 타입 → 사유.
# =========================================================================== #
class TestNoticeTypeReadFromResponse:
    """고시 타입은 응답의 productInfoProvidedNoticeType 에서 읽는다 (추측 금지)."""

    def test_type_read_from_response_not_caller(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()  # productInfoProvidedNoticeType: "ETC"
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("22222", save_as_template="타입-응답에서")
        saved = result["template_saved"]
        # 호출자는 save_as_template 이름만 줬고 타입은 안 줬다 — 응답에서 읽음.
        assert saved["notice_type"] == "ETC"

    def test_empty_type_reports_reason(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        # 타입을 비운 응답 — 추측하지 않고 사유를 반환해야 함.
        body["originProduct"]["detailAttribute"]["productInfoProvidedNotice"][
            "productInfoProvidedNoticeType"
        ] = ""
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("33333", save_as_template="빈-타입")
        saved = result["template_saved"]
        # 빈 템플릿이 조용히 만들어지지 않는다.
        assert saved["ok"] is False
        assert saved.get("reason")
        assert "추측" in saved["reason"] or "비어" in saved["reason"]
        # 빈 타입으로 저장되지 않았다.
        raw = listing_templates.templates_path()
        assert not raw.is_file() or "빈-타입" not in raw.read_text(encoding="utf-8")


# =========================================================================== #
# (e) originProduct/detailAttribute/productInfoProvidedNotice 누락 → 구별 사유.
# =========================================================================== #
class TestMissingPathDistinctReasons:
    """경로별 누락이 구별되는 사유로 드러난다 (조용한 실패 금지)."""

    def test_missing_origin_product(self, isolated_state_dir, monkeypatch):
        body = {"someOtherKey": {}}  # originProduct 없음
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("44444-1", save_as_template="경로1")
        saved = result["template_saved"]
        assert saved["ok"] is False
        assert "originProduct" in saved["reason"]

    def test_missing_detail_attribute(self, isolated_state_dir, monkeypatch):
        body = {"originProduct": {"name": "X"}}  # detailAttribute 없음
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("44444-2", save_as_template="경로2")
        saved = result["template_saved"]
        assert saved["ok"] is False
        assert "detailAttribute" in saved["reason"]

    def test_missing_notice_node(self, isolated_state_dir, monkeypatch):
        body = {
            "originProduct": {"detailAttribute": {}}  # productInfoProvidedNotice 없음
        }
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("44444-3", save_as_template="경로3")
        saved = result["template_saved"]
        assert saved["ok"] is False
        assert "productInfoProvidedNotice" in saved["reason"]

    def test_zero_notice_fields_reports(self, isolated_state_dir, monkeypatch):
        # 노드는 있으나 필드 전부 빈값 → 0개 읽음 → 사유.
        body = {
            "originProduct": {
                "detailAttribute": {
                    "productInfoProvidedNotice": {
                        "productInfoProvidedNoticeType": "ETC",
                        "etc": {"returnCostReason": ""},  # 빈값
                    }
                }
            }
        }
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("44444-4", save_as_template="경로4")
        saved = result["template_saved"]
        assert saved["ok"] is False
        assert "0" in saved["reason"]


# =========================================================================== #
# (f) 이름 안 주면 → 저장 안 함 (기존 조회 동작 회귀 없음).
# =========================================================================== #
class TestNoSaveOnEmptyName:
    """save_as_template 가 빈 문자열이면 템플릿 저장을 하지 않는다."""

    def test_no_save_returns_template_saved_none(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("55555")  # save_as_template 기본 ""
        assert result["ok"] is True
        # template_saved 가 None — 저장 시도 자체를 안 했다.
        assert result.get("template_saved") is None
        # 파일이 아예 없다.
        assert not listing_templates.templates_path().is_file()

    def test_existing_get_product_contract_preserved(self, isolated_state_dir, monkeypatch):
        """기존 get_product 규약(ok/status_code/product/error) 이 그대로다."""
        body = _api_body_etc()
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("66666")
        assert set(("ok", "status_code", "product", "error")).issubset(result.keys())
        assert result["status_code"] == 200
        assert result["product"] is not None

    def test_empty_origin_product_no_still_returns_template_saved_none(
        self, isolated_state_dir, monkeypatch
    ):
        _patch_get_product(monkeypatch, {}, status_code=404)
        result = mcp_server.get_product("77777", save_as_template="이름-있지만-404")
        # 조회 실패 → 템플릿 저장 경로 자체 진입 안 함.
        assert result["ok"] is False
        assert result.get("template_saved") is None


# =========================================================================== #
# (g) 서로 다른 상품군 2종이 각각 별도 템플릿으로 저장된다.
# =========================================================================== #
class TestMultipleCategoriesSeparateTemplates:
    """ETC 와 WEAR 가 각각 별도 템플릿으로 저장된다."""

    def test_two_categories_two_templates(self, isolated_state_dir, monkeypatch):
        # ETC 상품 저장.
        etc_body = _api_body_etc()
        _patch_get_product(monkeypatch, etc_body)
        r1 = mcp_server.get_product("111", save_as_template="ETC-템플릿")
        assert r1["template_saved"]["ok"] is True
        assert r1["template_saved"]["notice_type"] == "ETC"
        # WEAR 상품 저장.
        wear_body = _api_body_wear()
        _patch_get_product(monkeypatch, wear_body)
        r2 = mcp_server.get_product("222", save_as_template="WEAR-템플릿")
        assert r2["template_saved"]["ok"] is True
        assert r2["template_saved"]["notice_type"] == "WEAR"
        # 두 템플릿이 저장소에 별도 엔트리로 있다.
        listed = listing_templates.list_templates()
        names = {t["name"] for t in listed}
        assert "ETC-템플릿" in names
        assert "WEAR-템플릿" in names
        # 서로 다른 고시 타입 축.
        types = {t["name"]: t["notice_type"] for t in listed}
        assert types["ETC-템플릿"] == "ETC"
        assert types["WEAR-템플릿"] == "WEAR"

    def test_wear_specific_fields_stored(self, isolated_state_dir, monkeypatch):
        body = _api_body_wear()
        _patch_get_product(monkeypatch, body)
        result = mcp_server.get_product("333", save_as_template="WEAR-only")
        saved = result["template_saved"]
        summary = saved["notice_field_summary"]
        filled = summary.get("filled_fields") or []
        # WEAR 고유 필드(material/color/size) 가 저장됐는지.
        assert "material" in filled
        assert "color" in filled
        assert "size" in filled


# =========================================================================== #
# (h) 비밀값/상품명/가격/이미지는 여전히 제외된다 (회귀).
# =========================================================================== #
class TestSecretsAndProductValuesStillExcluded:
    """응답에서 온 상품 특정값/비밀값이 템플릿에 담기지 않는다."""

    def test_product_name_price_stock_image_not_stored(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        _patch_get_product(monkeypatch, body)
        mcp_server.get_product("888", save_as_template="회귀-안전")
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        # 상품명/가격/재고/이미지URL 이 템플릿에 없다.
        assert "실제상품명-이건-템플릿에-없어야함" not in raw
        assert "99000" not in raw  # salePrice
        assert "http://cdn.example/photo.png" not in raw  # image url

    def test_secret_keys_never_stored(self, isolated_state_dir, monkeypatch):
        # 응답에 token/secret 이 섞여 있어도 템플릿엔 담기지 않는다.
        body = _api_body_etc()
        body["originProduct"]["client_secret"] = "SECRET-LEAK-CANARY"
        body["originProduct"]["access_token"] = "ak-LEAK-TOKEN"
        _patch_get_product(monkeypatch, body)
        mcp_server.get_product("999", save_as_template="비밀-회귀")
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "SECRET-LEAK-CANARY" not in raw
        assert "ak-LEAK-TOKEN" not in raw

    def test_itemname_in_notice_body_excluded(self, isolated_state_dir, monkeypatch):
        # 고시 노드 안에 itemName 이 있어도 본문에 담기지 않는다(skip 키).
        body = _api_body_etc()
        body["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]["etc"]["itemName"] = (
            "내부상품명-빠져야함"
        )
        _patch_get_product(monkeypatch, body)
        mcp_server.get_product("101", save_as_template="itemName-제외")
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "내부상품명-빠져야함" not in raw


# =========================================================================== #
# 네이버 라이브 호출 0회 증명.
# =========================================================================== #
class TestNoLiveNaverCalls:
    """네이버 API 로의 라이브 호출이 0회임을 증명한다."""

    def test_get_product_uses_mock_only(self, isolated_state_dir, monkeypatch):
        body = _api_body_etc()
        calls = _patch_get_product_counting(monkeypatch, body)
        mcp_server.get_product("555", save_as_template="목-증명")
        # 정확히 1회 호출했고, 그 1회는 목이다(라이브가 아님).
        assert calls["count"] == 1
