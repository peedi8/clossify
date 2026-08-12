# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""토큰 만료(GW.AUTHN) 재시도 검증 (N84).

네이버 문서 계약: API 응답이 **401** 이고 응답 본문의 ``code`` 가
``"GW.AUTHN"`` 이면 토큰이 만료되었을 가능성이 높다. 이 경우 토큰을
1회 재발급받아 요청을 1회 재시도하는 fallback 을 권장한다.

본 테스트는 네트워크 없이(monkeypatch) 6가지 시나리오를 검증한다:
  1. 401 + GW.AUTHN → 재발급 1회 + 재시도 1회, 두 번째 200 → 성공.
  2. 401 + GW.AUTHN 이 두 번 연속 → 재시도 총 1회만(발급 호출 1회로 증명).
  3. 401 인데 code 가 다른 값(GW.AUTHZ) → 재시도 안 함(발급 0회).
  4. 401 인데 본문이 JSON 이 아님(빈 문자열/HTML) → 재시도 안 함.
  5. 403·500 → 재시도 안 함.
  6. 재시도 경로 메시지에 토큰 문자열 없음.
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
# 테스트용 mock response 팩토리.
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """``requests.Response`` 의 최소한의 mock.

    ``status_code``, ``_json`` (dict 또는 None), ``_text`` 를 가져온다.
    ``headers`` 는 ``content-type`` 만 반환한다.
    """

    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self._text = text
        self.headers = {}

    def json(self):
        if self._json_body is not None:
            return self._json_body
        raise ValueError("No JSON")

    @property
    def text(self):
        return self._text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _make_request_mock(responses):
    """``requests.get`` (또는 다른 verb) 를 대체하는 mock 을 만든다.

    ``responses`` 는 호출 순서대로 반환할 ``_FakeResponse`` 리스트다.
    리스트가 끝나면 마지막 응답을 계속 반환한다(무한 호출 방지용 안전장치).

    ``_api_request`` 는 메서드별 ``requests.<verb>`` 로 위임하므로, 이 mock 은
    verb 함수의 호출 횟수를 센다(최초 + 재시도 모두 같은 verb 를 탄다).
    """
    call_log = {"count": 0}

    def _mock_request(url, **kwargs):
        idx = min(call_log["count"], len(responses) - 1)
        call_log["count"] += 1
        return responses[idx]

    return _mock_request, call_log


def _make_token_mock(tokens):
    """``get_token`` 을 대체하는 mock. ``tokens`` 리스트를 순서대로 반환."""
    call_log = {"count": 0}

    def _mock_get_token():
        idx = min(call_log["count"], len(tokens) - 1)
        call_log["count"] += 1
        return tokens[idx]

    return _mock_get_token, call_log


def _clear_retry_events():
    """``_AUTHN_RETRY_EVENTS`` 를 비운다(테스트 간 격리)."""
    naver_client._AUTHN_RETRY_EVENTS.clear()


# --------------------------------------------------------------------------- #
# 시나리오 1: 401 + GW.AUTHN → 재발급 1회 + 재시도 1회, 두 번째 200 → 성공.
# --------------------------------------------------------------------------- #


class TestRetryOnAuthNExpired:
    def test_401_gw_authn_then_200_succeeds(self):
        """401 + GW.AUTHN 첫 요청, 재시도 시 200 → 성공 반환."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN", "message": "expired"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["new-token-abc"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no", tk="expired-token-xyz")

        assert sc == 200, f"재시도 후 200 이어야 함: {sc}"
        assert body == {"ok": True}
        # 발급(get_token) 은 정확히 1회.
        assert tok_log["count"] == 1, f"발급 호출 횟수: {tok_log['count']} (예상 1)"
        # HTTP 요청은 정확히 2회 (최초 + 재시도).
        assert req_log["count"] == 2, f"HTTP 요청 횟수: {req_log['count']} (예상 2)"
        # 재시도 사실이 기록되었다.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 1


# --------------------------------------------------------------------------- #
# 시나리오 2: 401 + GW.AUTHN 두 번 연속 → 재시도 총 1회만 (발급 1회).
# --------------------------------------------------------------------------- #


class TestRetryOnlyOnceOnRepeatedAuthN:
    def test_401_gw_authn_twice_retries_only_once(self):
        """401 + GW.AUTHN 이 재시도에서도 반복 → 재시도 1회만, 실패 전파."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["new-token"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no", tk="old")

        # 두 번째도 401 → 실패 전파.
        assert sc == 401, f"두 번째 401 을 그대로 올려야 함: {sc}"
        # 발급(get_token) 은 정확히 1회 (재시도는 1회만).
        assert tok_log["count"] == 1, f"발급 호출 횟수: {tok_log['count']} (예상 1 — 재시도 1회)"
        # HTTP 요청은 정확히 2회 (최초 + 재시도 1회, 3회 아님).
        assert (
            req_log["count"] == 2
        ), f"HTTP 요청 횟수: {req_log['count']} (예상 2 — 무한재시도 아님)"


# --------------------------------------------------------------------------- #
# 시나리오 3: 401 인데 code 가 다른 값(GW.AUTHZ) → 재시도 안 함.
# --------------------------------------------------------------------------- #


class TestNoRetryOnDifferentAuthCode:
    def test_401_gw_authz_no_retry(self):
        """401 이지만 code 가 GW.AUTHZ → 재시도 안 함 (발급 0회)."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHZ", "message": "forbidden"}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no", tk="some-token")

        assert sc == 401
        # 발급 0회.
        assert tok_log["count"] == 0, f"발급 호출 횟수: {tok_log['count']} (예상 0)"
        # HTTP 요청 1회 (재시도 없음).
        assert req_log["count"] == 1, f"HTTP 요청 횟수: {req_log['count']} (예상 1)"
        # 재시도 이벤트 없음.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 0


# --------------------------------------------------------------------------- #
# 시나리오 4: 401 인데 본문이 JSON 이 아님 → 재시도 안 함.
# --------------------------------------------------------------------------- #


class TestNoRetryOnNonJsonBody:
    def test_401_empty_string_body_no_retry(self):
        """401 인데 본문이 빈 문자열 → 재시도 안 함 (예외 없이)."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, text=""),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no", tk="some-token")

        assert sc == 401
        assert tok_log["count"] == 0, f"발급 호출 횟수: {tok_log['count']} (예상 0)"
        assert req_log["count"] == 1, f"HTTP 요청 횟수: {req_log['count']} (예상 1)"

    def test_401_html_body_no_retry(self):
        """401 인데 본문이 HTML → 재시도 안 함 (예외 없이)."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, text="<html><body>502 Gateway Error</body></html>"),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no", tk="some-token")

        assert sc == 401
        assert tok_log["count"] == 0, f"발급 호출 횟수: {tok_log['count']} (예상 0)"


# --------------------------------------------------------------------------- #
# 시나리오 5: 403·500 → 재시도 안 함.
# --------------------------------------------------------------------------- #


class TestNoRetryOn403And500:
    def test_403_no_retry(self):
        """403 → 재시도 안 함."""
        _clear_retry_events()
        responses = [_FakeResponse(403, json_body={"code": "FORBIDDEN"})]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("x", tk="t")

        assert sc == 403
        assert tok_log["count"] == 0
        assert req_log["count"] == 1

    def test_500_no_retry(self):
        """500 → 재시도 안 함."""
        _clear_retry_events()
        responses = [_FakeResponse(500, json_body={"code": "INTERNAL"})]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("x", tk="t")

        assert sc == 500
        assert tok_log["count"] == 0
        assert req_log["count"] == 1


# --------------------------------------------------------------------------- #
# 시나리오 6: 재시도 경로 메시지에 토큰 문자열 없음.
# --------------------------------------------------------------------------- #


class TestNoTokenLeakInRetryEvents:
    def test_retry_event_has_no_token_string(self):
        """재시도 이벤트 기록에 토큰 문자열이 없어야 한다."""
        _clear_retry_events()
        secret_token = "SECRET-TOKEN-DO-NOT-LEAK-12345"
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["new-" + secret_token])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                naver_client.get_product("x", tk=secret_token)

        # 이벤트가 기록되었다.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 1
        event = naver_client._AUTHN_RETRY_EVENTS[0]
        # 직렬화해도 토큰 문자열이 없어야 한다.
        import json

        serialized = json.dumps(event, ensure_ascii=False)
        assert secret_token not in serialized, f"재시도 이벤트에 토큰 문자열이 누출됨: {serialized}"
        assert "new-" + secret_token not in serialized


# --------------------------------------------------------------------------- #
# 시나리오 7: 파일 업로드 재시도 — 두 번째 요청의 파일 내용이 1차와 바이트 동일.
# (회귀: 재시도가 이미지를 0바이트로 올리는 결함 — 머지 차단급)
# --------------------------------------------------------------------------- #


class TestFileUploadRetryPreservesContent:
    """``upload_images`` 경로의 401+GW.AUTHN 재시도가 파일을 0바이트로 올리는
    결함(T3, 머지 차단급) 의 회귀 시험.

    ``requests`` 는 멀티파트 바디를 만들 때 각 파일 객체를 ``read()`` 한다.
    첫 요청이 끝나면 핸들은 EOF 다. 재시도 직전에 ``seek(0)`` 으로 되감지
    않으면 두 번째 요청은 **빈 바이트** 를 올린다.
    """

    def test_retry_rewinds_file_streams_content_identical(self, tmp_path):
        """실제 임시 파일 2개 → 1차 401+GW.AUTHN → 재시도.

        두 번째 요청이 받은 파일 내용이 1차와 **바이트 단위로 동일** 한지 확인.
        길이만 보지 말고 내용을 대조한다.
        """
        _clear_retry_events()
        # 실제 임시 파일 2개 생성 — 서로 다른 내용.
        content_a = b"\x89PNG\r\n\x1a\n" + b"alpha-image-payload" * 50
        content_b = b"\x89PNG\r\n\x1a\n" + b"beta-image-payload--" * 50
        fa = tmp_path / "a.png"
        fb = tmp_path / "b.png"
        fa.write_bytes(content_a)
        fb.write_bytes(content_b)

        # 매 요청마다 files 안의 파일 핸들을 read() 하여 캡처.
        uploaded_bodies: list[dict[str, bytes]] = []

        def _capture_and_respond(url, **kwargs):
            files = kwargs.get("files", [])
            captured = {}
            for entry in files:
                file_tuple = entry[1]
                fname = file_tuple[0]
                fobj = file_tuple[1]
                data = fobj.read()  # requests 가 multipart 바디를 만드는 방식
                captured[fname] = data
            uploaded_bodies.append(captured)
            # 1차 호출(인덱스 0) 은 401, 이후는 200.
            if len(uploaded_bodies) == 1:
                return _FakeResponse(401, json_body={"code": "GW.AUTHN"})
            return _FakeResponse(
                200,
                json_body={"images": [{"url": "https://x/a.png"}, {"url": "https://x/b.png"}]},
            )

        tok_mock, tok_log = _make_token_mock(["new-token"])

        with mock.patch.object(naver_client.requests, "post", side_effect=_capture_and_respond):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                urls = naver_client.upload_images([str(fa), str(fb)], tk="expired")

        # 재시도가 일어났다 (요청 2회).
        assert len(uploaded_bodies) == 2, f"요청 횟수: {len(uploaded_bodies)} (예상 2)"
        # 토큰 재발급 1회.
        assert tok_log["count"] == 1
        # 핵심: 두 번째 요청의 파일 내용이 1차와 바이트 동일.
        first = uploaded_bodies[0]
        second = uploaded_bodies[1]
        assert set(first.keys()) == set(second.keys()), "파일명 집합이 다름"
        for fname in first:
            assert first[fname] == second[fname], (
                f"재시도 파일 내용 불일치: {fname} — "
                f"1차 {len(first[fname])}바이트, 2차 {len(second[fname])}바이트 "
                f"(0바이트 이미지 회귀)"
            )
        # 내용이 실제로 비어있지 않음 (길이 0이면 seek 후에도 빈 것).
        for fname in first:
            assert len(first[fname]) > 0, f"1차 파일이 비어있음: {fname}"
            assert len(second[fname]) > 0, f"2차 파일이 비어있음: {fname}"
        # 반환값 확인.
        assert urls == ["https://x/a.png", "https://x/b.png"]


# --------------------------------------------------------------------------- #
# 시나리오 8: 되감기 불가 스트림 → 재시도 포기, 원 401 응답 그대로 반환.
# --------------------------------------------------------------------------- #


class TestNonSeekableStreamNoRetry:
    """되감을 수 없는 스트림이 섞이면 재시도하지 않고 원 응답을 반환한다.

    빈 본문으로 재시도하는 것(0바이트)보다 낫기 때문이다.
    """

    def test_non_seekable_stream_no_retry_returns_original_401(self):
        _clear_retry_events()

        class _UnseekableStream:
            """``seek`` 를 시도하면 ``OSError`` 를 일으키는 가짜 스트림."""

            def __init__(self, data):
                self._data = data
                self._pos = 0

            def read(self, size=-1):
                if size < 0:
                    chunk = self._data[self._pos :]
                else:
                    chunk = self._data[self._pos : self._pos + size]
                self._pos += len(chunk)
                return chunk

            def seek(self, offset, whence=0):
                raise OSError("unsupport seek operation")

        stream = _UnseekableStream(b"payload-data")
        files = [("imageFiles", ("x.png", stream, "image/png"))]
        call_count = {"n": 0}

        def _mock_post(url, **kwargs):
            call_count["n"] += 1
            return _FakeResponse(401, json_body={"code": "GW.AUTHN"})

        tok_mock, tok_log = _make_token_mock(["new-token"])

        with mock.patch.object(naver_client.requests, "post", side_effect=_mock_post):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                r = naver_client._api_request(
                    "POST",
                    "https://api.example.com/upload",
                    tk="expired",
                    header_builder=lambda t: {"Authorization": f"Bearer {t}"},
                    files=files,
                    timeout=30,
                )

        # 재시도 안 함 — 요청 1회만.
        assert call_count["n"] == 1, f"요청 횟수: {call_count['n']} (예상 1 — 재시도 포기)"
        # 토큰 재발급 안 함.
        assert tok_log["count"] == 0
        # 원 401 응답이 그대로 반환됨.
        assert r.status_code == 401
        # 재시도 이벤트 없음.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 0
