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
# 시나리오 10: ``register_product`` 다중 POST 재시도 시 토큰 1회 발급 고정.
#
# ``register_product`` 는 한 논리적 등록 작업이 여러 POST 로 이뤄진다
# (첫 POST → 제한태그 응답 → 태그 제거 후 재시도 → 전체 제거 재시도).
# ``_api_request`` 안에서 재발급한 토큰이 지역 변수로 끝나면, 다음 POST 가
# 다시 만료된 토큰을 써 401 을 한 번 더 맞고 토큰을 또 발급한다.
# 본 시험은 **등록 작업 전체에서 get_token 호출이 정확히 1회** 임을 고정한다.
# --------------------------------------------------------------------------- #


class TestRegisterProductTokenIssuedOnceAcrossRetries:
    """``register_product`` 가 여러 번 재시도하더라도 토큰 발급은 1회로
    그치는지 검증한다.

    시나리오:
      - 첫 POST: 401 GW.AUTHN → 토큰 재발급 → 재시도 → 400 제한태그 응답.
      - 두 번째 POST (태그 제거 후): 새 토큰으로 나가야 함 — 401 다시 없음.
      - 세 번째 POST (전체 제거 후): 새 토큰으로 나가야 함 — 401 다시 없음.
    """

    def test_register_product_issues_token_once(self):
        _clear_retry_events()

        # 제한태그 응답 본문 (``invalidInputs`` 형식 — ``_is_restricted_seller_tags_response``).
        restricted_body = {
            "code": "BAD_REQUEST",
            "invalidInputs": [
                {"type": "Restricted.sellerTags", "message": "(제한태그1)"},
            ],
        }

        post_calls = {"n": 0}

        def _mock_post(url, **kwargs):
            post_calls["n"] += 1
            # _api_request 는 내부적으로 requests.post 를 두 번 부를 수 있다.
            # 호출 패턴 (올바른 전파 시):
            #   1: POST#1 첫 시도 (만료 토큰) -> 401
            #   2: POST#1 재시도 (새 토큰)   -> 400 제한태그
            #   3: POST#2 첫 시도 (새 토큰)   -> 400 제한태그
            #   4: POST#2 재시도             -> (루프 내, 401 아님)
            #   ... 이하 생략 ...
            # 버그(전파 안 됨) 시:
            #   1: POST#1 만료 -> 401, 2: POST#1 새 토큰 -> 400 제한태그,
            #   3: POST#2 만료 -> 401, 4: POST#2 새 토큰 -> 400, ... 매번 401.
            headers = kwargs.get("headers", {})
            auth = headers.get("Authorization", "")
            # "expired" 토큰이면 401 GW.AUTHN, "fresh" 토큰이면 단계별 응답.
            if "expired" in auth:
                return _FakeResponse(401, json_body={"code": "GW.AUTHN"})
            # fresh 토큰 — fresh 호출 순서로 단계 판별.
            if not hasattr(_mock_post, "_fresh"):
                _mock_post._fresh = 0
            _mock_post._fresh += 1
            if _mock_post._fresh <= 2:
                # 첫 두 번의 fresh POST: 제한태그 응답.
                r = _FakeResponse(400, json_body=restricted_body)
                # _json_or_text_response 가 content-type 으로 JSON 여부를 판별하므로
                # content-type 헤더를 명시적으로 설정한다.
                r.headers = {"content-type": "application/json"}
                return r
            # 세 번째 fresh POST: 성공.
            r = _FakeResponse(200, json_body={"originProductNo": "1", "channelProductNo": "2"})
            r.headers = {"content-type": "application/json"}
            return r

        tok_mock, tok_log = _make_token_mock(["fresh-token"])

        payload = {
            "originProduct": {
                "deliveryInfo": {"deliveryCompany": "CJGLS"},
                "images": {"representativeImage": {"url": "https://x/y.jpg"}},
                "detailAttribute": {"seoInfo": {"sellerTags": [{"text": "제한태그1"}]}},
            }
        }

        with mock.patch.object(naver_client.requests, "post", side_effect=_mock_post):
            with mock.patch.object(naver_client, "get_token", side_effect=tok_mock):
                sc, body = naver_client.register_product(payload, tk="expired")

        # 핵심 단정: get_token 호출은 등록 작업 전체에서 정확히 1회.
        # (여러 POST 가 있더라도 첫 401 에서 재발급한 토큰이 이어져야 한다.)
        assert tok_log["count"] == 1, (
            f"get_token 호출 횟수: {tok_log['count']} (예상 1) — "
            f"재발급 토큰이 뒤따르는 POST 로 이어지지 않음"
        )
        # 등록은 결국 성공한다.
        assert sc == 200, f"최종 상태코드: {sc}"
