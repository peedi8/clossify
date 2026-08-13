# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
"""네이버 API 실패 본문 보존 테스트.

모든 HTTP 경로는 몽키패치한다. 이 파일의 시험은 실제 외부 API를 호출하지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client


class _FakeResponse:
    """실제 네트워크 없이 requests.Response 의 필요한 표면만 제공한다."""

    def __init__(
        self,
        status_code: int,
        *,
        json_body: object | None = None,
        text: str = "",
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = {"content-type": content_type}

    def json(self):
        if self._json_body is None:
            raise ValueError("not JSON")
        return self._json_body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} response", response=self)


def _patch_token_prerequisites(monkeypatch) -> None:
    """get_token 이 유효한 설정과 bcrypt 없이 요청 mock 까지 도달하게 한다."""
    monkeypatch.setattr(
        naver_client,
        "load_config",
        lambda: {
            "naver": {
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "type": "SELF",
            }
        },
    )
    monkeypatch.setattr(naver_client.bcrypt, "hashpw", lambda value, salt: b"test-signature")


def test_get_token_403_json_error_includes_status_code_and_message(monkeypatch):
    """403 JSON 오류는 상태·코드·메시지를 모두 보존한다."""
    _patch_token_prerequisites(monkeypatch)
    monkeypatch.setattr(
        naver_client.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            403,
            json_body={
                "code": "GW.IP_NOT_ALLOWED",
                "message": "호출이 허용되지 않은 IP입니다.",
            },
        ),
    )

    with pytest.raises(requests.HTTPError) as raised:
        naver_client.get_token()

    message = str(raised.value)
    assert "HTTP 403" in message
    assert "GW.IP_NOT_ALLOWED" in message
    assert "호출이 허용되지 않은 IP입니다." in message


def test_get_token_401_html_error_includes_body_preview(monkeypatch):
    """401 HTML 오류도 예외 없이 정화된 본문 앞부분을 남긴다."""
    _patch_token_prerequisites(monkeypatch)
    monkeypatch.setattr(
        naver_client.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            401,
            text="<html><body>credential rejected</body></html>",
            content_type="text/html",
        ),
    )

    with pytest.raises(requests.HTTPError) as raised:
        naver_client.get_token()

    message = str(raised.value)
    assert "HTTP 401" in message
    assert "credential rejected" in message


def test_get_token_error_never_leaks_client_secret(monkeypatch):
    """실패 본문 속 가짜 client_secret 값은 오류 메시지에 남지 않는다."""
    _patch_token_prerequisites(monkeypatch)
    fake_secret = "fake-client-secret-value"
    monkeypatch.setattr(
        naver_client.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(
            403,
            json_body={
                "code": "GW.AUTHN",
                "message": f"client_secret={fake_secret}",
            },
        ),
    )

    with pytest.raises(requests.HTTPError) as raised:
        naver_client.get_token()

    message = str(raised.value)
    assert fake_secret not in message
    assert "[REDACTED]" in message


def test_get_token_success_returns_the_existing_access_token_value(monkeypatch):
    """200 성공 경로는 기존처럼 액세스 토큰 문자열만 반환한다."""
    _patch_token_prerequisites(monkeypatch)
    monkeypatch.setattr(
        naver_client.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse(200, json_body={"access_token": "access-token"}),
    )

    assert naver_client.get_token() == "access-token"


def test_upload_400_includes_body_and_success_path_is_unchanged(tmp_path, monkeypatch):
    """이미지 400은 본문을 보존하고, 이어진 성공 호출은 URL 목록을 반환한다."""
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"png")
    responses = iter(
        [
            _FakeResponse(
                400,
                json_body={
                    "code": "GW.INVALID_IMAGE",
                    "message": "이미지 형식이 올바르지 않습니다.",
                },
            ),
            _FakeResponse(200, json_body={"images": [{"url": "https://cdn.example/image.png"}]}),
        ]
    )
    monkeypatch.setattr(naver_client, "_api_request", lambda *args, **kwargs: next(responses))

    with pytest.raises(requests.HTTPError) as raised:
        naver_client.upload_images([str(image_path)], tk="injected-token")

    message = str(raised.value)
    assert "HTTP 400" in message
    assert "GW.INVALID_IMAGE" in message
    assert "이미지 형식이 올바르지 않습니다." in message
    assert naver_client.upload_images([str(image_path)], tk="injected-token") == [
        "https://cdn.example/image.png"
    ]
