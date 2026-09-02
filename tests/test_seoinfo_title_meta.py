# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""seoInfo pageTitle·metaDescription 자동 작성(가산점 필드).

문서 근거(창작 금지 — 보유 API 정본 「(v2) 상품 등록」 스키마 실측 2026-09-02):
  originProduct.detailAttribute.seoInfo 오브젝트:
    - pageTitle (string, <= 100 characters)
    - metaDescription (string, <= 160 characters)
    - sellerTags (기존 배선 있음)
  미입력 시 플랫폼 기본값이 적용되는 가산점 필드 — 실패해도 등록은 막히지
  않는다. 따라서 길이 초과는 거부가 아니라 단어 경계 절단 + truncated 표기.

인수조건 대응:
  (a) prepare: 정상 입력 → 두 값 생성, 각 상한 이내, basis 표기.
  (b) meta_description 에 금지어 미포함(금지어 입력 케이스).
  (c) register 자동 채용 + filled_from_prepared 표기 / 명시 인자 우선 /
      미제안 시 미탑재.
  (d) payload 의 seoInfo 에 pageTitle·metaDescription·sellerTags 공존(병합).
  (e) 초과 길이 입력 → 절단+truncated 표기, 등록 미차단.
"""

from __future__ import annotations

import sys
from pathlib import Path

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
    """컴플라이언스 게이트·고시 타입 판정만 모킹(확립된 패턴 재사용)."""
    monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)
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
    # 실제 build_payload 를 쓰는 케이스(절단 검증) 에서 트립와이어가 카테고리
    # 메타의 고시 타입(HOME_APPLIANCES) 때문에 차단하지 않도록 판정만 모킹.
    monkeypatch.setattr(mcp_server, "_payload_notice_type", lambda payload: "ETC")
    monkeypatch.setattr(naver_client, "register_product", lambda p: {"ok": True})


# =========================================================================== #
# (a) prepare: seo_meta_suggestion 두 값 + 상한 + basis.
# =========================================================================== #
class TestPrepareSeoMetaSuggestion:
    def test_both_values_within_limits_with_basis(self, isolated_prepared_dir, monkeypatch):
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
        result = mcp_server.prepare_listing(
            {
                "name": "손으로 빚은 무광 도자기 식기",
                "salePrice": 15000,
                "image_sources": ["a.png"],
                "options": [
                    {"name": "긴접시", "stock": 2},
                    {"name": "맛종지", "stock": 2},
                ],
            }
        )
        assert result["ok"] is True, f"prepare 실패: {result}"
        suggestion = result["seo_meta_suggestion"]
        assert isinstance(suggestion, dict)
        assert isinstance(suggestion["page_title"], str) and suggestion["page_title"].strip()
        assert len(suggestion["page_title"]) <= naver_client.SEO_PAGE_TITLE_MAX_LEN
        assert (
            isinstance(suggestion["meta_description"], str)
            and suggestion["meta_description"].strip()
        )
        assert len(suggestion["meta_description"]) <= naver_client.SEO_META_DESCRIPTION_MAX_LEN
        assert suggestion["basis"], "basis(근거) 없음"
        assert isinstance(suggestion["note"], str) and suggestion["note"]
        # 옵션 요약 재료가 메타 설명에 반영된다(로컬 규칙 조립).
        assert "긴접시" in suggestion["meta_description"]

    def test_suggestion_persisted_in_prepared_payload(self, isolated_prepared_dir, monkeypatch):
        """제안은 prepared payload 에 저장된다 — 등록 단계 자동 채용 근거."""
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
        result = mcp_server.prepare_listing(
            {"name": "무광 도자기 접시", "salePrice": 9000, "image_sources": ["a.png"]}
        )
        assert result["ok"] is True, f"prepare 실패: {result}"
        stored = register.load_prepared_payload(product_key=result["product_key"])
        assert isinstance(stored.get("seo_meta_suggestion"), dict)

    def test_no_name_material_returns_null_with_note(self):
        """재료 부족 시 null + note 사유(조용한 생략 금지)."""
        suggestion = mcp_server._build_seo_meta_suggestion({}, None)
        assert suggestion["page_title"] is None
        assert suggestion["meta_description"] is None
        assert suggestion["note"], "제안 불가 사유가 없다"

    def test_page_title_reuses_seo_title_suggestion(self, isolated_prepared_dir, monkeypatch):
        """page_title 은 seo_title_suggestion.suggested 를 재사용한다(null 이면 name 정제본)."""
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
        result = mcp_server.prepare_listing(
            {"name": "무광 도자기 접시", "salePrice": 9000, "image_sources": ["a.png"]}
        )
        assert result["ok"] is True, f"prepare 실패: {result}"
        suggested = result["seo_title_suggestion"]["suggested"]
        page_title = result["seo_meta_suggestion"]["page_title"]
        if isinstance(suggested, str) and suggested.strip():
            assert page_title == suggested[: naver_client.SEO_PAGE_TITLE_MAX_LEN].strip()
        else:
            assert page_title, "suggested 가 null 이면 name 정제본이라 null 이면 안 된다"


# =========================================================================== #
# (b) 금지어: meta_description 정제기 통과.
# =========================================================================== #
class TestBannedClaimsExcluded:
    def test_banned_words_not_in_meta_description(self, isolated_prepared_dir, monkeypatch):
        monkeypatch.setattr("clossify.images.attach_images", lambda s: _ATTACH_MOCK)
        result = mcp_server.prepare_listing(
            {"name": "프리미엄 최고 도자기 식기", "salePrice": 15000, "image_sources": ["a.png"]}
        )
        assert result["ok"] is True, f"prepare 실패: {result}"
        desc = result["seo_meta_suggestion"]["meta_description"]
        assert desc, "메타 설명이 비어 있다"
        assert "프리미엄" not in desc, f"금지어 잔존: {desc}"
        assert "최고" not in desc, f"금지어 잔존: {desc}"
        title = result["seo_meta_suggestion"]["page_title"]
        assert "프리미엄" not in title, f"금지어 잔존: {title}"


# =========================================================================== #
# (c) register 자동 채용 + 명시 인자 우선 + 미제안 미탑재.
# =========================================================================== #
class TestRegisterSeoMetaAdoption:
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

    def test_auto_adopted_with_filled_from_prepared(self, isolated_prepared_dir, monkeypatch):
        name = "가산점자동상품"
        price = 34000
        self._write_prepared(
            name,
            price,
            suggestion_keys={
                "seo_meta_suggestion": {
                    "page_title": "제안 페이지제목",
                    "meta_description": "제안 메타설명",
                    "basis": ["input-name:x"],
                    "note": "제안",
                }
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
        assert "page_title" in filled, f"pageTitle 자동 채용 미표기: {filled}"
        assert "meta_description" in filled, f"metaDescription 자동 채용 미표기: {filled}"
        assert captured[0]["page_title"] == "제안 페이지제목"
        assert captured[0]["meta_description"] == "제안 메타설명"

    def test_explicit_args_win(self, isolated_prepared_dir, monkeypatch):
        name = "가산점명시상품"
        price = 35000
        self._write_prepared(
            name,
            price,
            suggestion_keys={
                "seo_meta_suggestion": {
                    "page_title": "제안 제목",
                    "meta_description": "제안 설명",
                    "basis": [],
                    "note": "제안",
                }
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
            page_title="명시 제목",
            meta_description="명시 설명",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"등록 실패: {result}"
        assert captured[0]["page_title"] == "명시 제목"
        assert captured[0]["meta_description"] == "명시 설명"
        filled = result.get("filled_from_prepared", [])
        assert "page_title" not in filled, "명시값인데 filled 에 들어갔다"
        assert "meta_description" not in filled, "명시값인데 filled 에 들어갔다"

    def test_no_suggestion_no_adoption(self, isolated_prepared_dir, monkeypatch):
        """제안이 없으면 자동 채용도 없다(창작 금지) — 키 미탑재."""
        name = "가산점제안없음상품"
        price = 36000
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
        assert "page_title" not in result.get("filled_from_prepared", [])
        assert "meta_description" not in result.get("filled_from_prepared", [])
        assert "page_title" not in captured[0]
        assert "meta_description" not in captured[0]

    def test_non_string_arg_rejected(self, isolated_prepared_dir, monkeypatch):
        _setup_dry_run_gate(monkeypatch)
        result = mcp_server.register_product(
            name="형태오류상품",
            price=37000,
            category_id="50002366",
            page_title=123,  # type: ignore[arg-type]
            preview_confirmed=True,
        )
        assert result["ok"] is False
        assert "page_title" in str(result.get("error"))


# =========================================================================== #
# (d)+(e) build_payload: seoInfo 공존 병합 + 초과 절단 표기.
# =========================================================================== #
class TestSeoInfoMergeAndTruncation:
    def _build_payload(self, p):
        from unittest import mock

        with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_MOCK):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                return naver_client.build_payload(p, "<html></html>", ["http://x/img.png"])

    def test_seoinfo_all_three_coexist(self):
        """pageTitle·metaDescription·sellerTags 공존 — 병합이지 덮어쓰기가 아니다."""
        payload = self._build_payload(
            {
                "name": "병합검증상품",
                "categoryId": "50002366",
                "salePrice": 10000,
                "tags": ["도자기", "접시"],
                "page_title": "병합검증 페이지제목",
                "meta_description": "병합검증 메타설명",
            }
        )
        seo_info = payload["originProduct"]["detailAttribute"]["seoInfo"]
        assert [t["text"] for t in seo_info["sellerTags"]] == ["도자기", "접시"]
        assert seo_info["pageTitle"] == "병합검증 페이지제목"
        assert seo_info["metaDescription"] == "병합검증 메타설명"

    def test_absent_values_keep_keys_out(self):
        payload = self._build_payload(
            {"name": "미탑재상품", "categoryId": "50002366", "salePrice": 10000}
        )
        seo_info = payload["originProduct"]["detailAttribute"]["seoInfo"]
        assert "pageTitle" not in seo_info
        assert "metaDescription" not in seo_info
        assert isinstance(seo_info["sellerTags"], list)

    def test_overlong_input_truncated_reported_not_blocked(self, monkeypatch, tmp_path):
        """초과 입력 → 단어 경계 절단 + truncated 표기, 등록 미차단(가산점)."""
        long_title = "제목단어 " * 25  # strip 후 124자 > 100
        long_desc = "설명단어 " * 40  # strip 후 199자 > 160
        captured: list[dict] = []

        def fake_register(payload):
            captured.append(payload)
            return {"ok": True}

        _setup_dry_run_gate(monkeypatch)
        monkeypatch.setattr(naver_client, "register_product", fake_register)

        result = mcp_server.register_product(
            name="절단검증상품",
            price=38000,
            category_id="50002366",
            image_urls=["http://cdn/a.png"],
            detail_html="<html><body>d</body></html>",
            page_title=long_title,
            meta_description=long_desc,
            preview_confirmed=True,
        )
        # 가산점 필드 초과가 등록을 막지 않는다.
        assert result["ok"] is True, f"절단이 등록을 막았다: {result}"
        truncated = result.get("seo_meta_truncated", [])
        assert "page_title" in truncated, f"절단 미표기: {truncated}"
        assert "meta_description" in truncated, f"절단 미표기: {truncated}"
        # 실제 전송 페이로드의 seoInfo 값은 상한 이내로 절단돼 있다.
        sent = captured[0]["originProduct"]["detailAttribute"]["seoInfo"]
        assert len(sent["pageTitle"]) <= naver_client.SEO_PAGE_TITLE_MAX_LEN
        assert len(sent["metaDescription"]) <= naver_client.SEO_META_DESCRIPTION_MAX_LEN
        # 절단은 단어 경계 — 값이 공백으로 끝나거나 비지 않는다.
        assert sent["pageTitle"].strip()
        assert sent["metaDescription"].strip()

    def test_truncation_helper_word_boundary(self):
        text = "가나다라 마바사아 자차카타 타파하"
        cut, truncated = naver_client._truncate_seo_text(text, 10)
        assert truncated is True
        assert len(cut) <= 10
        assert not cut.endswith(" "), "단어 경계 절단이 아니다"
        # 상한 이내 입력은 그대로.
        same, truncated2 = naver_client._truncate_seo_text("짧은문장", 100)
        assert same == "짧은문장" and truncated2 is False


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
