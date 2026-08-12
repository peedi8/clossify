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
        """401 + GW.AUTHN 첫 요청, 재시도 시 200 → 성공 반환.

        WO PR #27 8라운드 ②: ``get_product`` 같은 래퍼에 ``tk=<문자열>`` 을
        넘기면 **주입 토큰** 으로 간주되어 자동 갱신이 꺼진다. 내부 발급 토큰의
        재시도를 시험하려면 ``tk=None`` 으로 두고 ``get_token`` 모크가 두 번
        (최초 발급 + 재발급) 불려야 한다.
        """
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN", "message": "expired"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["initial-token", "new-token-abc"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no")

        assert sc == 200, f"재시도 후 200 이어야 함: {sc}"
        assert body == {"ok": True}
        # 발급(get_token) 은 정확히 2회 (최초 발급 + 재발급).
        assert tok_log["count"] == 2, f"발급 호출 횟수: {tok_log['count']} (예상 2 — 최초+재발급)"
        # HTTP 요청은 정확히 2회 (최초 + 재시도).
        assert req_log["count"] == 2, f"HTTP 요청 횟수: {req_log['count']} (예상 2)"
        # 재시도 사실이 기록되었다.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 1


# --------------------------------------------------------------------------- #
# 시나리오 2: 401 + GW.AUTHN 두 번 연속 → 재시도 총 1회만 (발급 1회).
# --------------------------------------------------------------------------- #


class TestRetryOnlyOnceOnRepeatedAuthN:
    def test_401_gw_authn_twice_retries_only_once(self):
        """401 + GW.AUTHN 이 재시도에서도 반복 → 재시도 1회만, 실패 전파.

        WO PR #27 8라운드 ②: 내부 발급 토큰 경로(``tk=None``) 로 시험.
        ``get_token`` 은 최초 발급 1회 + 재발급 1회 = 2회 불린다.
        """
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["initial-token", "new-token"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("test-origin-no")

        # 두 번째도 401 → 실패 전파.
        assert sc == 401, f"두 번째 401 을 그대로 올려야 함: {sc}"
        # 발급(get_token) 은 정확히 2회 (최초 발급 + 재발급 1회).
        assert (
            tok_log["count"] == 2
        ), f"발급 호출 횟수: {tok_log['count']} (예상 2 — 최초+재발급 1회)"
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
        """재시도 이벤트 기록에 토큰 문자열이 없어야 한다.

        WO PR #27 8라운드 ②: 내부 발급 토큰 경로(``tk=None``) 로 시험 —
        ``get_token`` 모크가 비밀 토큰을 반환한다. 주입 토큰 경로면 RuntimeError
        가 나므로 재시도 이벤트 자체가 생기지 않는다.
        """
        _clear_retry_events()
        secret_token = "SECRET-TOKEN-DO-NOT-LEAK-12345"
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock([secret_token, "new-" + secret_token])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                naver_client.get_product("x")

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

        tok_mock, tok_log = _make_token_mock(["initial-token", "new-token"])

        with mock.patch.object(naver_client.requests, "post", side_effect=_capture_and_respond):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                urls = naver_client.upload_images([str(fa), str(fb)])

        # 재시도가 일어났다 (요청 2회).
        assert len(uploaded_bodies) == 2, f"요청 횟수: {len(uploaded_bodies)} (예상 2)"
        # 토큰 재발급: 최초 발급 1회 + 재발급 1회 = 2회.
        assert (
            tok_log["count"] == 2
        ), f"get_token 호출 횟수: {tok_log['count']} (예상 2 — 최초+재발급)"
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
                    allow_retry=True,
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


# --------------------------------------------------------------------------- #
# 시나리오 9: dict형 ``files`` 도 되감는다 (회귀: 머지 차단급).
#
# ``requests`` 는 ``files`` 를 두 표준 형태로 받는다:
#   - 리스트형 ``[(field_name, (filename, fobj, mime)), ...]``  ← 기존 시험
#   - dict형 ``{field_name: (filename, fobj, mime)}``           ← 본 시험
# 이전 구현은 dict형 ``.values()`` 가 내놓는 ``(filename, fobj, mime)`` 튜플을
# ``(field_name, file_tuple)`` 로 착각해 ``file_spec = entry[1]`` (= fobj) 을
# 취했다. 뒤따르는 튜플/리스트 검사가 fobj 를 건너뛰어 ``True`` 를 반환 →
# 되감기가 일어나지 않은 채 재시도가 0바이트 본문을 보냈다.
# --------------------------------------------------------------------------- #


class TestDictFormFilesRewind:
    """dict형 ``files`` 에서도 401+GW.AUTHN 재시도 시 파일 스트림이
    ``seek(0)`` 으로 되감아지는지 검증한다.

    1차 요청과 2차(재시도) 요청이 **동일한 바이트** 를 받아야 한다.
    리스트형 기존 시험(시나리오 7)과 동일한 내용의 대조군이지만 ``files``
    를 dict형으로 넣는다.
    """

    def test_dict_form_files_rewound_content_identical(self):
        _clear_retry_events()
        content_a = b"\x89PNG\r\n\x1a\n" + b"alpha-image-payload" * 50
        content_b = b"\x89PNG\r\n\x1a\n" + b"beta-image-payload--" * 50

        import io

        s_a = io.BytesIO(content_a)
        s_b = io.BytesIO(content_b)
        files = {
            "image": ("a.png", s_a, "image/png"),
            "logo": ("b.png", s_b, "image/png"),
        }

        uploaded_bodies: list[dict[str, bytes]] = []

        def _capture_and_respond(url, **kwargs):
            files_kw = kwargs.get("files", {})
            captured = {}
            # requests 가 multipart 바디를 만드는 방식 — file_tuple.read().
            # dict형: values() 가 (filename, fobj, mime) 튜플을 내놓는다.
            for key, file_tuple in files_kw.items():
                fname = file_tuple[0]
                fobj = file_tuple[1]
                captured[f"{key}/{fname}"] = fobj.read()
            uploaded_bodies.append(captured)
            if len(uploaded_bodies) == 1:
                return _FakeResponse(401, json_body={"code": "GW.AUTHN"})
            return _FakeResponse(
                200,
                json_body={"images": [{"url": "https://x/a.png"}, {"url": "https://x/b.png"}]},
            )

        tok_mock, tok_log = _make_token_mock(["new-token"])

        with mock.patch.object(naver_client.requests, "post", side_effect=_capture_and_respond):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                r = naver_client._api_request(
                    "POST",
                    "https://api.example.com/upload",
                    tk="expired",
                    header_builder=lambda t: {"Authorization": f"Bearer {t}"},
                    allow_retry=True,
                    files=files,
                    timeout=30,
                )

        # 재시도가 일어났다 (요청 2회).
        assert (
            len(uploaded_bodies) == 2
        ), f"요청 횟수: {len(uploaded_bodies)} (예상 2 — dict형도 되감아야 함)"
        # 토큰 재발급 1회.
        assert tok_log["count"] == 1
        # 핵심: 2차 요청의 파일 내용이 1차와 바이트 동일 (되감기 확인).
        first = uploaded_bodies[0]
        second = uploaded_bodies[1]
        assert set(first.keys()) == set(
            second.keys()
        ), f"파일 키 집합 불일치: {set(first)} vs {set(second)}"
        for k in first:
            assert first[k] == second[k], (
                f"재시도 파일 내용 불일치: {k} — "
                f"1차 {len(first[k])}바이트, 2차 {len(second[k])}바이트 "
                f"(dict형 되감기 결함)"
            )
            assert len(first[k]) > 0, f"1차 파일이 비어있음: {k}"
            assert len(second[k]) > 0, f"2차 파일이 비어있음: {k} (0바이트 재시도 회귀)"
        # 최종 응답 200.
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# 시나리오 10: 상품 등록 POST 는 401+GW.AUTHN 에서 재시도하지 않는다.
#
# ``_post_product_payload`` 가 타는 POST /external/v2/products 는 상품 신규 생성이다.
# 게이트웨이가 인증 단계에서 잘랐다면 원 요청은 도달하지 않았겠지만, 우리는
# 그것을 증명할 수 없다. 증명 못 하는 전제 위에서 중복 상품이 라이브 마켓에
# 올라가는 위험을 감수할 이유가 없다 — POST 생성 경로는 재시도하지 않는다.
# --------------------------------------------------------------------------- #


class TestPostProductPayloadNoRetryOnAuthN:
    """상품 신규 등록 POST(``_post_product_payload``)는 401+GW.AUTHN 에서
    재시도하지 않는다.

    WO PR #27 5라운드 ①: ``_api_request`` 의 ``allow_retry`` 기본값이
    ``False`` 이고, ``_post_product_payload`` 는 이를 명시적으로 ``False``
    로 넘긴다. 401+GW.AUTHN 을 받으면 재시도 없이 원 응답을 올린다.
    호출 카운터로 이를 증명한다: HTTP 요청 1회, ``get_token`` 0회.
    """

    def test_post_product_payload_does_not_retry_on_401_gw_authn(self):
        """POST /external/v2/products 가 401 GW.AUTHN → 재시도 없음 (요청 1회)."""
        _clear_retry_events()
        call_count = {"n": 0}

        def _mock_post(url, **kwargs):
            call_count["n"] += 1
            return _FakeResponse(401, json_body={"code": "GW.AUTHN"})

        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        with mock.patch.object(naver_client.requests, "post", side_effect=_mock_post):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client._post_product_payload(
                    {"originProduct": {}}, tk="expired-token"
                )

        # HTTP 요청은 정확히 1회 (재시도 없음).
        assert (
            call_count["n"] == 1
        ), f"POST /products 요청 횟수: {call_count['n']} (예상 1 — 생성 POST 는 재시도 안 함)"
        # 토큰 재발급 0회.
        assert (
            tok_log["count"] == 0
        ), f"get_token 호출 횟수: {tok_log['count']} (예상 0 — 생성 POST 는 재시도 안 함)"
        # 401 을 그대로 올린다.
        assert sc == 401
        # 재시도 이벤트 없음.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 0


# --------------------------------------------------------------------------- #
# 시나리오 10b: 나머지 경로는 여전히 401+GW.AUTHN 에서 1회 재시도한다.
# --------------------------------------------------------------------------- #


class TestOtherPathsStillRetryOnAuthN:
    """조회·이미지 업로드·수정·삭제 경로는 ``allow_retry=True`` 로
    401+GW.AUTHN 시 1회 재시도한다.

    WO PR #27 5라운드 ①: 생성 POST 만 재시도에서 빼고, 나머지 안전 경로는
    현행 유지. ``get_product`` 로 대표 케이스를 증명한다.
    """

    def test_get_product_still_retries_once_on_401_gw_authn(self):
        """GET /origin-products/{no} 가 401 GW.AUTHN → 1회 재시도 (요청 2회).

        WO PR #27 8라운드 ②: 내부 발급 토큰 경로(``tk=None``) 로 시험.
        ``get_token`` 은 최초 발급 + 재발급 = 2회 불린다.
        """
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["initial-token", "new-token"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("x")

        assert sc == 200
        assert (
            tok_log["count"] == 2
        ), f"get_token 호출 횟수: {tok_log['count']} (예상 2 — 최초+재발급)"
        assert (
            req_log["count"] == 2
        ), f"GET 요청 횟수: {req_log['count']} (예상 2 — 조회 경로는 여전히 1회 재시도)"
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 1


# --------------------------------------------------------------------------- #
# 시나리오 11: ``_AUTHN_RETRY_EVENTS`` 버퍼 상한 — ``collections.deque(maxlen=...)``.
# (WO PR #27 3라운드 감리 — 무한 자라는 버퍼)
#
# 두 감리가 같이 지적: ``_AUTHN_RETRY_EVENTS`` 는 모듈 전역 리스트인데
# 운영 코드에서 비우거나 상한을 두는 곳이 없었다. ``mcp_server`` 처럼
# 오래 떠 있는 프로세스에서 토큰 만료가 반복되면 계속 자란다.
# --------------------------------------------------------------------------- #


class TestRetryEventsBufferIsBounded:
    """``_AUTHN_RETRY_EVENTS`` 가 상한 있는 ``deque`` 로 무한히 자라지 않는가.

    WO 3라운드 감리: ``mcp_server`` 같은 장기 프로세스에서 토큰 만료가
    반복되면 ``_AUTHN_RETRY_EVENTS`` 가 무한히 자라는 결함. 상한 있는
    ``collections.deque(maxlen=...)`` 로 바꿔 가장 오래된 이벤트부터
    밀려나게 한다. 기존 소비처(``len()``/``[i]``/``.clear()``) 가 깨지지 않는지도
    함께 검증한다.
    """

    def test_buffer_is_deque_with_maxlen(self):
        """``_AUTHN_RETRY_EVENTS`` 는 ``deque`` 이고 ``maxlen`` 이 양수다."""
        import collections

        buf = naver_client._AUTHN_RETRY_EVENTS
        assert isinstance(
            buf, collections.deque
        ), f"_AUTHN_RETRY_EVENTS 타입: {type(buf).__name__} (예상 deque)"
        assert buf.maxlen is not None, "maxlen 이 None (무한) 이면 안 됨"
        assert buf.maxlen > 0, f"maxlen: {buf.maxlen} (양수여야 함)"

    def test_buffer_drops_oldest_when_full(self):
        """maxlen 초과 시 가장 오래된 이벤트가 밀려난다.

        회귀: 구현이 list 였을 때는 무한히 자랐다.
        """
        _clear_retry_events()
        maxlen = naver_client._AUTHN_RETRY_EVENTS_MAXLEN
        # maxlen 보다 많은 이벤트를 직접 append 한다(모듈 전역 deque).
        for i in range(maxlen + 50):
            naver_client._AUTHN_RETRY_EVENTS.append(
                {"url": f"https://x/{i}", "method": "GET", "retried": True}
            )
        # 길이는 maxlen 이하여야 한다.
        assert (
            len(naver_client._AUTHN_RETRY_EVENTS) == maxlen
        ), f"길이: {len(naver_client._AUTHN_RETRY_EVENTS)} (예상 {maxlen})"
        # 가장 오래된 이벤트는 밀려났다.
        first_kept = naver_client._AUTHN_RETRY_EVENTS[0]
        assert (
            first_kept["url"] == "https://x/50"
        ), f"가장 오래된 이벤트가 밀려나지 않음: {first_kept}"

    def test_buffer_list_interfaces_preserved(self):
        """``len()``·``[i]``·``.clear()`` 등 list 호환 인터페이스가 동작한다.

        기존 소비처(시험) 가 deque 전환 후에도 깨지지 않아야 한다.
        """
        _clear_retry_events()
        naver_client._AUTHN_RETRY_EVENTS.append({"url": "https://x/1", "retried": True})
        naver_client._AUTHN_RETRY_EVENTS.append({"url": "https://x/2", "retried": True})
        # len() 호환.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 2
        # 인덱스 접근 호환.
        assert naver_client._AUTHN_RETRY_EVENTS[0]["url"] == "https://x/1"
        assert naver_client._AUTHN_RETRY_EVENTS[-1]["url"] == "https://x/2"
        # 순회 호환.
        urls = [e["url"] for e in naver_client._AUTHN_RETRY_EVENTS]
        assert urls == ["https://x/1", "https://x/2"]
        # clear() 호환.
        naver_client._AUTHN_RETRY_EVENTS.clear()
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 0


# --------------------------------------------------------------------------- #
# 시나리오 12: ``_api_request`` docstring 의 재시도 안전 전제 출처 명시.
# (WO PR #27 3라운드 감리 — 근거 없는 전제로 읽히지 않게)
# --------------------------------------------------------------------------- #


class TestApiRequestProvenanceComment:
    """``_api_request`` 가 재시도 안전 전제의 근거를 docstring 에 명시하는가.

    WO PR #27 5라운드 ②: 되돌리기 어려운 동작(``register_product`` 등) 도 이 함수를
    경유한다. 안전하려면 *"401 GW.AUTHN 이면 원 요청은 서버에서 처리되지
    않았다"* 는 전제가 성립해야 하는데, 이 전제의 근거를 문서 원문 인용으로
    남긴다. 파일명만 적지 않고, 공개 문서의 원문 문구를 직접 인용하며,
    그 문서가 보장하지 않는 것(원 요청 미처리 보장 없음)도 함께 적는다.
    """

    def test_docstring_quotes_public_auth_document(self):
        """docstring 이 공개 문서 원문과 GW.AUTHN 을 함께 인용한다.

        WO PR #27 5라운드 ②: 비공개 경로(``docs_auth.txt``)를 가리키지 않고,
        네이버 커머스API 인증 문서(공개 문서)의 원문 문구를 직접 인용한다.
        """
        import inspect

        doc = inspect.getdoc(naver_client._api_request) or ""
        assert "GW.AUTHN" in doc, "GW.AUTHN 이 docstring 에 없음"
        # 공개 문서 원문 인용 — "재발급받는 fallback 처리를 권장합니다" 문구.
        assert "fallback" in doc, "인증 문서 원문(fallback 권장)이 인용되지 않음"
        # 문서가 보장하지 않는 것도 함께 명시되어야 한다.
        assert (
            "보장하지 않는다" in doc
        ), "인증 문서가 원 요청 미처리를 보장하지 않는다는 범위 명시가 없음"


# --------------------------------------------------------------------------- #
# 시나리오 13: 주입된 토큰은 자동 갱신하지 않는다 (WO PR #27 8라운드 ②).
#
# 호출자가 ``tk=<문자열>`` 로 외부 자격증명에서 받은 토큰을 넘겼는데 만료(401+
# GW.AUTHN) 되면, ``get_token()`` (``load_config()`` 기반) 으로 갱신하면 **다른
# 판매자 신원**으로 요청이 나간다. 따라서 주입 토큰은 사유 있는 RuntimeError
# 를 올리고 재시도하지 않는다. 내부 발급 토큰(``tk=None``) 만 갱신 대상이다.
# --------------------------------------------------------------------------- #


class TestInjectedTokenNoAutoRefresh:
    """``tk=<문자열>`` 로 주입된 토큰은 401+GW.AUTHN 시 자동 갱신하지 않는다.

    증명 (요청 횟수로):
      - 주입 토큰 경로(``get_product("x", tk="external")``): HTTP 1회,
        ``get_token`` 0회, RuntimeError 사유 포함.
      - 내부 발급 경로(``get_product("x")``): HTTP 2회, ``get_token`` 2회.
    """

    def test_injected_token_no_retry_raises_with_reason(self):
        """주입 토큰 → HTTP 1회, get_token 0회, RuntimeError (사유 포함)."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN", "message": "expired"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["should-not-be-called"])

        import pytest

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                with pytest.raises(RuntimeError) as exc_info:
                    naver_client.get_product("x", tk="external-credential-token")

        # HTTP 요청은 1회 (재시도 없음).
        assert (
            req_log["count"] == 1
        ), f"주입 토큰 경로 HTTP 요청: {req_log['count']}회 (예상 1 — 재시도 금지)"
        # get_token 호출 0회 (갱신 안 함).
        assert (
            tok_log["count"] == 0
        ), f"주입 토큰 경로 get_token 호출: {tok_log['count']}회 (예상 0 — 자동 갱신 금지)"
        # 사유가 메시지에 있다.
        msg = str(exc_info.value)
        assert "주입된 토큰" in msg, f"RuntimeError 사유에 '주입된 토큰' 없음: {msg}"
        assert (
            "자동 재발급하지 않는다" in msg
        ), f"RuntimeError 사유에 '자동 재발급하지 않는다' 없음: {msg}"
        # 재시도 이벤트 없음.
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 0

    def test_internal_token_still_retries_once(self):
        """내부 발급 토큰(``tk=None``) → HTTP 2회, get_token 2회 (최초+재발급)."""
        _clear_retry_events()
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["initial", "refreshed"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.get_product("x")

        assert sc == 200
        # 대조군: 내부 경로는 재시도한다.
        assert (
            req_log["count"] == 2
        ), f"내부 토큰 경로 HTTP 요청: {req_log['count']}회 (예상 2 — 재시도 1회)"
        assert (
            tok_log["count"] == 2
        ), f"내부 토큰 경로 get_token 호출: {tok_log['count']}회 (예상 2 — 최초+재발급)"
        assert len(naver_client._AUTHN_RETRY_EVENTS) == 1


# --------------------------------------------------------------------------- #
# 시나리오 14: 단조 증가 카운터 ``_AUTHN_RETRY_COUNT`` (WO PR #27 8라운드 ①).
#
# ``_AUTHN_RETRY_EVENTS`` 가 ``deque(maxlen=1000)`` 이라 가득 차면 ``len()``
# 이 안 변한다. ``mcp_server`` 의 삭제 404 예외 판정이 ``len()`` 에 의존하면
# 포화 시점부터 영구히 "재시도 없었음"으로 잘못 판정한다. 단조 증가 카운터는
# 버퍼 크기와 무관하게 매 재시도마다 올라간다.
# --------------------------------------------------------------------------- #


class TestRetryCounterMonotonic:
    """``_AUTHN_RETRY_COUNT`` 가 재시도마다 단조 증가하고, 버퍼 포화와 무관하다."""

    def test_counter_starts_at_zero_and_increments(self):
        """재시도 1회 → 카운터 1 증가. clear/reset 시 0."""
        _clear_retry_events()
        naver_client._AUTHN_RETRY_COUNT = 0
        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["initial", "new"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                naver_client.get_product("x")

        assert (
            naver_client._AUTHN_RETRY_COUNT == 1
        ), f"재시도 1회 후 카운터: {naver_client._AUTHN_RETRY_COUNT} (예상 1)"

    def test_counter_increments_even_when_buffer_full(self):
        """버퍼가 ``maxlen`` 까리 찬 상태에서도 카운터는 증가한다.

        이것이 핵심: ``len(_AUTHN_RETRY_EVENTS)`` 는 포화 시 변하지 않지만,
        ``_AUTHN_RETRY_COUNT`` 는 버퍼 크기와 무관하게 올라간다.
        """
        _clear_retry_events()
        naver_client._AUTHN_RETRY_COUNT = 0
        maxlen = naver_client._AUTHN_RETRY_EVENTS_MAXLEN
        # 버퍼를 가득 채운다.
        for i in range(maxlen):
            naver_client._AUTHN_RETRY_EVENTS.append({"url": f"prep/{i}", "retried": True})
        len_before = len(naver_client._AUTHN_RETRY_EVENTS)
        assert len_before == maxlen, "전제: 버퍼가 가득 참"

        responses = [
            _FakeResponse(401, json_body={"code": "GW.AUTHN"}),
            _FakeResponse(200, json_body={"ok": True}),
        ]
        req_mock, req_log = _make_request_mock(responses)
        tok_mock, tok_log = _make_token_mock(["initial", "new"])

        with mock.patch.object(naver_client.requests, "get", side_effect=req_mock):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                naver_client.get_product("x")

        # 카운터는 증가했다.
        assert (
            naver_client._AUTHN_RETRY_COUNT == 1
        ), f"버퍼 포화 상태 재시도 후 카운터: {naver_client._AUTHN_RETRY_COUNT} (예상 1)"
        # ``len()`` 은 포화 상태라 여전히 maxlen (변하지 않음).
        assert len(naver_client._AUTHN_RETRY_EVENTS) == maxlen, (
            "버퍼는 포화 상태로 ``len()`` 불변 — " "이것이 ``len()`` 대신 카운터를 쓰는 이유다."
        )
