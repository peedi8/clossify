# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""네이버 커머스 API 등록 페이로드의 최상위 구조(shape) 를 검증한다.

회귀 대상: ``smartstoreChannelProduct`` 블록이 ``originProduct`` 안에 잘못
중첩되어 있었다. 네이버 커머스 API ``POST /external/v2/products`` 는 이 블록을
``originProduct`` 와 동일한 최상위 형제로 요구한다:

    {"originProduct": {...}, "smartstoreChannelProduct": {...}}

중첩된 위치에서는 API 가 최상위를 보고 빈 것으로 간주해 HTTP 400
``smartstoreChannelProduct / NotNull`` 로 거절한다. 본 테스트는:

  1. 빌드된 페이로드에 ``smartstoreChannelProduct`` 가 최상위에 있다.
  2. ``originProduct`` 에는 ``smartstoreChannelProduct`` 키가 없다.
  3. 블록 내용(``naverShoppingRegistration`` /
     ``channelProductDisplayStatusType``) 과 display 전달은 보존된다.
  4. HTTP 계층에서 송신되는 요청 본문도 최상위에 블록을 싣는다
     (빌드 결과가 아닌 실제 송신 대상에 대한 단언).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원: 프로젝트 루트를 path 에 추가.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client


def _make_product(**overrides) -> dict:
    """build_payload 에 넘길 최소 상품 dict (display 등 오버라이드 허용)."""
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
    with mock.patch.object(naver_client, "_notice_config", return_value={}):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            return naver_client.build_payload(p, "<html></html>", ["http://x/img.png"])


# ============================================================================ #
# (1) smartstoreChannelProduct 는 최상위 형제.
# ============================================================================ #
class TestSmartStoreChannelProductTopLevel:
    """``smartstoreChannelProduct`` 가 페이로드 최상위에 있는가."""

    def test_block_present_at_top_level(self):
        payload = _build_payload(_make_product())
        assert "smartstoreChannelProduct" in payload, (
            "smartstoreChannelProduct 가 페이로드 최상위에 있어야 한다 "
            "(originProduct 내부가 아님)."
        )

    def test_block_not_nested_in_origin_product(self):
        payload = _build_payload(_make_product())
        origin = payload.get("originProduct", {})
        assert isinstance(origin, dict)
        assert (
            "smartstoreChannelProduct" not in origin
        ), "smartstoreChannelProduct 가 originProduct 안에 중첩되어 있으면 안 된다."

    def test_block_is_sibling_of_origin_product(self):
        payload = _build_payload(_make_product())
        assert "originProduct" in payload
        assert "smartstoreChannelProduct" in payload
        # 형제 조건: 두 키가 모두 최상위에 존재.
        assert payload["originProduct"] is not payload["smartstoreChannelProduct"]


# ============================================================================ #
# (2) 블록 내용 보존 — naverShoppingRegistration + channelProductDisplayStatusType.
# ============================================================================ #
class TestSmartStoreChannelProductContents:
    """블록 내용이 변경 없이 보존되는가."""

    def test_block_has_expected_keys(self):
        payload = _build_payload(_make_product())
        block = payload["smartstoreChannelProduct"]
        assert isinstance(block, dict)
        assert set(block.keys()) == {
            "naverShoppingRegistration",
            "channelProductDisplayStatusType",
        }, f"블록 키가 예상과 다르다: {set(block.keys())}"

    def test_naver_shopping_registration_true(self):
        payload = _build_payload(_make_product())
        assert payload["smartstoreChannelProduct"]["naverShoppingRegistration"] is True

    def test_display_defaults_to_on_for_sale_status(self):
        payload = _build_payload(_make_product())
        assert (
            payload["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "ON"
        ), "status=SALE 일 때 display 기본값은 ON 이어야 한다."

    def test_display_defaults_to_suspension_for_suspension_status(self):
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                suspended = naver_client.build_payload(
                    _make_product(), "<html></html>", ["http://x/img.png"], status="SUSPENSION"
                )
        assert (
            suspended["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "SUSPENSION"
        ), "status=SUSPENSION 일 때 display 기본값은 SUSPENSION 이어야 한다 (OFF 는 NotValidEnum)."

    def test_display_input_passed_through(self):
        payload = _build_payload(_make_product(display="OFF"))
        assert (
            payload["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "OFF"
        ), "명시적 display 입력값이 그대로 전달되어야 한다."

    def test_display_explicit_input_overrides_default(self):
        # status=SALE 이면 기본 ON 이지만, 명시 입력 OFF 가 우선.
        payload = _build_payload(_make_product(display="OFF"))
        assert payload["smartstoreChannelProduct"]["channelProductDisplayStatusType"] == "OFF"


# ============================================================================ #
# (3) HTTP 계층 송신 본문 — _post_product_payload 에 도달한 페이로드를 검증.
#     build_payload 결과가 아니라 "실제로 와이어에 올라가는 본문" 을 단언.
# ============================================================================ #
class TestOutgoingRequestBody:
    """``register_product`` 가 HTTP 로 송신하는 본문에 블록이 최상위로 있는가.

    ``naver_client.register_product`` 는 ``build_payload`` 결과를 깊은 복사한 뒤
    내부 메타 키를 제거하고 ``_post_product_payload`` 로 보낸다. 따라서
    ``_post_product_payload`` 에 도달한 ``payload`` 인자가 와이어 본문의 구조와
    동일하다. 이 단언이 "빌드 결과가 아니라 송신 결과" 를 커버한다.
    """

    def test_sent_body_carries_block_at_top_level(self):
        built = _build_payload(_make_product(display="ON"))
        captured: list = []

        def _fake_post(payload, tk):
            captured.append(payload)
            return 200, {"originProductNo": "TEST-1"}

        with mock.patch.object(naver_client, "get_token", lambda: "t"):
            with mock.patch.object(naver_client, "_post_product_payload", _fake_post):
                naver_client.register_product(built)

        assert len(captured) == 1, "송신은 정확히 1회"
        sent = captured[0]
        assert (
            "smartstoreChannelProduct" in sent
        ), "송신 본문의 최상위에 smartstoreChannelProduct 가 있어야 한다."
        assert "smartstoreChannelProduct" not in sent.get(
            "originProduct", {}
        ), "송신 본문의 originProduct 안에 중첩되어 있으면 안 된다."
        block = sent["smartstoreChannelProduct"]
        assert block["naverShoppingRegistration"] is True
        assert block["channelProductDisplayStatusType"] == "ON"

    def test_sent_body_strips_internal_meta_but_keeps_block(self):
        """내부 메타(_kcWarning 등)는 송신 본문에서 빠지지만,
        smartstoreChannelProduct 는 API 계약상 최상위에 남아야 한다."""
        built = _build_payload(_make_product())
        # 내부 메타를 인위적으로 붙여 송신 전 제거 여부와 블록 보존 여부를 함께 검증.
        built["_kcWarning"] = "테스트 경고"
        captured: list = []

        def _fake_post(payload, tk):
            captured.append(payload)
            return 200, {"originProductNo": "TEST-2"}

        with mock.patch.object(naver_client, "get_token", lambda: "t"):
            with mock.patch.object(naver_client, "_post_product_payload", _fake_post):
                naver_client.register_product(built)

        sent = captured[0]
        assert "_kcWarning" not in sent, "내부 메타는 송신 본문에서 제거되어야 한다."
        assert (
            "smartstoreChannelProduct" in sent
        ), "내부 메타 제거 과정에서 API 계약 블록까지 사라지면 안 된다."


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
