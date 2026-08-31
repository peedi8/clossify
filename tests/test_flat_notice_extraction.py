# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""평평한 notice dict 가 조용히 버려지는 결함 검증 (FIX-flat-notice-silent-drop).

워크오더: ``clossify-ops/tickets/FIX-flat-notice-silent-drop.md``

결함: ``prepare_listing(product={"notice": {"returnCostReason": "A", ...}})``
처럼 **평평한 고시 dict** 를 넘기면 값이 전부 버려지고, needs_user 가 사용자가
방금 준 값을 "없다"고 말했다(조용한 실패).

계약: ``naver_client._merge_notice`` 는 평평한 dict 를 받는다(노드 우선,
etc/furniture 폴백, 마지막으로 평탄 본문). 본 테스트는 두 소비자가 같은
입력 계약을 갖게 만든 것을 증명한다:

  (a) 평평한 notice → 필드 추출됨 (listing_templates._extract_notice_body)
  (b) 노드 중첩 형태 회귀
  (c) 최상위 snake_case 형태 회귀
  (d) 노드값과 평평한 값 동시 존재 시 노드값 우선
  (e) 후보 목록 밖 키는 통과하지 않음
  (f) _NOTICE_BODY_SKIP_KEYS 는 평평한 자리에서도 제외
  (g) ★ 끝단 — mcp_server.prepare_listing 평평한 notice 로 호출 시
      needs_user 에 그 필드들이 사라진다 (이미지는 네이버 CDN URL 로
      재업로드 회피 — images.py "이미 네이버 CDN URL → 재업로드 금지").
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

from clossify import common, listing_templates, mcp_server, naver_client

# 네이버 CDN URL — attach_images 가 재업로드 없이 통과시킨다(images.py 규칙).
_CDN_IMAGE = "https://shop-phinf.pstatic.net/testflat/image.png"


@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """STATE_DIR/PREPARED_DIR 을 tmp_path 로 격리."""
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
    monkeypatch.setattr(naver_client, "_NOTICE_TYPES_CACHE", None)
    monkeypatch.setattr(naver_client, "_NOTICE_TYPE_INDEX", None)
    yield


def _empty_notice_config():
    """공통 고시 5필드가 config 에 전혀 없는 mock 컨텍스트."""
    return (
        mock.patch.object(naver_client, "_notice_config", return_value={}),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(common, "cfg", return_value={"smartstore_notice_defaults": {}}),
    )


# =========================================================================== #
# (a)~(f) listing_templates._extract_notice_body — 입력 계약 통일.
# =========================================================================== #
class TestExtractNoticeBodyFlatDict:
    def test_a_flat_notice_dict_fields_extracted(self):
        """(a) 평평한 ``{"notice": {"returnCostReason": "A", ...}}`` → 추출된다."""
        body = listing_templates._extract_notice_body(
            {"notice": {"returnCostReason": "A", "noRefundReason": "B"}}
        )
        assert body.get("returnCostReason") == "A"
        assert body.get("noRefundReason") == "B"

    def test_b_node_nested_form_still_works(self):
        """(b) 노드 중첩 ``{"notice": {"etc": {...}}}`` 회귀 없음."""
        body = listing_templates._extract_notice_body(
            {"notice": {"etc": {"returnCostReason": "노드값"}}}
        )
        assert body.get("returnCostReason") == "노드값"

    def test_c_top_level_snake_case_still_works(self):
        """(c) 최상위 snake_case ``{"return_cost_reason": ...}`` 회귀 없음."""
        body = listing_templates._extract_notice_body({"return_cost_reason": "최상위값"})
        assert body.get("returnCostReason") == "최상위값"

    def test_d_node_value_beats_flat_value(self):
        """(d) 노드값과 평평한 값이 동시에 있으면 노드값이 이긴다."""
        body = listing_templates._extract_notice_body(
            {
                "notice": {
                    "etc": {"returnCostReason": "노드값"},
                    "returnCostReason": "평평값",
                }
            }
        )
        assert body.get("returnCostReason") == "노드값"

    def test_e_unknown_keys_not_passed_through(self):
        """(e) 후보 목록에 없는 키(notAFieldXyz)는 담기지 않는다."""
        body = listing_templates._extract_notice_body(
            {"notice": {"returnCostReason": "A", "notAFieldXyz": "X"}}
        )
        assert "notAFieldXyz" not in body
        assert body.get("returnCostReason") == "A"

    def test_f_skip_keys_excluded_in_flat_position(self):
        """(f) _NOTICE_BODY_SKIP_KEYS(itemName 등)는 평평한 자리에서도 제외."""
        body = listing_templates._extract_notice_body(
            {"notice": {"returnCostReason": "A", "itemName": "상품특정명"}}
        )
        assert "itemName" not in body
        assert body.get("returnCostReason") == "A"

    def test_notice_type_keys_skipped_in_flat_position(self):
        """타입 메타 키(productInfoProvidedNoticeType/notice_type)는 본문에서 뺀다."""
        body = listing_templates._extract_notice_body(
            {
                "notice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "notice_type": "ETC",
                    "returnCostReason": "A",
                }
            }
        )
        assert "productInfoProvidedNoticeType" not in body
        assert "notice_type" not in body
        assert body.get("returnCostReason") == "A"

    def test_empty_and_blank_flat_values_not_extracted(self):
        """빈 문자열/공백 평평 값은 담지 않는다(값 없음과 값 있음 구분)."""
        body = listing_templates._extract_notice_body(
            {"notice": {"returnCostReason": "   ", "noRefundReason": ""}}
        )
        assert "returnCostReason" not in body
        assert "noRefundReason" not in body


# =========================================================================== #
# (g) ★ 끝단 — mcp_server.prepare_listing 평평한 notice → needs_user 에서 사라짐.
# =========================================================================== #
class TestPrepareListingFlatNoticeEndToEnd:
    def test_g_flat_notice_not_reported_missing_in_needs_user(self, isolated_state_dir):
        """(g) 평평한 notice 로 호출하면 그 필드들이 needs_user 에 없다."""
        product = {
            "name": "평평고시끝단테스트",
            "salePrice": 10000,
            "image_sources": [_CDN_IMAGE],
            "category_id": "50001060",
            "notice": {"returnCostReason": "A", "noRefundReason": "B"},
        }
        with (
            _empty_notice_config()[0],
            _empty_notice_config()[1],
            _empty_notice_config()[2],
        ):
            result = mcp_server.prepare_listing(product)

        assert result.get("ok") is True, f"준비 실패: {result.get('error')}"
        fields_reported = {
            str(item.get("field") or "")
            for item in (result.get("needs_user") or [])
            if isinstance(item, dict)
        }
        assert "returnCostReason" not in fields_reported, (
            "사용자가 평평한 notice 로 준 returnCostReason 을 needs_user 가 "
            f"'없다'고 보고한다(조용한 실패): {sorted(fields_reported)}"
        )
        assert "noRefundReason" not in fields_reported

    def test_g_control_empty_notice_still_reported(self, isolated_state_dir):
        """대조 — notice 를 비우면 같은 필드들이 needs_user 에 보고된다.

        위 (g) 테스트가 "애초에 그 필드가 needs_user 에 안 오른다" 로 허위
        통과하지 않는지 확인하는 이빨 역할.
        """
        product = {
            "name": "빈고시대조테스트",
            "salePrice": 10000,
            "image_sources": [_CDN_IMAGE],
            "category_id": "50001060",
            "notice": {},
        }
        with (
            _empty_notice_config()[0],
            _empty_notice_config()[1],
            _empty_notice_config()[2],
        ):
            result = mcp_server.prepare_listing(product)

        fields_reported = {
            str(item.get("field") or "")
            for item in (result.get("needs_user") or [])
            if isinstance(item, dict)
        }
        assert "returnCostReason" in fields_reported, (
            "대조군(빈 notice) 에서 returnCostReason 이 보고되지 않는다 — "
            f"(g) 테스트의 이빨이 없다: {sorted(fields_reported)}"
        )


# =========================================================================== #
# naver_client 공유 판정기 — _merge_notice 와 같은 평평 dict 계약.
# =========================================================================== #
class TestNoticeCommonUserBodiesFlatContract:
    def test_flat_notice_counted_as_provided(self):
        """평평한 notice 의 공통 필드는 '상품 입력이 제공함' 으로 판정된다."""
        p = {"notice": {"returnCostReason": "A"}}
        assert (
            naver_client._notice_common_field_provided_by_product(
                p, "returnCostReason", ("return_cost_reason",)
            )
            is True
        )

    def test_node_body_still_found(self):
        """노드 본문 회귀 — 기존 형태도 여전히 발견된다."""
        p = {"notice": {"etc": {"returnCostReason": "노드값"}}}
        assert (
            naver_client._notice_common_field_provided_by_product(
                p, "returnCostReason", ("return_cost_reason",)
            )
            is True
        )

    def test_empty_flat_notice_not_provided(self):
        """빈 notice 는 여전히 '미제공' 이다 (빈 값 유효 입력 둔갑 금지)."""
        p = {"notice": {"returnCostReason": "   "}}
        assert (
            naver_client._notice_common_field_provided_by_product(
                p, "returnCostReason", ("return_cost_reason",)
            )
            is False
        )
