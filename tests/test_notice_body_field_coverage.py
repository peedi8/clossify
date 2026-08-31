# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""고시 본문 필드 동적 로딩 검증 (하드 리뷰 과업 (d)-(g) 템플릿/고시 부분).

본 파일은 두 결함의 수정을 검증한다:

**결함 (d) 고시 본문 필드 누락**: 과거 ``listing_templates`` 의
``_NOTICE_BODY_FIELD_CANDIDATES`` 가 17개 필드를 하드코딩했으나,
``data/notice_types.json`` 의 verified 35타입에 선언된 필드 합집합은
약 120개였다. 식품·화장품 판매자는 템플릿을 저장해도 대부분의 필드가
조용히 버려지는 결함이 있었다.

이제 ``_notice_body_field_candidates()`` 가 정본(``notice_types.json``) 에서
필드를 읽는다 — ``naver_client._load_notice_type_specs`` 와 같은 단일 진실
공급원.

과업 매항:
  (d) FOOD/DIET_FOOD/COSMETIC 고시 필드가 템플릿에 저장된다 (과거 누락).
  (e) ``itemName``/``productInfoProvidedNoticeType``/``name`` 은 여전히 제외.
  (f) 비밀값/상품명/가격/이미지 는 여전히 제외 (회귀 없음).
  (g) 자동 적용 금지/사용자값 덮어쓰기 금지/출처 추적 회귀 없음.

모든 테스트는 ``common.STATE_DIR`` 을 tmp_path 로 격리한다.
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

from clossify import common, listing_templates, naver_client


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
    prepared = fake_state / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", prepared)
    return fake_state


@pytest.fixture(autouse=True)
def _reset_candidates_cache(monkeypatch):
    """각 테스트마다 고시 후보 캐시를 리셋 — 테스트 격리."""
    monkeypatch.setattr(listing_templates, "_NOTICE_BODY_FIELD_CANDIDATES_CACHE", None)
    # naver_client 캐시도 리셋 — 테스트가 동일 정본에서 다시 읽게.
    monkeypatch.setattr(naver_client, "_NOTICE_TYPES_CACHE", None)
    monkeypatch.setattr(naver_client, "_NOTICE_TYPE_INDEX", None)
    yield


# =========================================================================== #
# (d) FOOD/DIET_FOOD/COSMETIC 고시 필드가 템플릿에 저장된다.
# =========================================================================== #
class TestNoticeBodyFieldsFromAuthoritativeSource:
    """정본(``notice_types.json``) 의 필드가 동적으로 후보가 된다."""

    def test_certificate_details_uses_cert_detail_without_regressing_model_name(self):
        product = {
            "cert_detail": "KCC-REI-XXX",
            "model_name": "M-1",
            "manufacturer": "test-manufacturer",
        }

        candidates = dict(listing_templates._notice_body_field_candidates())
        body = listing_templates._extract_notice_body(product)

        assert "cert_detail" in candidates["certificateDetails"]
        assert body["certificateDetails"] == "KCC-REI-XXX"
        assert body["modelName"] == "M-1"

    def test_candidates_include_all_verified_fields(self):
        """후보 합집합이 정본 verified 35타입의 모든 camelCase 필드를 포함한다."""
        candidates = listing_templates._notice_body_field_candidates()
        candidate_fields = {camel for camel, _ in candidates}
        # 정본에서 필드 합집합을 직접 구한다.
        specs = naver_client._load_notice_type_specs()
        all_fields: set[str] = set()
        for spec in specs:
            for f in spec.get("fields", []):
                all_fields.add(f)
        # itemName/productInfoProvidedNoticeType/name 은 skip 이므로 후보에서
        # 빠질 수 있지만, 그 외 모든 정본 필드는 후보에 있어야 한다.
        skip_keys = listing_templates._NOTICE_BODY_SKIP_KEYS
        expected = {f for f in all_fields if f not in skip_keys}
        missing = expected - candidate_fields
        assert not missing, (
            f"정본에 있지만 후보에 없는 필드: {sorted(missing)} " "(과거 하드코딩 결함이 재현됨)"
        )

    def test_candidate_count_far_exceeds_old_hardcoded_17(self):
        """과거 하드코딩(17개) 보다 훨씬 많은 필드가 후보가 된다."""
        candidates = listing_templates._notice_body_field_candidates()
        # 정본의 고유 camelCase 필드 수. 17보다 월등히 많아야 한다.
        assert len(candidates) > 50, (
            f"후보 필드 수가 너무 적음: {len(candidates)} "
            "(과거 17개 하드코딩 결함이 재현됐을 가능성)"
        )

    def test_food_specific_field_stored_in_template(self, isolated_state_dir):
        """FOOD 타입의 고유 필드(예: foodAdditive, allergen) 가 템플릿에 담긴다."""
        # FOOD 타입의 고유 필드를 정본에서 찾는다.
        specs = naver_client._load_notice_type_specs()
        food_spec = next((s for s in specs if s.get("type") == "FOOD"), None)
        assert food_spec is not None
        food_specific = [
            f
            for f in food_spec.get("fields", [])
            if f
            not in {
                "returnCostReason",
                "noRefundReason",
                "qualityAssuranceStandard",
                "compensationProcedure",
                "troubleShootingContents",
                "itemName",
                "productInfoProvidedNoticeType",
                "name",
            }
        ]
        assert food_specific, "FOOD 타입에 고유 필드가 없음 (테스트 전제 실패)"
        # FOOD 고유 필드를 ``notice.food.<field>`` 자리에 넣어 저장.
        sample_field = food_specific[0]
        product = {
            "name": "식품샘플",
            "salePrice": 15000,
            "notice": {
                "productInfoProvidedNoticeType": "FOOD",
                "food": {sample_field: f"테스트값-{sample_field}"},
            },
        }
        result = listing_templates.save_template(
            name="food-fields", notice_type="FOOD", product=product
        )
        # notice_field_summary 에 FOOD 필드가 있다.
        summary = result.get("notice_field_summary") or {}
        filled = summary.get("filled_fields") or []
        assert (
            sample_field in filled
        ), f"FOOD 필드 {sample_field!r} 이(가) 저장 안 됨 (과거 결함 재현): {filled}"

    def test_diet_food_specific_field_stored(self, isolated_state_dir):
        """DIET_FOOD 타입의 고유 필드도 템플릿에 담긴다."""
        specs = naver_client._load_notice_type_specs()
        diet_spec = next((s for s in specs if s.get("type") == "DIET_FOOD"), None)
        assert diet_spec is not None
        diet_fields = set(diet_spec.get("fields", []))
        # DIET_FOOD 는 22개 필드로 FOOD(19) 보다 많다 — 그 차이가 담겨야 한다.
        extra = diet_fields - {
            "returnCostReason",
            "noRefundReason",
            "qualityAssuranceStandard",
            "compensationProcedure",
            "troubleShootingContents",
            "itemName",
            "productInfoProvidedNoticeType",
            "name",
        }
        assert extra, "DIET_FOOD 고유 필드가 없음"
        sample = sorted(extra)[0]
        product = {
            "name": "다이어트식품",
            "salePrice": 25000,
            "notice": {
                "productInfoProvidedNoticeType": "DIET_FOOD",
                "dietFood": {sample: f"값-{sample}"},
            },
        }
        result = listing_templates.save_template(
            name="diet-fields", notice_type="DIET_FOOD", product=product
        )
        summary = result.get("notice_field_summary") or {}
        filled = summary.get("filled_fields") or []
        assert sample in filled, f"DIET_FOOD 필드 {sample!r} 이(가) 저장 안 됨: {filled}"

    def test_cosmetic_specific_field_stored(self, isolated_state_dir):
        """COSMETIC 타입의 고유 필드도 템플릿에 담긴다."""
        specs = naver_client._load_notice_type_specs()
        cosmetic_spec = next((s for s in specs if s.get("type") == "COSMETIC"), None)
        assert cosmetic_spec is not None
        cosmetic_fields = set(cosmetic_spec.get("fields", []))
        extra = cosmetic_fields - {
            "returnCostReason",
            "noRefundReason",
            "qualityAssuranceStandard",
            "compensationProcedure",
            "troubleShootingContents",
            "itemName",
            "productInfoProvidedNoticeType",
            "name",
        }
        assert extra, "COSMETIC 고유 필드가 없음"
        sample = sorted(extra)[0]
        product = {
            "name": "화장품",
            "salePrice": 30000,
            "notice": {
                "productInfoProvidedNoticeType": "COSMETIC",
                "cosmetic": {sample: f"값-{sample}"},
            },
        }
        result = listing_templates.save_template(
            name="cosmetic-fields", notice_type="COSMETIC", product=product
        )
        summary = result.get("notice_field_summary") or {}
        filled = summary.get("filled_fields") or []
        assert sample in filled, f"COSMETIC 필드 {sample!r} 저장 안 됨: {filled}"


# =========================================================================== #
# (e) itemName/productInfoProvidedNoticeType/name 은 여전히 제외.
# =========================================================================== #
class TestProductSpecificFieldsStillExcluded:
    """상품 특정값(itemName 등) 은 템플릿 본문에서 빠진다."""

    def test_itemname_not_stored_in_notice_body(self, isolated_state_dir):
        """``notice.<node>.itemName`` 은 상품명이므로 본문에서 뺀다."""
        product = {
            "name": "외부상품명",
            "salePrice": 10000,
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "itemName": "내부상품명-이건빠져야함",
                    "returnCostReason": "단순변심",
                },
            },
        }
        result = listing_templates.save_template(
            name="itemname-test", notice_type="ETC", product=product
        )
        # 저장소 파일에 itemName 값이 없다.
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "내부상품명-이건빠져야함" not in raw
        # 본문에는 returnCostReason 만 있다.
        summary = result.get("notice_field_summary") or {}
        filled = summary.get("filled_fields") or []
        assert "returnCostReason" in filled
        assert "itemName" not in filled

    def test_productinfoprovidednoticetype_not_in_body(self, isolated_state_dir):
        """``productInfoProvidedNoticeType`` 은 본문이 아닌 템플릿 메타로 간다."""
        product = {
            "name": "X",
            "salePrice": 10000,
            "notice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "productInfoProvidedNoticeType": "ETC",
                    "returnCostReason": "문구",
                },
            },
        }
        result = listing_templates.save_template(
            name="type-test", notice_type="ETC", product=product
        )
        summary = result.get("notice_field_summary") or {}
        filled = summary.get("filled_fields") or []
        assert "productInfoProvidedNoticeType" not in filled
        assert "returnCostReason" in filled


# =========================================================================== #
# (f) 비밀값/상품명/가격/이미지 는 여전히 제외 (회귀 없음).
# =========================================================================== #
class TestSecretsAndProductValuesStillExcluded:
    """회귀 — 기존 화이트리스트 정책이 동적 로딩 변경 후에도 유지된다."""

    def test_secret_not_in_stored_template(self, isolated_state_dir):
        """비밀값은 어떤 형태든 템플릿에 담기지 않는다."""
        product = {
            "name": "상품",
            "salePrice": 10000,
            "return_cost_reason": "문구",
            "client_secret": "SECRET-TOKEN-XYZ",
            "api_key": "ak-SECRET",
        }
        listing_templates.save_template(name="sec-regress", notice_type="ETC", product=product)
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "SECRET-TOKEN-XYZ" not in raw
        assert "ak-SECRET" not in raw

    def test_product_name_price_image_not_stored(self, isolated_state_dir):
        """상품명·가격·이미지·재고 는 템플릿에 담기지 않는다."""
        product = {
            "name": "상품명-비밀아님",
            "salePrice": 99000,
            "stock": 50,
            "image_sources": ["http://cdn/img.png"],
            "return_cost_reason": "문구",
        }
        listing_templates.save_template(name="pv-regress", notice_type="ETC", product=product)
        raw = listing_templates.templates_path().read_text(encoding="utf-8")
        assert "상품명-비밀아님" not in raw
        assert "99000" not in raw
        assert "http://cdn/img.png" not in raw


# =========================================================================== #
# (g) 자동 적용 금지/사용자값 덮어쓰기 금지/출처 추적 회귀 없음.
# =========================================================================== #
class TestNoAutoApplyNoOverrideSourceTrackingRegression:
    """동적 필드 로딩 변경이 기존 안전 정책을 깨뜨리지 않는다."""

    def test_empty_name_still_applies_nothing(self, isolated_state_dir):
        """빈 이름 → 어떤 템플릿도 적용되지 않는다 (자동 적용 금지 회귀 없음)."""
        listing_templates.save_template(
            name="auto-test",
            notice_type="ETC",
            product={"return_cost_reason": "문구"},
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="", product=product)
        assert result["applied"] is False
        assert "return_cost_reason" not in product

    def test_user_value_not_overwritten(self, isolated_state_dir):
        """사용자가 준 값을 템플릿이 덮어쓰지 않는다 (빈 자리만 채운다)."""
        listing_templates.save_template(
            name="override-regress",
            notice_type="ETC",
            product={"return_cost_reason": "템플릿문구"},
        )
        product = {
            "name": "X",
            "salePrice": 1000,
            "return_cost_reason": "내가직접",
        }
        result = listing_templates.apply_template(name="override-regress", product=product)
        assert product["return_cost_reason"] == "내가직접"
        # skipped_existing 에 returnCostReason 이 있다.
        skipped = {(s["section"], s["field"]) for s in result["skipped_existing"]}
        assert any(f == "returnCostReason" for _, f in skipped)

    def test_apply_result_carries_source(self, isolated_state_dir):
        """적용 결과에 어느 템플릿에서 왔는지 출처가 드러난다."""
        listing_templates.save_template(
            name="출처-회귀",
            notice_type="ETC",
            product={"return_cost_reason": "문구"},
        )
        product = {"name": "X", "salePrice": 1000}
        result = listing_templates.apply_template(name="출처-회귀", product=product)
        assert result["applied"] is True
        assert result["template_name"] == "출처-회귀"
        assert result["notice_type"] == "ETC"
        assert len(result["filled"]) >= 1


# =========================================================================== #
# 가시성 — notice_field_summary 가 결과에 있다 (조용한 누락 방지).
# =========================================================================== #
class TestNoticeFieldSummaryVisibility:
    """``save_template`` 반환에 ``notice_field_summary`` 가 있다."""

    def test_summary_has_filled_count_and_candidate_total(self, isolated_state_dir):
        """요약에 filled_count/candidate_total/filled_fields 가 있다."""
        product = {
            "name": "X",
            "salePrice": 10000,
            "return_cost_reason": "문구1",
            "no_refund_reason": "문구2",
        }
        result = listing_templates.save_template(
            name="visibility", notice_type="ETC", product=product
        )
        summary = result.get("notice_field_summary")
        assert summary is not None, "notice_field_summary 키가 없음 (조용한 누락)"
        assert "filled_count" in summary
        assert "candidate_total" in summary
        assert "filled_fields" in summary
        assert summary["filled_count"] == 2
        assert summary["candidate_total"] > 50
        assert "returnCostReason" in summary["filled_fields"]
        assert "noRefundReason" in summary["filled_fields"]

    def test_summary_filled_fields_are_names_not_values(self, isolated_state_dir):
        """filled_fields 는 필드 *이름* 만 담는다 — 값은 없다 (비밀값 안전)."""
        sensitive_value = "민감한반품문구-CANARY"
        product = {
            "name": "X",
            "salePrice": 10000,
            "return_cost_reason": sensitive_value,
        }
        result = listing_templates.save_template(
            name="safe-names", notice_type="ETC", product=product
        )
        flat = json.dumps(result, ensure_ascii=False)
        assert sensitive_value not in flat
        assert "returnCostReason" in flat

    def test_summary_shows_zero_when_no_notice_values(self, isolated_state_dir):
        """고시 본문에 값이 없으면 filled_count=0 이다 (조용한 빈 값 금지)."""
        product = {"name": "X", "salePrice": 10000}
        result = listing_templates.save_template(
            name="empty-notice", notice_type="ETC", product=product
        )
        summary = result.get("notice_field_summary") or {}
        assert summary.get("filled_count") == 0
        assert summary.get("filled_fields") == []
