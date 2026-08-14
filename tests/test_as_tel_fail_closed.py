"""A/S 연락처를 build_payload 경계에서 fail-closed로 검증한다."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client, register


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

_COMMON_NOTICE_FIELDS = {
    "returnCostReason",
    "noRefundReason",
    "qualityAssuranceStandard",
    "compensationProcedure",
    "troubleShootingContents",
}

_WEAR_NOTICE_WITHOUT_COMMON = {
    "productInfoProvidedNoticeType": "WEAR",
    "etc": {
        "material": "면 100%",
        "color": "검정",
        "size": "FREE",
        "caution": "세탁 전 라벨을 확인하세요.",
        "packDateText": "2026-08-14",
        "warrantyPolicy": "소비자분쟁해결기준에 따릅니다.",
    },
}


def _fake_attach_ok(sources):
    return {
        "urls": [f"http://cdn.example.test/{index}.png" for index, _source in enumerate(sources)],
        "rejected": [],
        "notes": [],
    }


def _prepare_response_with_notice_config(
    product: dict, notice_config: dict, tmp_path: Path, monkeypatch
) -> dict:
    """실제 준비 경로를 attach/tag 외부 의존성 없이 호출한다."""
    prepared_dir = tmp_path / "prepared"
    prepared_dir.mkdir()
    monkeypatch.setattr(common, "PREPARED_DIR", prepared_dir)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"smartstore_notice_defaults": notice_config}, ensure_ascii=False),
        encoding="utf-8",
    )

    original_prepare = register.prepare_listing

    def prepare_with_local_dependencies(input_product):
        return original_prepare(
            input_product,
            attach_fn=_fake_attach_ok,
            recommend_fn=lambda _name: (200, []),
            restricted_fn=lambda _tags: (200, []),
        )

    with (
        mock.patch.object(naver_client, "_notice_config", return_value=notice_config),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(naver_client, "config_path", return_value=str(config_path)),
        mock.patch.object(
            mcp_server._register_mod,
            "prepare_listing",
            side_effect=prepare_with_local_dependencies,
        ),
    ):
        return mcp_server.prepare_listing(product)


def _config_diagnostics(tmp_path: Path, notice_config: dict) -> tuple[dict, dict]:
    """동일 config에서 공개 점검과 컴플라이언스 플래그를 함께 읽는다."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "naver": {
                    "client_id": "test-id",
                    "client_secret": "test-secret",
                    "type": "SELF",
                    "store_url_slug": "test-store",
                },
                "smartstore_notice_defaults": notice_config,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with (
        mock.patch.object(naver_client, "config_path", return_value=str(config_path)),
        mock.patch.object(naver_client, "resolve_config_path", return_value=str(config_path)),
    ):
        return mcp_server.check_config(), mcp_server._build_config_flags()


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


@pytest.mark.parametrize(
    ("config_key", "phone"),
    [
        ("as_tel", "070-1111-1111"),
        ("seller_tel", "070-2222-2222"),
        ("customerServicePhoneNumber", "070-3333-3333"),
    ],
)
def test_each_config_as_tel_key_matches_diagnostics_and_payload(tmp_path, config_key, phone):
    """세 설정 키 각각은 공개 점검·컴플라이언스·조립기에서 모두 유효하다."""
    notice_config = {**_ORIGIN, config_key: phone}

    check_result, flags = _config_diagnostics(tmp_path, notice_config)
    payload = _build_payload(_product(), notice_config)

    assert check_result["as_tel_configured"] is True
    assert flags["as_configured"] is True
    assert (
        payload["originProduct"]["detailAttribute"]["afterServiceInfo"][
            "afterServiceTelephoneNumber"
        ]
        == phone
    )


def test_all_config_as_tel_keys_missing_are_rejected_with_all_key_names(tmp_path):
    """세 설정 키가 모두 없으면 진단과 조립기가 함께 fail-closed 한다."""
    check_result, flags = _config_diagnostics(tmp_path, _ORIGIN)

    assert check_result["as_tel_configured"] is False
    assert flags["as_configured"] is False
    with pytest.raises(ValueError) as exc_info:
        _build_payload(_product(), _ORIGIN)
    message = str(exc_info.value)
    for config_key in ("as_tel", "seller_tel", "customerServicePhoneNumber"):
        assert config_key in message


def test_all_config_as_tel_placeholders_are_rejected(tmp_path):
    """세 설정 키의 자리표시자는 기존 _first_value 규칙으로 모두 미설정이다."""
    notice_config = {
        **_ORIGIN,
        "as_tel": "REPLACE_WITH_AS_TEL",
        "seller_tel": "REPLACE_WITH_SELLER_TEL",
        "customerServicePhoneNumber": "REPLACE_WITH_CUSTOMER_SERVICE_PHONE",
    }

    check_result, flags = _config_diagnostics(tmp_path, notice_config)

    assert check_result["as_tel_configured"] is False
    assert flags["as_configured"] is False
    with pytest.raises(ValueError):
        _build_payload(_product(), notice_config)


@pytest.mark.parametrize(
    ("product_key", "config_key"),
    [
        ("as_tel", "seller_tel"),
        ("seller_tel", "customerServicePhoneNumber"),
    ],
)
def test_product_as_tel_candidates_take_precedence_over_all_config_keys(product_key, config_key):
    """기존 상품 입력 후보 둘은 어떤 설정 키보다도 우선한다."""
    payload = _build_payload(
        _product(**{product_key: "02-9876-5432"}),
        {**_ORIGIN, config_key: "070-1234-5678"},
    )

    assert (
        payload["originProduct"]["detailAttribute"]["afterServiceInfo"][
            "afterServiceTelephoneNumber"
        ]
        == "02-9876-5432"
    )
