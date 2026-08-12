# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""배송비 config 폴백 + 설정 유래 보고 확장.

배송비 config 폴백:
  - 배송비(baseFee) 결정: ``p.delivery_fee``(명시) → ``config.delivery_fee``
    (snake/camel 모두) → 기존 기본값 3000.
  - 상거래 조건이므로 fail-closed 로 만들지 않는다 — 다만 어디서 왔는지 보고.

설정 유래 보고:
  - ``notice_filled_from_config`` 에 공통 5필드 외 아래도 포함:
    ``origin_content``·``importer``·``manufacturer``·``delivery_fee``.
  - **상품 입력에 없고 config 에서만 채워졌을 때** 보고에 등장.
  - 상품 입력에 있으면 보고에 없다(입력 우선).

판단 근거(주석):
  이 필드들은 상품마다 달라야 하는 규제값/상거래 조건인데 스토어 서랍(config)
  에 있어서, 묻지 않고 채워지면 잘못 신고된다. 그래서 **차단이 아니라 가시화**로
  다룬다 — config 유래면 보고에 등장시켜 사용자가 확인하게 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #


def _make_product(extra=None):
    """테스트용 상품 dict. 최소한의 필드만. config 폴백/보고 필드는 extra 로 주입."""
    p = {
        "name": "테스트상품",
        "categoryId": "50000000",
        "salePrice": 30000,
        "origin_code": "04",
        "made_in": "중국",
    }
    if extra:
        p.update(extra)
    return p


def _build_payload(p, cfg):
    """notice_config 를 cfg 로 고정하고 build_payload 실행. 네트워크 없음."""
    with mock.patch.object(naver_client, "_notice_config", return_value=cfg):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            return naver_client.build_payload(p, "<html></html>", ["http://x.png"])


def _base_fee(payload):
    """payload → originProduct.deliveryInfo.deliveryFee.baseFee."""
    return (
        payload.get("originProduct", {})
        .get("deliveryInfo", {})
        .get("deliveryFee", {})
        .get("baseFee")
    )


def _filled_from_config(payload):
    """payload → notice_filled_from_config (없으면 빈 리스트)."""
    return payload.get("notice_filled_from_config") or []


# =========================================================================== #
# 배송비 config 폴백.
# =========================================================================== #


class TestDeliveryFeeConfigFallback:
    """배송비(baseFee) 결정: 명시 → config → 3000."""

    def test_explicit_delivery_fee_wins(self):
        """p.delivery_fee 명시 → 그 값이 baseFee."""
        p = _make_product({"delivery_fee": 5000})
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 4000}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 5000, "명시값이 config 보다 우선해야 함"

    def test_config_delivery_fee_when_no_explicit(self):
        """p.delivery_fee 없고 config 만 있으면 → config 값."""
        p = _make_product()
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 4000}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 4000, "config 값이 사용되어야 함"

    def test_camel_case_config_key(self):
        """camelCase deliveryFee 도 받는다(기존 패턴 동일)."""
        p = _make_product()
        cfg = {"origin_area_code": "04", "origin_content": "중국", "deliveryFee": 4500}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 4500, "camelCase config 키가 작동해야 함"

    def test_default_3000_when_neither(self):
        """명시도 없고 config 도 없으면 → 3000 (기존 동작 유지)."""
        p = _make_product()
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 3000, "기본값 3000 이 유지되어야 함"

    def test_explicit_zero_is_respected(self):
        """p.delivery_fee=0 (무료배송) 명시 → 0. config 폴백이 덮어쓰지 않는다."""
        p = _make_product({"delivery_fee": 0})
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 4000}
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 0, "명시 0(무료배송) 이 config 에게 지면 안 됨"


# =========================================================================== #
# 배송비 config 폴백 + 설정 유래 보고 — delivery_fee 보고.
# =========================================================================== #


class TestDeliveryFeeReporting:
    """delivery_fee 가 config 유래일 때 notice_filled_from_config 에 등장."""

    def test_delivery_fee_reported_when_from_config(self):
        """상품 입력에 없고 config 에만 있으면 → 보고에 등장."""
        p = _make_product()  # delivery_fee 없음
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 4000}
        payload = _build_payload(p, cfg)
        assert "delivery_fee" in _filled_from_config(
            payload
        ), "config 유래 delivery_fee 가 보고에 없음"

    def test_delivery_fee_not_reported_when_explicit(self):
        """상품 입력에 명시값이 있으면 → 보고에 없다(입력 우선)."""
        p = _make_product({"delivery_fee": 5000})
        cfg = {"origin_area_code": "04", "origin_content": "중국", "delivery_fee": 4000}
        payload = _build_payload(p, cfg)
        assert "delivery_fee" not in _filled_from_config(
            payload
        ), "명시값이 있는데 config 유래로 보고됨"

    def test_delivery_fee_not_reported_when_default_3000(self):
        """명시도 config 도 없으면 → 보고에 없다(기본값은 config 유래가 아님)."""
        p = _make_product()
        cfg = {"origin_area_code": "04", "origin_content": "중국"}
        payload = _build_payload(p, cfg)
        assert "delivery_fee" not in _filled_from_config(
            payload
        ), "기본값 3000 경로인데 config 유래로 보고됨"

    def test_camel_case_config_key_also_reported(self):
        """camelCase deliveryFee config 로 채워졌을 때도 보고에 등장."""
        p = _make_product()
        cfg = {"origin_area_code": "04", "origin_content": "중국", "deliveryFee": 4500}
        payload = _build_payload(p, cfg)
        assert "delivery_fee" in _filled_from_config(
            payload
        ), "camelCase config 유래 delivery_fee 가 보고에 없음"


# =========================================================================== #
# 설정 유래 보고 — origin_content·importer·manufacturer 보고.
# =========================================================================== #


class TestPerProductFieldsReporting:
    """origin_content·importer·manufacturer 가 config 유래일 때 보고."""

    def test_origin_content_reported_when_from_config(self):
        """상품 입력에 made_in/origin_content 없고 config 에만 있으면 → 보고."""
        # made_in 도 origin_content 도 없는 상품.
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
        }
        cfg = {"origin_area_code": "04", "origin_content": "베트남"}
        payload = _build_payload(p, cfg)
        assert "origin_content" in _filled_from_config(
            payload
        ), "config 유래 origin_content 가 보고에 없음"

    def test_origin_content_not_reported_when_product_input(self):
        """상품 입력에 made_in 이 있으면 → 보고에 없다(입력 우선)."""
        p = _make_product({"made_in": "중국"})
        cfg = {"origin_area_code": "04", "origin_content": "베트남"}
        payload = _build_payload(p, cfg)
        assert "origin_content" not in _filled_from_config(
            payload
        ), "상품 입력에 made_in 이 있는데 config 유래로 보고됨"

    def test_origin_content_not_reported_when_product_origin_content(self):
        """상품 입력에 origin_content 직접 키가 있어도 → 보고에 없다."""
        p = _make_product({"origin_content": "한국"})
        cfg = {"origin_area_code": "04", "origin_content": "베트남"}
        payload = _build_payload(p, cfg)
        assert "origin_content" not in _filled_from_config(
            payload
        ), "상품 입력에 origin_content 가 있는데 config 유래로 보고됨"

    def test_importer_reported_when_from_config(self):
        """상품 입력에 importer 없고 config 에만 있으면 → 보고."""
        p = _make_product()
        cfg = {"origin_area_code": "04", "origin_content": "중국", "importer": "(주)수입사"}
        payload = _build_payload(p, cfg)
        assert "importer" in _filled_from_config(payload), "config 유래 importer 가 보고에 없음"

    def test_importer_not_reported_when_product_input(self):
        """상품 입력에 importer 가 있으면 → 보고에 없다(입력 우선)."""
        p = _make_product({"importer": "(주)명시수입사"})
        cfg = {"origin_area_code": "04", "origin_content": "중국", "importer": "(주)수입사"}
        payload = _build_payload(p, cfg)
        assert "importer" not in _filled_from_config(
            payload
        ), "상품 입력에 importer 가 있는데 config 유래로 보고됨"

    def test_manufacturer_reported_when_from_config(self):
        """상품 입력에 manufacturer 없고 config 에만 있으면 → 보고."""
        p = _make_product()
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)제조사",
        }
        payload = _build_payload(p, cfg)
        assert "manufacturer" in _filled_from_config(
            payload
        ), "config 유래 manufacturer 가 보고에 없음"

    def test_manufacturer_not_reported_when_product_input(self):
        """상품 입력에 manufacturer 가 있으면 → 보고에 없다(입력 우선)."""
        p = _make_product({"manufacturer": "(주)명시제조사"})
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)제조사",
        }
        payload = _build_payload(p, cfg)
        assert "manufacturer" not in _filled_from_config(
            payload
        ), "상품 입력에 manufacturer 가 있는데 config 유래로 보고됨"

    def test_manufacturer_not_reported_when_seller_name(self):
        """seller_name_ko 등으로 제조자가 결정되면 → 보고에 없다(입력 우선)."""
        p = _make_product({"seller_name_ko": "판매자상사"})
        cfg = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "manufacturer": "(주)제조사",
        }
        payload = _build_payload(p, cfg)
        assert "manufacturer" not in _filled_from_config(
            payload
        ), "seller_name_ko 로 제조자가 결정되었는데 config 유래로 보고됨"


# =========================================================================== #
# 설정 유래 보고 — 보고명 확인: origin_content (countryOfOrigin 아님).
# =========================================================================== #


class TestReportingFieldName:
    """보고명은 내부 키 이름 그대로 (origin_content, countryOfOrigin 아님)."""

    def test_origin_content_uses_internal_key_name(self):
        """origin_content 보고명은 'origin_content' 이지 'countryOfOrigin' 이 아님."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
        }
        cfg = {"origin_area_code": "04", "origin_content": "베트남"}
        payload = _build_payload(p, cfg)
        filled = _filled_from_config(payload)
        assert "origin_content" in filled
        assert (
            "countryOfOrigin" not in filled
        ), "countryOfOrigin 보고명이 사용되면 안 됨 — 내부 키 origin_content 사용"


# =========================================================================== #
# 설정 유래 보고 — 통합: 모든 보고 필드가 동시에 config 유래일 때.
# =========================================================================== #


class TestAllN7FieldsReportedTogether:
    """origin_content·importer·manufacturer·delivery_fee 모두 config 유래일 때."""

    def test_all_n7_fields_reported(self):
        """4개 설정 유래 필드가 모두 config 유래이면 4개 모두 보고에 등장."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
            # made_in / origin_content / importer / manufacturer / delivery_fee 모두 없음
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "베트남",
            "importer": "(주)수입사",
            "manufacturer": "(주)제조사",
            "delivery_fee": 4000,
        }
        payload = _build_payload(p, cfg)
        filled = _filled_from_config(payload)
        for field in ("origin_content", "importer", "manufacturer", "delivery_fee"):
            assert field in filled, f"{field} 가 config 유래인데 보고에 없음: {filled!r}"

    def test_base_fee_uses_config_value(self):
        """모든 설정 유래 필드가 config 유래일 때 baseFee 도 config 값."""
        p = {
            "name": "테스트상품",
            "categoryId": "50000000",
            "salePrice": 30000,
            "origin_code": "04",
        }
        cfg = {
            "origin_area_code": "04",
            "origin_content": "베트남",
            "delivery_fee": 4000,
        }
        payload = _build_payload(p, cfg)
        assert _base_fee(payload) == 4000
