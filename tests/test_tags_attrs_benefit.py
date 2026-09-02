# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""태그·속성 자동 결선 + 구매/리뷰 혜택 지원.

인수조건 대응:

  (a) prepare 반환에 tags/attributes 제안 키(재료 있을 때), 근거 표기.
  (b) register 자동 채용 시 filled_from_prepared 표기. 명시 인자 우선.
  (c) 제안 불가 상황에서 조용한 생략 없음(사유 키).
  (d) Part C 문서 근거(아래 경로 인용) + 인자→payload 매핑 테스트.
  (e) 외부 호출 전부 모킹(conftest 소켓 차단 + monkeypatch).

Part C 문서 근거(창작 금지 — 스펙의 단일 진실):
  운영 문서 색인의 커머스API "원상품 정보 구조체" 스키마
  ("원상품 정보 구조체" — customerBenefit 하위 정책 8종: immediateDiscountPolicy,
  purchasePointPolicy, reviewPointPolicy, freeInterestPolicy, giftPolicy,
  multiPurchaseDiscountPolicy, reservedDiscountPolicy, promotionDiscountPolicies)
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

from clossify import common, mcp_server, naver_client, register


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


_ATTACH_MOCK = {
    "urls": ["http://cdn/test/img0.png"],
    "rejected": [],
    "notes": [],
}

_NOTICE_MOCK = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "delivery_company": "HKSTRANS",
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


def _compliant_payload():
    return {
        "originProduct": {
            "originProductNo": "test-no",
            "images": {"representativeImage": {"url": "http://cdn/a.png"}},
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": {
                        "returnCostReason": "x",
                        "noRefundReason": "x",
                        "qualityAssuranceStandard": "x",
                        "compensationProcedure": "x",
                        "troubleShootingContents": "x",
                    },
                },
                "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                "afterServiceInfo": {"afterServiceTelephoneNumber": "070-1234-5678"},
            },
        }
    }


def _setup_dry_run_gate(monkeypatch):
    monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
    monkeypatch.setattr(naver_client, "register_product", lambda p: {"ok": True})
    # 본 파일의 검증 대상은 태그·속성 자동 채용과 customerBenefit 전달이지
    # 고시 게이트 자체가 아니다. 카테고리 50002366(스탠드) 은 카테고리 메타
    # 기준 HOME_APPLIANCES + KC 인증이 필요해 게이트가 차단하므로, 다른
    # 테스트 파일(test_silent_failure_gates 등) 의 확립된 패턴대로 컴플라이언스
    # 게이트와 트립와이어 판정만 모킹한다. 모킹된 _compliant_payload() 의
    # 고시 타입(ETC) 과 게이트 판정(ETC) 을 일치시켜 트립와이어를 통과한다.
    monkeypatch.setattr(
        mcp_server,
        "_run_compliance_gate",
        lambda name, category_id, payload, deferred_notice_fields=None: {
            "blocked": False,
            "violations": [],
            "needs_user": [],
            "pending_reviews": [],
        },
    )
    monkeypatch.setattr(mcp_server, "_gate_notice_type", lambda category_id, product=None: "ETC")


# =========================================================================== #
# (a) prepare 반환: 태그·속성 제안 키 + 근거.
# =========================================================================== #
class TestPrepareSuggestions:
    def test_tags_suggestion_with_basis(self, isolated_prepared_dir, monkeypatch):
        """prepare 성공 반환에 seo_tags_suggestion(tags+basis) 이 있다."""
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
        result = mcp_server.prepare_listing(
            {"name": "도자기 머그컵", "salePrice": 15000, "image_sources": ["a.png"]}
        )
        assert result["ok"] is True, f"prepare 실패: {result}"
        suggestion = result["seo_tags_suggestion"]
        assert isinstance(suggestion, dict)
        assert suggestion["tags"], "태그 제안이 비어 있다"
        assert len(suggestion["tags"]) <= 10
        assert "도자기" in suggestion["tags"]
        assert any(b.startswith("name:") for b in suggestion["basis"]), "근거(basis) 없음"

    def test_attributes_suggestion_matched_only(self, isolated_prepared_dir, monkeypatch):
        """문자열 일치 속성값만 attributes_suggestion 으로 나간다(외부 호출 모킹)."""
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
        attr_fn_calls: list = []

        def fake_attr_fn(category_id, product_text):
            attr_fn_calls.append((category_id, product_text))
            return {
                "ok": True,
                "error": None,
                "suggestions": [
                    {
                        "attributeSeq": 11,
                        "attributeName": "재질",
                        "attributeTypeCodeName": "주요",
                        "classification": "선택형",
                        "status": "matched",
                        "selected": [
                            {
                                "attributeValueSeq": 1101,
                                "minAttributeValue": "도자기",
                                "evidence": "name[0:3]:'도자기'",
                            }
                        ],
                        "candidates": [],
                        "truncated": False,
                    },
                    {
                        "attributeSeq": 12,
                        "attributeName": "색상",
                        "attributeTypeCodeName": "일반",
                        "classification": "선택형",
                        "status": "unknown",
                        "selected": [],
                        "candidates": [{"attributeValueSeq": 1201, "minAttributeValue": "블랙"}],
                        "truncated": False,
                    },
                ],
            }

        d = {
            "name": "도자기 머그컵",
            "salePrice": 15000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
        }
        payload = register.prepare_listing(d, attributes_fn=fake_attr_fn)
        assert attr_fn_calls, "속성 제안 함수가 호출되지 않았다(카테고리 확정 시 1회)"
        assert payload["attributes_suggestion"] == [{"attributeSeq": 11, "attributeValueSeq": 1101}]
        assert payload["attributes_suggestion_basis"], "근거 없음"
        # 일치 없는 속성의 후보는 needs_user 로 드러난다(조용한 생략 금지).
        assert any(
            isinstance(n, dict) and n.get("field") == "attributes" for n in payload["needs_user"]
        ), f"속성 후보 needs_user 없음: {payload['needs_user']}"

    def test_attributes_error_surfaced_not_blocking(self, isolated_prepared_dir, monkeypatch):
        """조회 실패는 attributes_error 로 드러나되 준비는 성공해야 한다."""
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)

        def failing_attr_fn(category_id, product_text):
            return {"ok": False, "suggestions": None, "error": "속성 목록 조회 실패: 테스트"}

        d = {
            "name": "제안실패상품",
            "salePrice": 12000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
        }
        payload = register.prepare_listing(d, attributes_fn=failing_attr_fn)
        assert payload["attributes_error"], "조회 실패가 조용히 삼켜졌다"
        assert "attributes_suggestion" not in payload

    def test_tags_suggestion_reason_when_no_material(self):
        """재료가 없으면 사유(reason) 키로 드러난다(조용한 생략 금지)."""
        suggestion = register._build_seo_tags_suggestion("", [], "")
        assert suggestion["tags"] == []
        assert suggestion["reason"], "제안 불가 사유가 없다"


# =========================================================================== #
# (b) register 자동 채용: filled_from_prepared 표기 + 명시 인자 우선.
# =========================================================================== #
class TestRegisterAutoAdoption:
    def _write_prepared(self, name, price, *, suggestion_keys):
        from clossify import qa_agents

        qa = qa_agents.aggregate_qa_results(
            [
                qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
                qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
            ]
        )
        payload_obj = {
            "product_key": register.make_product_key(name, price),
            "version": common.PREPARED_PAYLOAD_VERSION,
            "product": {"name": name, "salePrice": price},
            "images": {"listing_urls": ["http://cdn/a.png"], "detail_urls": []},
            "detail_html": "<html><body>detail</body></html>",
            "qa": qa,
            "needs_llm": [],
            "needs_user": [],
        }
        payload_obj.update(suggestion_keys)
        register.write_prepared_payload(payload_obj)
        return payload_obj

    def test_tags_and_attributes_auto_adopted(self, isolated_prepared_dir, monkeypatch):
        name = "자동채용상품"
        price = 30000
        self._write_prepared(
            name,
            price,
            suggestion_keys={
                "seo_tags_suggestion": {
                    "tags": ["도자기", "머그컵"],
                    "basis": ["name:도자기"],
                    "reason": None,
                },
                "attributes_suggestion": [{"attributeSeq": 11, "attributeValueSeq": 1101}],
            },
        )
        captured: list[dict] = []

        def capturing_build(product, dh, imgs, status="SALE", **kw):
            captured.append(dict(product))
            return _compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name, price=price, category_id="50002366", preview_confirmed=True
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        filled = result.get("filled_from_prepared", [])
        assert "tags" in filled, f"태그 자동 채용이 드러나지 않음: {filled}"
        assert "attributes" in filled, f"속성 자동 채용이 드러나지 않음: {filled}"
        product = captured[0]
        assert product["tags"] == ["도자기", "머그컵"]
        assert product["attributes"] == [{"attributeSeq": 11, "attributeValueSeq": 1101}]

    def test_explicit_tags_and_attributes_win(self, isolated_prepared_dir, monkeypatch):
        name = "명시우선상품"
        price = 31000
        self._write_prepared(
            name,
            price,
            suggestion_keys={
                "seo_tags_suggestion": {"tags": ["제안태그"], "basis": [], "reason": None},
                "attributes_suggestion": [{"attributeSeq": 1, "attributeValueSeq": 2}],
            },
        )
        captured: list[dict] = []

        def capturing_build(product, dh, imgs, status="SALE", **kw):
            captured.append(dict(product))
            return _compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50002366",
            tags=["명시태그"],
            attributes=[{"attributeSeq": 9, "attributeValueSeq": 9}],
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        product = captured[0]
        assert product["tags"] == ["명시태그"], f"명시 태그가 제안에게 짐: {product['tags']}"
        assert product["attributes"][0]["attributeSeq"] == 9, "명시 속성이 제안에게 짐"
        filled = result.get("filled_from_prepared", [])
        # 명시 tags 는 filled 에 없어야 한다. attributes 도 명시값이므로 없어야 한다.
        assert "tags" not in filled
        assert "attributes" not in filled

    def test_no_suggestion_no_adoption(self, isolated_prepared_dir, monkeypatch):
        """제안이 없으면 자동 채용도 없다(창작 금지)."""
        name = "제안없음상품"
        price = 32000
        self._write_prepared(name, price, suggestion_keys={})
        captured: list[dict] = []

        def capturing_build(product, dh, imgs, status="SALE", **kw):
            captured.append(dict(product))
            return _compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name, price=price, category_id="50002366", preview_confirmed=True
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert "tags" not in result.get("filled_from_prepared", [])
        assert "attributes" not in result.get("filled_from_prepared", [])
        assert captured[0].get("tags") == []
        assert "attributes" not in captured[0]


# =========================================================================== #
# (d) Part C — customerBenefit: 문서 근거 기반 인자→payload 매핑.
# =========================================================================== #
class TestCustomerBenefit:
    def _build_payload(self, p):
        # _notice_defaults 는 AS 연락처·원산지가 없으면 fail-closed 로
        # ValueError 를 낸다(기존 확정 거동). 본 클래스의 검증 대상은
        # customerBenefit 매핑이므로 다른 테스트 파일과 같은 전체
        # notice config mock 을 쓴다.
        with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_MOCK):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                return naver_client.build_payload(p, "<html></html>", ["http://x/img.png"])

    def test_default_none_keeps_key_absent(self):
        payload = self._build_payload(
            {"name": "혜택미제공", "categoryId": "50002366", "salePrice": 10000}
        )
        assert "customerBenefit" not in payload["originProduct"], "None 인데 키가 실렸다"

    def test_valid_benefit_mapped_to_origin_product(self):
        benefit = {
            "purchasePointPolicy": {"value": 1, "unitType": "PERCENT"},
            "reviewPointPolicy": {
                "textReviewPoint": 100,
                "photoVideoReviewPoint": 300,
                "startDate": "2026-09-01",
                "endDate": "2026-12-31",
            },
            "giftPolicy": {"presentContent": "사은품 컵받침"},
        }
        payload = self._build_payload(
            {
                "name": "혜택상품",
                "categoryId": "50002366",
                "salePrice": 10000,
                "customer_benefit": benefit,
            }
        )
        carried = payload["originProduct"]["customerBenefit"]
        assert carried["purchasePointPolicy"] == {"value": 1, "unitType": "PERCENT"}
        assert carried["reviewPointPolicy"]["textReviewPoint"] == 100
        assert carried["giftPolicy"] == {"presentContent": "사은품 컵받침"}

    def test_unknown_top_key_rejected(self):
        with pytest.raises(ValueError, match="문서.*없는 키"):
            self._build_payload(
                {
                    "name": "x",
                    "categoryId": "50002366",
                    "salePrice": 10000,
                    "customer_benefit": {"notInDoc": {}},
                }
            )

    def test_discount_value_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="10000000"):
            self._build_payload(
                {
                    "name": "x",
                    "categoryId": "50002366",
                    "salePrice": 10000,
                    "customer_benefit": {
                        "immediateDiscountPolicy": {
                            "discountMethod": {"value": 99999999999, "unitType": "PERCENT"}
                        }
                    },
                }
            )

    def test_bad_unit_type_rejected(self):
        with pytest.raises(ValueError, match="unitType"):
            self._build_payload(
                {
                    "name": "x",
                    "categoryId": "50002366",
                    "salePrice": 10000,
                    "customer_benefit": {"purchasePointPolicy": {"value": 1, "unitType": "YEN"}},
                }
            )

    def test_mcp_register_passes_benefit_to_builder(self, isolated_prepared_dir, monkeypatch):
        """mcp register_product(customer_benefit=...) 가 빌더까지 흐른다."""
        name = "혜택MCP상품"
        price = 33000
        captured: list[dict] = []

        def capturing_build(product, dh, imgs, status="SALE", **kw):
            captured.append(dict(product))
            return _compliant_payload()

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "build_payload", capturing_build)

        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50002366",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>d</body></html>",
            customer_benefit={"giftPolicy": {"presentContent": "군밤"}},
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert captured[0]["customer_benefit"] == {"giftPolicy": {"presentContent": "군밤"}}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
