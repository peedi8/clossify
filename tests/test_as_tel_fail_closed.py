"""A/S 연락처를 build_payload 경계에서 fail-closed로 검증한다."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client


def _product(**overrides: str) -> dict:
    return {
        "name": "A/S 테스트상품",
        "categoryId": "50002366",
        "salePrice": 10000,
        **overrides,
    }


def _build_payload(product: dict, notice_config: dict) -> dict:
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=notice_config),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(product, "<html></html>", ["http://x.png"])


_ORIGIN = {"origin_area_code": "04", "origin_content": "한국"}


def test_missing_as_tel_rejected_with_canonical_config_path():
    """AS 연락처가 상품·정본 설정 모두에 없으면 빈 값 전송 전에 거부한다."""
    with pytest.raises(
        ValueError,
        match=r"smartstore_notice_defaults\.as_tel",
    ):
        _build_payload(_product(), _ORIGIN)


def test_placeholder_as_tel_rejected():
    """기존 _first_value 자리표시자 판정을 재사용하여 AS도 거부한다."""
    with pytest.raises(
        ValueError,
        match=r"smartstore_notice_defaults\.as_tel",
    ):
        _build_payload(_product(), {**_ORIGIN, "as_tel": "REPLACE_WITH_REAL_AS_TEL"})


def test_configured_as_tel_and_empty_guide_are_preserved():
    """정상 정본 번호는 그대로 보내고 안내 문구의 빈값 규율은 바꾸지 않는다."""
    payload = _build_payload(_product(), {**_ORIGIN, "as_tel": "070-1234-5678"})
    after_service = payload["originProduct"]["detailAttribute"]["afterServiceInfo"]

    assert after_service == {
        "afterServiceTelephoneNumber": "070-1234-5678",
        "afterServiceGuideContent": "",
    }


def test_product_as_tel_takes_precedence_over_config():
    """상품 입력의 실번호가 정본 설정값보다 먼저 선택된다."""
    payload = _build_payload(
        _product(as_tel="02-9876-5432"),
        {**_ORIGIN, "as_tel": "070-1234-5678"},
    )

    assert (
        payload["originProduct"]["detailAttribute"]["afterServiceInfo"][
            "afterServiceTelephoneNumber"
        ]
        == "02-9876-5432"
    )
