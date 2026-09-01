# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""check_config 연결 진단(connection probe) 테스트.

본 파일은 ``check_config(probe=...)`` / ``check_config(include_public_ip=...)``
경로가 다음 계약을 지키는지 검증한다 — **모두 mock, 실제 외부 호출 없음**.

테스트 목록(티켓 a~h 대응):
  (a) probe=False → 외부 호출 0회 (기본 동작 속도 저하 없음 증명).
  (b) 403 mock → IP 허용목록 불일치를 최우선 원인으로 제시 + 확인 위치 안내.
  (c) 401 mock → 자격증명 문제로 해석 (403 과 구분).
  (d) 네트워크 예외 mock → 사유를 있는 그대로 (조용한 성공 금지).
  (e) 성공 mock → "정상", 토큰 값은 반환에 없음 (canary).
  (f) 오류 본문에 절대경로/계정명이 섞여 있으면 정화된다.
  (g) include_public_ip=False → 외부 호출 0회; True 지만 실패 → 진단은 살아 있음.
  (h) check_config 기존 반환 키가 모두 보존된다 (호환 회귀).

**모든 네트워크 호출은 mock 처리한다** — 이 파일이 실행될 때
api.commerce.naver.com 이나 api.ipify.org 로 실제 패킷이 나가면 안 된다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import bcrypt

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client


# ---------------------------------------------------------------------------- #
# 테스트용 config fixture — check_config 가 파일을 읽을 수 있게.
# ---------------------------------------------------------------------------- #
def _write_complete_config(tmp_path) -> Path:
    """완전한 config.json 을 tmp_path 에 쓰고 경로를 반환한다.

    check_config 가 정상 경로(조기 반환 아님)를 타려면 naver 섹션의 키 3종이
    모두 채워져 있어야 한다. probe 테스트는 이 정상 경로 위에서 동작한다.

    **client_secret 은 유효한 bcrypt salt 형태여야 한다.** 네이버 커머스 API
    의 ``client_secret`` 은 bcrypt 해시 문자열(``$2b$12$...``)이며,
    ``_probe_token_endpoint`` 는 이 값을 ``bcrypt.hashpw`` 의 salt 인수로
    직접 쓴다. 임의 문자열을 넣으면 ``ValueError: Invalid salt`` 가 발생해
    ``requests.post`` (mock) 에 도달하기 전에 실패한다. ``bcrypt.gensalt()``
    로 매 테스트마다 새 유효 salt 를 생성한다 — 이 salt 자체는 시크릿이 아님.
    """
    valid_bcrypt_salt = bcrypt.gensalt(rounds=4).decode()
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "naver": {
                    "client_id": "test_client_id",
                    "client_secret": valid_bcrypt_salt,
                    "store_url_slug": "test-slug",
                    "type": "SELF",
                }
            }
        ),
        encoding="utf-8",
    )
    return config_file


class _FakeResponse:
    """requests.post mock 이 반환할 가짜 응답 객체."""

    def __init__(
        self, status_code: int, body=None, text: str = "", content_type: str = "application/json"
    ):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text or (json.dumps(self._body, ensure_ascii=False) if body is not None else "")
        self.headers = {"content-type": content_type}

    def json(self):
        return self._body


# ============================================================================ #
# (a) probe=False → 외부 호출 0회
# ============================================================================ #
class TestProbeDefaultOff:
    """(a) probe 를 요청하지 않으면 외부 API 호출이 0회다."""

    def test_no_probe_keys_when_probe_false(self, tmp_path, monkeypatch):
        """probe=False 면 connection_probe/connection_hint 키가 결과에 없다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        # _probe_token_endpoint 가 호출되면 즉시 실패하도록 spy 를 단다.
        with mock.patch.object(
            naver_client, "_probe_token_endpoint", side_effect=AssertionError("must not be called")
        ):
            result = mcp_server.check_config()

        assert "connection_probe" not in result
        assert "connection_hint" not in result
        assert "public_ip" not in result

    def test_default_call_count_zero(self, tmp_path, monkeypatch):
        """check_config() 기본 호출이 네트워크 호출을 발생시키지 않는다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        post_calls = {"count": 0}

        def _count_post(*args, **kwargs):
            post_calls["count"] += 1
            return _FakeResponse(200, body={"access_token": "x"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_count_post):
            mcp_server.check_config()

        assert (
            post_calls["count"] == 0
        ), f"probe=False 인데 naver_client.requests.post 가 {post_calls['count']}회 호출됨"


# ============================================================================ #
# (b) 403 mock → IP 허용목록 불일치 최우선
# ============================================================================ #
class TestProbeForbiddenIpAllowlist:
    """(b) 403 응답이 IP 허용목록 불일치 안내로 번역되는가."""

    def test_403_hint_mentions_ip_allowlist_first(self, tmp_path, monkeypatch):
        """403 → '호출 IP 허용목록 불일치 가능성 최우선' 안내."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(403, body={"message": "forbidden"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        probe = result["connection_probe"]
        assert probe["ok"] is False
        assert probe["status_code"] == 403
        hint = result["connection_hint"]
        assert "IP 허용목록 불일치" in hint or "호출 IP" in hint

    def test_403_hint_mentions_check_location(self, tmp_path, monkeypatch):
        """403 안내에 확인 위치(커머스API 센터, 애플리케이션 > 내스토어 애플리케이션)가 있다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(403, body={"message": "denied"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        hint = result["connection_hint"]
        assert "apicenter.commerce.naver.com" in hint
        assert "[애플리케이션]" in hint
        assert "[내스토어 애플리케이션]" in hint

    def test_403_hint_mentions_router_ip_change(self, tmp_path, monkeypatch):
        """403 안내가 공유기/회선 재연결로 공인 IP 변경 가능성을 언급한다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(403, body={"message": "denied"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        hint = result["connection_hint"]
        # 공유기 교체 또는 회선 재연결 언급.
        assert "공유기" in hint or "회선" in hint

    def test_403_probe_call_count_one(self, tmp_path, monkeypatch):
        """probe=True 일 때 토큰 엔드포인트로 정확히 1회 POST 한다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        post_calls = {"count": 0}

        def _count_post(*args, **kwargs):
            post_calls["count"] += 1
            return _FakeResponse(403, body={"message": "denied"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_count_post):
            mcp_server.check_config(probe=True)

        assert post_calls["count"] == 1


# ============================================================================ #
# (c) 401 mock → 자격증명 문제
# ============================================================================ #
class TestProbeUnauthorizedCredentials:
    """(c) 401 응답이 자격증명 문제로 번역되는가 (403 과 구분)."""

    def test_401_hint_mentions_credentials(self, tmp_path, monkeypatch):
        """401 → 자격증명(client_id/client_secret) 문제 안내."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(401, body={"message": "unauthorized"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        probe = result["connection_probe"]
        assert probe["ok"] is False
        assert probe["status_code"] == 401
        hint = result["connection_hint"]
        assert "자격증명" in hint
        assert "401" in hint

    def test_401_hint_distinguishes_from_403(self, tmp_path, monkeypatch):
        """401 안내가 403(IP 허용목록) 과 다름을 명시한다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(401, body={"message": "unauthorized"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        hint = result["connection_hint"]
        # 401 안내가 "IP 허용목록(403) 과 구분" 문구를 포함한다.
        assert "403" in hint or "IP 허용목록" in hint
        # 하지만 "최우선" 단정은 401 안내에 없어야 한다(401 은 자격증명 문제).
        assert "최우선" not in hint


# ============================================================================ #
# (d) 네트워크 예외 mock → 사유 있는 그대로 (조용한 성공 금지)
# ============================================================================ #
class TestProbeNetworkError:
    """(d) 네트워크 예외가 사유를 잃지 않고 그대로 전달되는가."""

    def test_network_exception_preserves_reason(self, tmp_path, monkeypatch):
        """requests.RequestException → ok=False, status_code=None, detail 에 사유."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _boom(*args, **kwargs):
            raise naver_client.requests.ConnectionError("DNS resolution failed")

        with mock.patch.object(naver_client.requests, "post", side_effect=_boom):
            result = mcp_server.check_config(probe=True)

        probe = result["connection_probe"]
        assert probe["ok"] is False
        assert probe["status_code"] is None
        # 예외 타입(ConnectionError) 이 detail 에 남는다.
        assert "ConnectionError" in probe["detail"]

    def test_network_hint_no_silent_success(self, tmp_path, monkeypatch):
        """네트워크 실패 안내가 '조용한 성공' 으로 바뀌지 않는다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _boom(*args, **kwargs):
            raise naver_client.requests.Timeout("read timed out")

        with mock.patch.object(naver_client.requests, "post", side_effect=_boom):
            result = mcp_server.check_config(probe=True)

        hint = result["connection_hint"]
        assert "네트워크" in hint or "연결" in hint
        # "정상" 이라는 단어가 성공을 의미하는 맥락에서 등장하면 안 된다.
        assert "정상: 토큰" not in hint

    def test_network_exception_call_count_one(self, tmp_path, monkeypatch):
        """예외가 발생해도 probe 시도는 1회다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        post_calls = {"count": 0}

        def _boom(*args, **kwargs):
            post_calls["count"] += 1
            raise naver_client.requests.ConnectionError("down")

        with mock.patch.object(naver_client.requests, "post", side_effect=_boom):
            mcp_server.check_config(probe=True)

        assert post_calls["count"] == 1


# ============================================================================ #
# (e) 성공 mock → 정상, 토큰 값 노출 없음 (canary)
# ============================================================================ #
class TestProbeSuccess:
    """(e) 2xx 응답이 '정상' 으로 번역되고, 토큰 값이 반환에 없는가."""

    def test_success_hint_says_ok(self, tmp_path, monkeypatch):
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(200, body={"access_token": "AAAA-BBBB-CCCC-SECRET-TOKEN"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        probe = result["connection_probe"]
        assert probe["ok"] is True
        assert probe["status_code"] == 200
        assert probe["detail"] == "정상"
        hint = result["connection_hint"]
        assert "정상" in hint

    def test_success_token_value_not_in_result(self, tmp_path, monkeypatch):
        """성공 응답의 access_token 값이 결과 어디에도 없다 (canary).

        client_secret(bcrypt salt) 값도 결과에 노출되면 안 된다.
        config 에서 실제 secret 값을 읽어 canary 로 쓴다.
        """
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        # config 에 넣은 client_secret(bcrypt salt) 값을 canary 로 쓴다.
        config_data = json.loads(config_file.read_text(encoding="utf-8"))
        canary_secret = config_data["naver"]["client_secret"]
        canary_token = "AAAA-BBBB-CCCC-SECRET-TOKEN-12345"

        def _fake_post(*args, **kwargs):
            return _FakeResponse(200, body={"access_token": canary_token})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        # 토큰 값(canary) 이 결과 dict 어디에도 없어야 한다.
        result_str = json.dumps(result, ensure_ascii=False)
        assert (
            canary_token not in result_str
        ), "connection_probe 결과에 access_token 값이 노출됨 (canary 실패)"
        # client_secret 값도 결과에 노출되면 안 된다(게이트 계약).
        assert canary_secret not in result_str, "client_secret 값이 결과에 노출됨"


# ============================================================================ #
# (f) 오류 본문에 절대경로/계정명 → 정화
# ============================================================================ #
class TestProbeErrorBodySanitized:
    """(f) 403/401 오류 본문에 민감 정보(경로/계정)가 섞여 있으면 정화되는가."""

    def test_403_body_with_windows_path_sanitized(self, tmp_path, monkeypatch):
        """403 본문에 Windows 사용자 경로가 있으면 [REDACTED] 처리."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        leak_path = "C:" + chr(92) + "Users" + chr(92) + "operator" + chr(92) + "secrets.json"

        def _fake_post(*args, **kwargs):
            return _FakeResponse(
                403,
                body={"message": f"denied for {leak_path}"},
            )

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        detail = result["connection_probe"]["detail"]
        assert leak_path not in detail, "403 detail 에 Windows 경로가 노출됨"
        assert "[REDACTED]" in detail

    def test_401_body_with_api_key_sanitized(self, tmp_path, monkeypatch):
        """401 본문에 sk- 키 형태가 있으면 [REDACTED] 처리."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        leak_key = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"

        def _fake_post(*args, **kwargs):
            return _FakeResponse(
                401,
                body={"message": f"auth failed key={leak_key}"},
            )

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        detail = result["connection_probe"]["detail"]
        assert leak_key not in detail, "401 detail 에 sk- 키가 노출됨"
        assert "[REDACTED]" in detail

    def test_network_exception_message_sanitized(self, tmp_path, monkeypatch):
        """네트워크 예외 메시지에 경로가 섞여 있으면 정화된다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        leak_path = "/home/operator/config.json"

        def _boom(*args, **kwargs):
            raise naver_client.requests.ConnectionError(f"cannot read {leak_path}")

        with mock.patch.object(naver_client.requests, "post", side_effect=_boom):
            result = mcp_server.check_config(probe=True)

        detail = result["connection_probe"]["detail"]
        assert leak_path not in detail, "네트워크 예외 detail 에 POSIX 경로가 노출됨"


# ============================================================================ #
# (g) 공인 IP 조회 — opt-in + 부분 실패 허용
# ============================================================================ #
class TestPublicIpLookup:
    """(g) include_public_ip 동작: 기본 OFF, 실패해도 진단은 살아 있음."""

    def test_public_ip_off_by_default(self, tmp_path, monkeypatch):
        """include_public_ip=False 면 public_ip 키가 없다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        import requests as _real_requests

        get_calls = {"count": 0}
        real_get = _real_requests.get

        def _count_get(*args, **kwargs):
            get_calls["count"] += 1
            return real_get(*args, **kwargs)

        with mock.patch("requests.get", side_effect=_count_get):
            mcp_server.check_config()

        assert (
            get_calls["count"] == 0
        ), f"include_public_ip=False 인데 requests.get 이 {get_calls['count']}회 호출됨"

    def test_public_ip_off_no_key(self, tmp_path, monkeypatch):
        """include_public_ip=False 면 public_ip 키가 결과에 없다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        assert "public_ip" not in result

    def test_public_ip_on_success(self, tmp_path, monkeypatch):
        """include_public_ip=True + 성공 → public_ip.ip 가 채워진다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_get(*args, **kwargs):
            ip_resp = _FakeResponse(200, body={"ip": "203.0.113.42"})
            return ip_resp

        with mock.patch("requests.get", side_effect=_fake_get):
            result = mcp_server.check_config(include_public_ip=True)

        assert "public_ip" in result
        assert result["public_ip"]["ok"] is True
        assert result["public_ip"]["ip"] == "203.0.113.42"
        assert result["public_ip"]["source"] == "https://api.ipify.org?format=json"

    def test_public_ip_on_failure_diagnostic_survives(self, tmp_path, monkeypatch):
        """include_public_ip=True + IP 조회 실패해도 probe/기존 진단은 살아 있다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(403, body={"message": "denied"})

        def _boom_get(*args, **kwargs):
            raise naver_client.requests.ConnectionError("ipify down")

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            with mock.patch("requests.get", side_effect=_boom_get):
                result = mcp_server.check_config(probe=True, include_public_ip=True)

        # probe 결과는 살아 있다.
        assert "connection_probe" in result
        assert result["connection_probe"]["ok"] is False
        assert result["connection_probe"]["status_code"] == 403
        # IP 조회 실패도 결과에 반영된다(조용한 성공 아님).
        assert "public_ip" in result
        assert result["public_ip"]["ok"] is False
        assert result["public_ip"]["ip"] is None
        # 기존 진단 키도 살아 있다.
        assert "ok" in result
        assert "present" in result


# ============================================================================ #
# (h) check_config 기존 반환 키 보존 (호환 회귀)
# ============================================================================ #
class TestCheckConfigKeysPreserved:
    """(h) probe/include_public_ip 추가로 기존 반환 키가 사라지지 않는가."""

    # 기존(이 기능 추가 전) 부터 존재하던 반환 키 목록. 이 중 하나라도
    # 사라지면 호환 회귀다.
    _LEGACY_KEYS = frozenset(
        {
            "ok",
            "config_path",
            "present",
            "missing",
            "placeholders",
            "error",
            "origin_configured",
            "as_tel_configured",
            "policy_gaps",
            "suggested_from_existing",
            "drift_from_existing",
            "existing_read_error",
            "image_generation_configured",
            "templates",
            "templates_read_error",
        }
    )

    def test_default_call_preserves_all_legacy_keys(self, tmp_path, monkeypatch):
        """probe=False, include_public_ip=False 일 때 기존 키가 모두 있다."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        for key in self._LEGACY_KEYS:
            assert key in result, f"기존 반환 키 {key!r} 가 check_config 기본 호출에서 사라짐"

    def test_probe_on_preserves_all_legacy_keys(self, tmp_path, monkeypatch):
        """probe=True 여도 기존 키가 모두 있다 (probe 는 새 키만 추가)."""
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(200, body={"access_token": "x"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        for key in self._LEGACY_KEYS:
            assert key in result, f"probe=True 일 때 기존 반환 키 {key!r} 가 사라짐"
        # 새 키도 있다.
        assert "connection_probe" in result
        assert "connection_hint" in result

    def test_ok_meaning_unchanged(self, tmp_path, monkeypatch):
        """ok 키의 의미가 probe 결과와 무관하게 기존대로다.

        ok 는 '필수 키가 모두 있고 플레이스홀더가 아님' 이다. probe 가 실패해도
        ok 가 바뀌면 안 된다(ok 의 의미를 변경하지 마라).
        """
        config_file = _write_complete_config(tmp_path)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        def _fake_post(*args, **kwargs):
            return _FakeResponse(403, body={"message": "denied"})

        with mock.patch.object(naver_client.requests, "post", side_effect=_fake_post):
            result = mcp_server.check_config(probe=True)

        # config 가 완전하므로 ok=True (probe 403 과 무관).
        assert result["ok"] is True
        # probe 는 실패.
        assert result["connection_probe"]["ok"] is False

    def test_eleven_tools_registered_unchanged(self):
        """probe 추가로 MCP 도구 수가 변하지 않는다 (12개 유지)."""
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            tools = asyncio.run(tools)
        assert (
            len(tools) == 12
        ), f"Expected 11 tools (probe is a parameter, not a new tool), got {len(tools)}"
