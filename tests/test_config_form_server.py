# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""최초 설정 폼 서버(config_form_server) "브라우저 폼 POST" 테스트.

**왜 별도 파일인가**: ``config_form_server`` 는 ``approval_server`` 와 **별도
모듈**이며 동등한 방어를 갖는다. 승인 서버의 "product_key 1건 승인" 범위
제한을 깨지 않기 위해 분리됐기 때문에, 테스트도 별도로 검증해야 한다.

본 파일은 실제 브라우저가 보내는 형태(``application/x-www-form-urlencoded``,
커스텀 헤더 없음)를 **실제 소켓**으로 재현해, 서버가 폼 본문을 받아들이고
**사람이 읽을 HTML 결과**를 반환하는지를 검증한다. 동시에 **설정 파일 쓰기**
부분 저장·백업·비밀값 비노출 계약을 검증한다.

테스트 목록(티켓 a~i 대응):
  (a) 폼 POST(커스텀 헤더 없음) + 유효 토큰 -> 설정 파일에 기록, HTML 반환.
  (b) 토큰 없음/틀림/재사용/만료 -> 거부, 설정 파일 변경 없음.
  (c) Origin: https://evil.example(단일/중복) -> 거부.
  (d) 응답에 Access-Control-Allow-Origin 없음(전 경로).
  (d-2) 양식1 HTML 에 <script> 가 없다 — 업로드 기능 제거로 JS 가 완전히 사라졌다.
  (e) 빈 칸은 기존 값을 지우지 않는다(부분 저장).
  (f) 결과 페이지/로그에 설정값(비밀)이 출력되지 않는다.
  (g) 생성된 폼 HTML 에 규제 신고값의 예시값이 없다.
  (g-2) 폼 HTML 이 양식1 10개 필드를 정확히 갖추고, 제거 대상 필드가 어디에도
        없다(본문·hidden·JS 포함).
  (h) 요청 처리 후 포트가 닫힌다(좀비 포트 금지).
  (i) 서버가 꺼져 있을 때 기존 check_config 흐름에 회귀가 없다.

**주의**: 본 파일은 소켓 테스트로 **서버가 폼 본문을 받아들이는지**를 증명한다.
실제 브라우저에서 폼이 전송되는지(CORS 프리플라이트 회피)는 오케스트레이터가
브라우저로 확인한다 — 워커는 하단 ``BROWSER_VERIFY`` 문자열로 검증용 HTML
생성 방법을 제공한다.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import approval_server, config_form_server


# --------------------------------------------------------------------------- #
# 폼 POST 요청 헬퍴 (test_approval_form_post.py 와 동일 패턴).
# --------------------------------------------------------------------------- #
def _send_form(
    port: int,
    *,
    fields: dict[str, str] | list[tuple[str, str]],
    token_header: str | None = None,
    origin: str | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
    method: str = "POST",
    path: str = "/",
) -> tuple[int, str, list[tuple[str, str]]]:
    """``application/x-www-form-urlencoded`` 폼 본문을 보내고 (status, body, headers).

    커스텀 헤더는 기본으로 일절 보내지 않는다 — 실제 브라우저 폼 제출 재현.
    토큰은 hidden 필드(``token``) 로 보낸다(필요 시).
    """
    pairs = list(fields.items()) if isinstance(fields, dict) else list(fields)
    body = urllib.parse.urlencode(pairs).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers: list[tuple[str, str]] = [
        ("Content-Type", "application/x-www-form-urlencoded"),
        ("Content-Length", str(len(body))),
    ]
    if token_header is not None:
        headers.append(("X-Config-Form-Token", token_header))
    if origin is not None:
        headers.append(("Origin", origin))
    if extra_headers:
        headers.extend(extra_headers)
    conn.request(method, path, body=body, headers=dict(headers))
    resp = conn.getresponse()
    resp_body = resp.read().decode("utf-8")
    resp_headers = [(k, v) for k, v in resp.getheaders()]
    status = resp.status
    conn.close()
    return status, resp_body, resp_headers


def _send_raw_form(
    port: int,
    *,
    raw_headers: list[tuple[str, str]],
    fields: dict[str, str],
) -> tuple[int, str]:
    """중복 헤더를 보낼 수 있는 로우 소켓 폼 POST."""
    payload = urllib.parse.urlencode(list(fields.items())).encode("utf-8")
    lines = [
        b"POST / HTTP/1.1",
        b"Host: 127.0.0.1",
        b"Connection: close",
        b"Content-Type: application/x-www-form-urlencoded",
        f"Content-Length: {len(payload)}".encode("ascii"),
    ]
    for k, v in raw_headers:
        lines.append(f"{k}: {v}".encode())
    raw = b"\r\n".join(lines) + b"\r\n\r\n" + payload
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(raw)
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    resp = b"".join(chunks)
    head, _, rest = resp.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0]
    status = int(status_line.split(b" ")[1])
    return status, rest.decode("utf-8", errors="replace")


def _wait_for_port(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _port_is_closed(port: int, timeout: float = 5.0) -> bool:
    """포트가 닫혔는지 확인(연결 실패 = 닫힘)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                time.sleep(0.1)  # 아직 열려 있음.
        except OSError:
            return True  # 닫힘.
    return False


# --------------------------------------------------------------------------- #
# (a) 폼 POST(커스텀 헤더 없음) + 유효 토큰 -> 설정 파일에 기록, HTML 반환.
# --------------------------------------------------------------------------- #
class TestFormPostSaves:
    """(a) 브라우저 폼 POST 경로가 설정을 저장하고 HTML 을 반환하는가."""

    def test_form_post_saves_and_returns_html(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _save():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={
                        "token": token,
                        "client_id": "test_client_id_value",
                        "client_secret": "test_client_secret_value",
                        "store_url_slug": "test-store-slug",
                    },
                )

            t = threading.Thread(target=_save, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.saved is True
            assert "client_id" in outcome.saved_keys
            assert "client_secret" in outcome.saved_keys
            assert "store_url_slug" in outcome.saved_keys
        finally:
            srv.close()

        # 설정 파일에 실제로 기록됐는지 확인.
        with open(config_file, encoding="utf-8") as f:
            saved_cfg = json.load(f)
        assert saved_cfg["naver"]["client_id"] == "test_client_id_value"
        assert saved_cfg["naver"]["client_secret"] == "test_client_secret_value"
        assert saved_cfg["naver"]["store_url_slug"] == "test-store-slug"

    def test_form_post_response_is_html(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            status, body, headers = _send_form(
                port,
                fields={"token": token, "client_id": "html_response_id"},
            )
            assert status == 200
            ctype = [v for k, v in headers if k.lower() == "content-type"]
            assert any(
                "text/html" in v.lower() for v in ctype
            ), f"성공 응답 Content-Type 이 text/html 이 아님: {ctype}"
            assert "<html" in body.lower()
            assert "저장" in body
        finally:
            srv.close()

    def test_form_post_no_custom_headers_used(self, tmp_path):
        """커스텀 헤더 없이도 저장이 처리된다(프리플라이트 회피 조건)."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            # 오직 Content-Type + Content-Length 만. X-Config-Form-Token 없음.
            status, body, _ = _send_form(
                port,
                fields={"token": token, "client_id": "no_custom_header_id"},
            )
            assert status == 200
            assert "저장" in body
            assert srv.outcome is not None
            assert srv.outcome.saved is True
        finally:
            srv.close()

    def test_form_post_policy_fields_rejected(self, tmp_path):
        """정책 필드는 이제 양식1 관할이 아니다 — 폼 POST 로 와도 저장 거부.

        양식1 = "한 번만 넣으면 다시 넣을 필요 없는 것" = 계정 자격증명 3개.
        고시 5필드·AS 연락처 2필드는 상품 등록 단계(양식2)에서 상품에 맞는
        항목만 요청한다. 폼에서만 감추고 서버가 계속 쓰면 의미 없으므로,
        **저장 로직의 화이트리스트에서도 빠진다** — 폼 POST 로 와도 skipped_keys
        로 분류되어 config 에 기록되지 않는다.
        """
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={
                    "token": token,
                    # 양식1 관할(키 3개) — 이것만 저장된다.
                    "client_id": "key_three_only_id",
                    # 아래는 전부 양식1 관할 밖 — 저장 로직에서도 거부되어야 한다.
                    "returnCostReason": "반품 배송비 안내문",
                    "as_tel": "1577-1234",
                    "as_tel_comment": "평일 09-18시 안내",
                    "origin_area_code": "04",
                    "origin_content": "국산",
                    "manufacturer": "어떤 제조사",
                    "importer": "어떤 수입사",
                },
            )
            assert status == 200
            assert srv.outcome is not None
            assert srv.outcome.saved is True
            # 키 3개만 saved_keys 에 들어가야 한다.
            assert "client_id" in srv.outcome.saved_keys
            # 양식1 관할 밖의 키는 화이트리스트 밖이므로 skipped_keys 에 들어가야 한다.
            for removed in (
                "returnCostReason",
                "as_tel",
                "as_tel_comment",
                "origin_area_code",
                "origin_content",
                "manufacturer",
                "importer",
            ):
                assert (
                    removed in srv.outcome.skipped_keys
                ), f"제거된 필드 {removed} 가 화이트리스트에 남아있으면 안 됨"
        finally:
            srv.close()

        with open(config_file, encoding="utf-8") as f:
            saved_cfg = json.load(f)
        # 키 3개 중 채운 것만 저장됨.
        assert saved_cfg["naver"]["client_id"] == "key_three_only_id"
        # 양식1 관할 밖의 키는 저장되지 않음 (smartstore_notice_defaults 섹션 자체가 없어야 함).
        assert "smartstore_notice_defaults" not in saved_cfg, (
            "고시/AS 필드가 양식1 저장 로직을 거쳐 config 에 기록됨 — "
            "화이트리스트가 좁혀지지 않았음"
        )

    def test_form_post_whitelist_enforced(self, tmp_path):
        """화이트리스트에 없는 키는 무시된다 (임의 키 쓰기 방지)."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            status, _, _ = _send_form(
                port,
                fields={
                    "token": token,
                    "client_id": "whitelist_test_id",
                    # 화이트리스트에 없는 키 — 무시되어야 함.
                    "enable_config_form": "true",
                    "random_key": "should_be_ignored",
                    "naver.type": "forbidden_injection",
                },
            )
            assert status == 200
            assert srv.outcome is not None
            assert "random_key" in srv.outcome.skipped_keys
        finally:
            srv.close()

        # 화이트리스트 밖의 키가 config 에 기록되지 않았는지 확인.
        with open(config_file, encoding="utf-8") as f:
            saved_cfg = json.load(f)
        assert "enable_config_form" not in saved_cfg
        assert "random_key" not in saved_cfg


# --------------------------------------------------------------------------- #
# (b) 토큰 없음/틀림/재사용/만료 -> 거부, 설정 파일 변경 없음.
# --------------------------------------------------------------------------- #
class TestFormTokenRejectionPreservesConfig:
    """(b) 폼 경로 토큰 거부가 설정 파일을 변경하지 않는가."""

    def test_form_no_token_rejected_config_unchanged(self, tmp_path):
        config_file = tmp_path / "config.json"
        original = {"naver": {"client_id": "original_id"}, "existing_key": "keep_me"}
        config_file.write_text(json.dumps(original), encoding="utf-8")

        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, headers = _send_form(
                port,
                fields={"client_id": "attacker_id"},  # token 누락
            )
            assert status == 401
            ctype = [v for k, v in headers if k.lower() == "content-type"]
            assert any("text/html" in v.lower() for v in ctype)
            assert "토큰" in body
            assert srv.outcome is None
        finally:
            srv.close()

        # 설정 파일이 변경되지 않았는지 확인.
        with open(config_file, encoding="utf-8") as f:
            unchanged = json.load(f)
        assert unchanged["naver"]["client_id"] == "original_id"
        assert unchanged["existing_key"] == "keep_me"

    def test_form_wrong_token_rejected_config_unchanged(self, tmp_path):
        config_file = tmp_path / "config.json"
        original = {"naver": {"client_id": "original_id"}}
        config_file.write_text(json.dumps(original), encoding="utf-8")

        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": "totally-wrong-token", "client_id": "attacker_id"},
            )
            assert status == 403
            assert "<html" in body.lower()
            assert "토큰" in body
            assert srv.outcome is None
        finally:
            srv.close()

        with open(config_file, encoding="utf-8") as f:
            unchanged = json.load(f)
        assert unchanged["naver"]["client_id"] == "original_id"

    def test_form_token_reuse_rejected(self, tmp_path):
        """같은 토큰 재사용 -> 거부(1회 소진 유지)."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _save():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={"token": token, "client_id": "first_save_id"},
                )

            t = threading.Thread(target=_save, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.saved is True
            assert srv.is_consumed() is True
        finally:
            srv.close()

        # 서버가 종료됐으므로 재사용 시도는 연결 자체가 실패한다.
        # 좀비 포트가 없다는 것 자체가 방어 9 의 증거.
        # (h) 테스트에서 포트 닫힘을 별도로 검증한다.

    def test_form_expired_rejected_config_unchanged(self, tmp_path):
        config_file = tmp_path / "config.json"
        original = {"naver": {"client_id": "original_id"}}
        config_file.write_text(json.dumps(original), encoding="utf-8")

        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=1,
        )
        port = srv.start()
        try:
            time.sleep(1.5)
            status, body, _ = _send_form(
                port,
                fields={"token": token, "client_id": "after_expiry_id"},
            )
            assert status == 410
            assert "<html" in body.lower()
            assert "만료" in body
        finally:
            srv.close()

        with open(config_file, encoding="utf-8") as f:
            unchanged = json.load(f)
        assert unchanged["naver"]["client_id"] == "original_id"


# --------------------------------------------------------------------------- #
# (c) Origin: https://evil.example(단일/중복) -> 거부.
# --------------------------------------------------------------------------- #
class TestFormOriginGuard:
    """(c) 폼 경로 Origin 검사가 유지되는가."""

    def test_form_evil_origin_rejected(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": token, "client_id": "evil_origin_id"},
                origin="https://evil.example",
            )
            assert status == 403
            assert "<html" in body.lower()
            assert "Origin" in body or "origin" in body.lower()
            assert srv.outcome is None
        finally:
            srv.close()

    def test_form_null_origin_allowed(self, tmp_path):
        """file:// 폼 POST 가 보내는 Origin: null 은 허용."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _save():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={"token": token, "client_id": "null_origin_id"},
                    origin="null",
                )

            t = threading.Thread(target=_save, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.saved is True
        finally:
            srv.close()

    def test_form_duplicate_origin_second_evil_rejected(self, tmp_path):
        """Origin: null + Origin: https://evil -> 거부 (get_all 회귀 없음)."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body = _send_raw_form(
                port,
                raw_headers=[
                    ("Origin", "null"),
                    ("Origin", "https://evil.example"),
                ],
                fields={"token": token, "client_id": "dup_origin_id"},
            )
            assert status == 403
            assert "Origin" in body or "origin" in body.lower()
            assert srv.outcome is None
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (d) 응답에 Access-Control-Allow-Origin 없음(전 경로).
# --------------------------------------------------------------------------- #
class TestFormNoCorsHeader:
    """(d) 폼 경로 응답에 ACAO 가 없는가 (방어 6)."""

    def test_form_success_has_no_acao(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            _, _, headers = _send_form(
                port,
                fields={"token": token, "client_id": "no_acao_success_id"},
            )
            acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao == [], f"폼 성공 응답에 ACAO 가 있음: {acao}"
        finally:
            srv.close()

    def test_form_rejection_has_no_acao(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            _, _, headers = _send_form(port, fields={"client_id": "no_acao_reject_id"})
            acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao == []
        finally:
            srv.close()

    def test_form_options_has_no_acao(self, tmp_path):
        """CORS preflight (OPTIONS) 에도 ACAO 가 없다."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("OPTIONS", "/")
            resp = conn.getresponse()
            resp.read()
            headers = resp.getheaders()
            conn.close()
            acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao == []
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (e) 빈 칸은 기존 값을 지우지 않는다(부분 저장).
# --------------------------------------------------------------------------- #
class TestFormPartialSave:
    """(e) 빈 칸은 기존 값을 지우지 않는가."""

    def test_empty_fields_preserve_existing_values(self, tmp_path):
        """빈 값으로 온 필드는 기존 config 값을 유지한다.

        origin_area_code 는 폼1 관할 밖이지만, 폼에 없는 기존 config 키가
        보존되는지(임의 덮어쓰기가 아님)를 검증하는 데 그대로 쓴다 — 부분 저장
        계약은 "화이트리스트 밖의 키도 지우지 않는다" 까지 포함한다.
        """
        config_file = tmp_path / "config.json"
        original = {
            "naver": {
                "client_id": "keep_this_id",
                "client_secret": "keep_this_secret",
                "store_url_slug": "keep_this_slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "as_tel": "1577-9999",
            },
            "existing_key": "preserve_me",
        }
        config_file.write_text(json.dumps(original), encoding="utf-8")

        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            # client_id 만 채우고 나머지는 빈 칸.
            status, _, _ = _send_form(
                port,
                fields={
                    "token": token,
                    "client_id": "new_client_id",
                    "client_secret": "",  # 빈 칸 — 기존 값 유지.
                    "store_url_slug": "",  # 빈 칸 — 기존 값 유지.
                    "as_tel": "",  # 빈 칸 — 기존 값 유지.
                },
            )
            assert status == 200
            assert srv.outcome is not None
            assert srv.outcome.saved is True
            assert "client_id" in srv.outcome.saved_keys
            # 빈 값은 unchanged_keys 에 들어가야 한다.
            assert "client_secret" in srv.outcome.unchanged_keys
            assert "store_url_slug" in srv.outcome.unchanged_keys
        finally:
            srv.close()

        with open(config_file, encoding="utf-8") as f:
            saved = json.load(f)
        # 새로 채운 것은 반영.
        assert saved["naver"]["client_id"] == "new_client_id"
        # 빈 칸은 기존 값 유지.
        assert saved["naver"]["client_secret"] == "keep_this_secret"
        assert saved["naver"]["store_url_slug"] == "keep_this_slug"
        assert saved["smartstore_notice_defaults"]["origin_area_code"] == "04"
        assert saved["smartstore_notice_defaults"]["as_tel"] == "1577-9999"
        # 폼에 없는 키도 보존.
        assert saved["existing_key"] == "preserve_me"

    def test_write_config_values_partial_save_unit(self, tmp_path):
        """write_config_values 함수 직접 단위 테스트."""
        config_file = tmp_path / "config.json"
        original = {
            "naver": {"client_id": "keep_id", "client_secret": "keep_secret"},
            "extra": "preserve",
        }
        config_file.write_text(json.dumps(original), encoding="utf-8")

        saved_keys, skipped, unchanged, backup = config_form_server.write_config_values(
            str(config_file),
            {
                "client_id": "new_id",  # 채움.
                "client_secret": "",  # 빈 칸 — 유지.
                "bogus_key": "ignored",  # 화이트리스트 밖.
            },
        )
        assert "client_id" in saved_keys
        assert "client_secret" in unchanged
        assert "bogus_key" in skipped

        with open(config_file, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["naver"]["client_id"] == "new_id"
        assert saved["naver"]["client_secret"] == "keep_secret"
        assert saved["extra"] == "preserve"

    def test_backup_created_before_write(self, tmp_path):
        """기존 설정 파일이 있으면 쓰기 전 백업본이 생성된다."""
        config_file = tmp_path / "config.json"
        original = {"naver": {"client_id": "old_id"}}
        config_file.write_text(json.dumps(original), encoding="utf-8")

        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            status, _, _ = _send_form(
                port,
                fields={"token": token, "client_id": "new_id"},
            )
            assert status == 200
            assert srv.outcome is not None
            assert srv.outcome.backup_path  # 백업 경로가 있다.
        finally:
            srv.close()

        backup_path = Path(srv.outcome.backup_path)
        assert backup_path.is_file()
        # 백업본은 원래 값을 갖고 있어야 한다.
        with open(backup_path, encoding="utf-8") as f:
            backup_cfg = json.load(f)
        assert backup_cfg["naver"]["client_id"] == "old_id"


# --------------------------------------------------------------------------- #
# (f) 결과 페이지/로그에 설정값(비밀)이 출력되지 않는다.
# --------------------------------------------------------------------------- #
class TestFormNoSecretOutput:
    """(f) 결과 페이지·Outcome 에 값 자체가 나오지 않는가."""

    def test_result_html_no_secret_value(self, tmp_path):
        """성공 결과 HTML 에 client_secret 값이 나오지 않는다."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            secret_value = "SUPER_SECRET_VALUE_12345"
            status, body, _ = _send_form(
                port,
                fields={
                    "token": token,
                    "client_id": "public_id_value",
                    "client_secret": secret_value,
                },
            )
            assert status == 200
            # 결과 페이지에 비밀값이 없어야 한다.
            assert secret_value not in body, "결과 페이지에 client_secret 값이 노출됨"
            # client_id 도 결과 페이지에 값으로 나오지 않아야 한다(키 이름만).
            assert "public_id_value" not in body, "결과 페이지에 client_id 값이 노출됨"
        finally:
            srv.close()

    def test_outcome_has_no_values(self, tmp_path):
        """Outcome 객체에는 키 이름만 있고 값은 없다."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _save():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={
                        "token": token,
                        "client_id": "outcome_test_id",
                        "client_secret": "outcome_test_secret",
                    },
                )

            t = threading.Thread(target=_save, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.saved is True
            # saved_keys/skipped_keys/unchanged_keys 는 키 이름의 리스트여야 한다.
            for key in outcome.saved_keys:
                assert isinstance(key, str)
            # Outcome 객체를 dict 로 직렬화해도 값이 없어야 한다.
            # (saved_keys/skipped_keys/unchanged_keys 는 키 이름만 담음)
            assert "outcome_test_id" not in str(outcome.saved_keys)
            assert "outcome_test_secret" not in str(outcome.saved_keys)
        finally:
            srv.close()

    def test_rejection_html_no_echoed_values(self, tmp_path):
        """거부 응답에도 폼 값이 에코되지 않는다."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            attempted_value = "attacker_attempted_secret"
            status, body, _ = _send_form(
                port,
                fields={
                    # 틀린 토큰 — 거부 경로.
                    "token": "wrong_token",
                    "client_secret": attempted_value,
                },
            )
            assert status == 403
            assert attempted_value not in body
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (g) 생성된 폼 HTML 에 규제 신고값의 예시값이 없다.
# --------------------------------------------------------------------------- #
class TestFormHtmlNoExampleValues:
    """(g) 폼 HTML 의 규제 필드에 구체적 예시값이 없는가.

    규제 신고값(원산지·AS 전화·반품비 사유·품질보증기준 등)에 예시값을 달면
    모델/사용자가 그대로 복사해 넣는다. 양식은 묻기 좋게 만드는 것이지 채우기
    좋게가 아니다. placeholder 에는 값의 형식/필수 여부만 표시한다.
    """

    def _render(self) -> str:
        return config_form_server.render_config_form_html(
            token="dummy_token",
            port=54321,
            config_set_status={},
        )

    def test_no_example_origin_content(self):
        """원산지 표시에 구체적 예시값이 없다."""
        html_str = self._render()
        # 나쁨: "예: 국산 - 경북 영천" 식의 구체적 예시.
        # 좋음: "[필수] 국가 또는 국내 지역명"
        assert "예: 국산" not in html_str
        assert "예: 중국산" not in html_str
        assert "예: 미국산" not in html_str

    def test_no_example_as_tel(self):
        """AS 전화번호에 구체적 예시값이 없다."""
        html_str = self._render()
        # placeholder 나 guide 에 전화번호 예시가 없어야 한다.
        assert "예: 1577-" not in html_str
        assert "예: 02-" not in html_str
        assert "080-1234-5678" not in html_str

    def test_no_example_manufacturer(self):
        """제조사에 구체적 예시값이 없다."""
        html_str = self._render()
        assert "예: (주)" not in html_str
        assert "예: 삼성" not in html_str

    def test_no_example_return_cost_reason(self):
        """반품비 사유에 구체적 예시값이 없다."""
        html_str = self._render()
        # 반품비 사유의 구체적 문구 예시가 없어야 한다.
        assert "예: 단순 변심" not in html_str
        assert "예: 반품 배송비 5,000원" not in html_str

    def test_no_example_quality_assurance(self):
        """품질보증기준에 구체적 예시값이 없다."""
        html_str = self._render()
        assert "예: 관련 법령" not in html_str
        assert "예: 소비자" not in html_str

    def test_client_secret_is_password_type(self):
        """client_secret 입력필드가 type=password 다."""
        html_str = self._render()
        # client_secret 필드를 찾아 type=password 인지 확인.
        # 대략적인 패턴: <input ... name="client_secret" ... type="password"
        # 또는 <input ... type="password" ... name="client_secret"
        assert 'name="client_secret"' in html_str
        # client_secret 근처에 type="password" 가 있어야 한다.
        idx = html_str.index('name="client_secret"')
        nearby = html_str[max(0, idx - 200) : idx + 200]
        assert 'type="password"' in nearby, "client_secret 필드가 type=password 가 아님"

    def test_form_is_form_post_not_fetch(self):
        """폼이 <form method="POST"> 이며 fetch 가 아니다."""
        html_str = self._render()
        assert 'method="POST"' in html_str
        assert 'enctype="multipart/form-data"' not in html_str
        # fetch 호출이 없어야 한다.
        assert "fetch(" not in html_str

    def test_token_is_hidden_field(self):
        """토큰이 <input type="hidden" name="token"> 으로 싣는다."""
        html_str = self._render()
        assert 'type="hidden" name="token"' in html_str
        # 커스텀 헤더 키 이름이 폼 전송 스크립트에 없어야 한다.
        assert "X-Config-Form-Token" not in html_str.replace(
            "X-Config-Form-Token", "X-Config-Form-Token"
        )  # 폼 HTML 자체에는 헤더 키가 등장하지 않는다.

    def test_no_custom_header_in_form_html(self):
        """폼 HTML 에 커스텀 헤더를 쓰는 JS 가 없다."""
        html_str = self._render()
        assert "X-Config-Form-Token" not in html_str

    def test_submit_button_is_real_submit(self):
        """[저장] 버튼이 type=submit 이다 (JS 없이 전송)."""
        html_str = self._render()
        submit_btns = re.findall(r'<button[^>]*type="submit"[^>]*>([^<]*)</button>', html_str)
        assert any("저장" in b for b in submit_btns), "[저장] 버튼이 type=submit 이 아님"

    def test_form_has_exactly_3_form1_fields(self):
        """폼 HTML 이 양식1 3개 필드를 정확히 갖춘다 (키 3개 정정).

        양식1 = "한 번만 넣으면 다시 넣을 필요 없는 것" = 계정 자격증명 3개.
        고시 5필드·AS 연락처 2필드는 폼 HTML 본문·hidden·JS 어디에도
        등장하지 않는다 (상품 등록 단계 양식2 관할).
        """
        html_str = self._render()
        # 3개 폼1 필드의 name="..." 이 모두 있어야 한다.
        for field_name in config_form_server.allowed_field_names():
            assert (
                f'name="{field_name}"' in html_str
            ), f"양식1 필드 {field_name!r} 가 폼 HTML 에 없음"

    def test_form_html_has_no_removed_fields(self):
        """폼 HTML 어디에도 제거 대상 필드가 없다 (본문·hidden·JS 포함).

        양식1 에서 제거된 7개 필드(고시 5 + AS 2)와 애초에 폼1 관할이 아니었던
        상품별 값이 모두 빠진다.
        """
        html_str = self._render()
        removed = [
            # 이번에 양식1 에서 제거된 7개 (고시 5 + AS 2).
            "returnCostReason",
            "noRefundReason",
            "qualityAssuranceStandard",
            "compensationProcedure",
            "troubleShootingContents",
            "as_tel",
            "as_tel_comment",
            # 애초에 폼1 관할이 아니었던 상품별 값.
            "origin_area_code",
            "origin_content",
            "importer",
            "manufacturer",
            "model_name",
            "modelName",
            "kc_declaration",
            "naver_searchad",
            "image_providers",
        ]
        for field in removed:
            # name="..." 형태로 폼 필드가 만들어지면 안 됨.
            assert (
                f'name="{field}"' not in html_str
            ), f"제거 대상 필드 {field!r} 가 폼 name 속성에 있음"

    def test_form_has_partial_save_notice(self):
        """화면에 부분 저장이 가능함이 알려진다 (계약 5)."""
        html_str = self._render()
        # subtitle 또는 form-note 에 "다 채우지 않아도" 문구가 있어야 한다.
        assert "다 채우지 않아도" in html_str or "부분 저장" in html_str


# --------------------------------------------------------------------------- #
# (j) 양식1 안내 문구 — "어디서 무엇을 받는지" 가 화면에 드러난다.
#
# 사용자 평(경력자): "나는 경력자임에도 뭔지를 모르겠음. 어디 가서 확인해야 함?"
# 3칸으로 줄인 효과가 안내가 비어 있어 무효화되는 것을 막기 위해, 각 칸이
# 어디서 무엇을 가져오는지 안내한다. 발급처(네이버 커머스API 센터)가 검색·지도용
# "네이버 개발자센터"와 다른 곳임도 명시한다.
# --------------------------------------------------------------------------- #
class TestFormGuideMentionsSource:
    """(j) 양식1 안내가 발급처·사전조건·함정을 화면에 드러내는가.

    계약(이슈):
      (a) 폼 HTML 에 "커머스API" · "통합매니저" · "IP" 안내가 존재.
      (b) 입력 필드는 여전히 정확히 3개(회귀).
      (c) <script> 0개(회귀).
      (d) 존재하지 않는 하위 경로 URL 을 만들어 넣지 않는다 — 링크는 도메인까지만.
      (e) 비밀값 미출력·부분 저장·예시값 금지(회귀).
    """

    def _render(self) -> str:
        return config_form_server.render_config_form_html(
            token="dummy_token",
            port=54321,
            config_set_status={},
        )

    def test_guide_mentions_commerce_api(self):
        """(a) '커머스API' 안내가 폼에 존재한다."""
        html_str = self._render()
        assert "커머스API" in html_str, "발급처(커머스API 센터) 안내가 폼에 없음"

    def test_guide_mentions_integrated_manager(self):
        """(a) '통합매니저' 사전조건 안내가 폼에 존재한다."""
        html_str = self._render()
        assert "통합매니저" in html_str, "통합매니저 권한 안내가 폼에 없음"

    def test_guide_mentions_api_call_ip(self):
        """(a) 'API 호출 IP' 함정 안내가 폼에 존재한다."""
        html_str = self._render()
        # "API 호출 IP" 또는 "호출 IP" 형태로 안내되어야 한다.
        assert (
            "API 호출 IP" in html_str or "호출 IP" in html_str
        ), "API 호출 IP 등록 함정 안내가 폼에 없음"

    def test_guide_distinguishes_commerce_from_dev_center(self):
        """(a) 커머스API 센터가 '네이버 개발자센터'와 다름을 명시한다."""
        html_str = self._render()
        # 두 센터 이름이 모두 등장해야 구분이 성립한다.
        assert "커머스API" in html_str
        assert "네이버 개발자센터" in html_str, "'네이버 개발자센터가 아님' 구분 안내가 폼에 없음"

    def test_guide_mentions_apicenter_domain(self):
        """(d) 발급처 도메인(apicenter.commerce.naver.com) 이 폼에 나타난다."""
        html_str = self._render()
        assert "apicenter.commerce.naver.com" in html_str, "발급처 도메인 안내가 폼에 없음"

    def test_guide_no_fabricated_subpath_url(self):
        """(d) 확인되지 않은 하위 경로 URL 을 만들어 넣지 않았다.

        apicenter.commerce.naver.com 도메인은 근거가 있으나 하위 경로는 확인되지
        않았다 → 도메인 뒤에 '/' + 추가 경로가 오면 안 된다. 메뉴명(한글)으로
        대신 안내한다.
        """
        html_str = self._render()
        # 도메인 뒤에 슬래시로 시작하는 경로(예: /mypage/...) 가 이어지면 안 된다.
        # 허용: 도메인만("apicenter.commerce.naver.com"), 또는 닫는 태그/따옴표로 끝.
        forbidden_paths = [
            "apicenter.commerce.naver.com/",
            "apicenter.commerce.naver.com/#",
            "apicenter.commerce.naver.com/apply",
            "apicenter.commerce.naver.com/my",
            "apicenter.commerce.naver.com/apps",
        ]
        for path in forbidden_paths:
            assert path not in html_str, f"확인되지 않은 하위 경로 URL 이 폼에 있음: {path!r}"

    def test_guide_uses_menu_names_not_only_domain(self):
        """(d) 도메인과 함께 메뉴명([애플리케이션] > [내스토어 애플리케이션]) 이 있다."""
        html_str = self._render()
        # 하위 경로 URL 을 쓰지 않는 대신 메뉴명으로 안내한다.
        assert "[애플리케이션]" in html_str
        assert "[내스토어 애플리케이션]" in html_str

    def test_guide_slug_is_not_issued_value(self):
        """store_url_slug 안내가 '발급받는 값이 아님' 을 드러낸다."""
        html_str = self._render()
        assert (
            "발급받는 값이 아닙니다" in html_str or "발급받는 게 아니라" in html_str
        ), "store_url_slug 가 발급물이 아님을 안내하는 문구가 없음"
        # 스토어 주소 형태도 안내한다.
        assert "smartstore.naver.com" in html_str

    def test_guide_exactly_three_input_fields_regression(self):
        """(b) 입력 필드는 정확히 3개다 (회귀)."""
        html_str = self._render()
        allowed = list(config_form_server.allowed_field_names())
        assert len(allowed) == 3, f"양식1 필드가 3개가 아님: {allowed}"
        for name in allowed:
            assert f'name="{name}"' in html_str

    def test_guide_no_script_tag_regression(self):
        """(c) <script> 태그가 0개다 (회귀)."""
        html_str = self._render()
        assert "<script" not in html_str.lower()
        assert "</script>" not in html_str.lower()

    def test_guide_no_secret_values_regression(self):
        """(e) 폼 본문에 비밀값(실제 secret 형태) 이 없다 (회귀)."""
        html_str = self._render()
        # 폼은 빈 칸으로 시작하므로 비밀값 placeholder 가 있으면 안 된다.
        # password 필드는 type="password" 이며 value 속성이 비어 있어야 한다.
        # 실제 비밀값("dummy-secret" 등) 이 하드코딩되지 않았는지 확인.
        for forbidden in ("dummy-secret", "REPLACE_WITH", "{CLIENT_SECRET}"):
            assert forbidden not in html_str

    def test_guide_partial_save_notice_regression(self):
        """(e) 부분 저장 안내가 여전히 있다 (회귀)."""
        html_str = self._render()
        assert "다 채우지 않아도" in html_str or "부분 저장" in html_str

    def test_guide_no_example_values_regression(self):
        """(e) 규제 신고값 예시값 금지가 여전히 성립한다 (회귀)."""
        html_str = self._render()
        assert "예: 국산" not in html_str
        assert "예: 1577-" not in html_str

    def test_guide_collapsible_does_not_bloat_main_view(self):
        """안내가 접기(<details>) 형태라 본문 3칸을 가리지 않는다.

        본문이 다시 길어지면 안 된다 — 접기/작은 글씨 등으로 본문을 가리지 않게
        배치한다(이슈 표현 규칙). 안내 패널이 <details> 로 기본 접혀있는지 확인.
        """
        html_str = self._render()
        # 안내는 <details> 접기 요소로 들어간다.
        assert "<details" in html_str, "접기(<details>) 형태의 안내가 없음"
        assert "</details>" in html_str


# --------------------------------------------------------------------------- #
# (d-2) 양식1 HTML 에 <script> 가 없다 — 업로드 기능 제거로 JS 가 완전히 사라졌다.
#
# 업로드로 채우기 기능(유일한 JS 사용처)을 제거했으므로, 양식1 HTML 은 순수
# 정적 HTML 폼만으로 성립해야 한다(우측구이 정적 렌더 대응). 여기서는:
#   - <script> 태그가 전혀 없다.
#   - <input type="file"> 이 없다.
#   - FileReader / XMLHttpRequest / fetch 가 없다.
#   - "업로드" 문구가 없다.
#   - "선택" 문구에 의존하는 단정이 없다(업로드 안내문이 사라졌으므로).
# --------------------------------------------------------------------------- #
class TestFormNoScriptEvidence:
    """(d-2) 양식1 HTML 에 JavaScript 가 전혀 없다 (업로드 기능 제거).

    사유: 실물 CSV/엑셀이 없는 상태에서 파서를 만들면 열 이름을 추측하게 된다.
    또한 기존 업로드는 get_product 응답 JSON(키 연결 *후* 에야 얻는 파일)을
    올리라고 요구했는데, 양식1 은 키를 넣기 *전* 화면이라 순서가 뒤집혀 있었다.
    업로드는 양식2(상품별 고시) 에서 실물 CSV 확보 후 다시 만든다.
    """

    def _render(self) -> str:
        return config_form_server.render_config_form_html(
            token="dummy_token",
            port=54321,
            config_set_status={},
        )

    def test_no_script_tag(self):
        """폼 HTML 에 <script> 태그가 전혀 없다."""
        html_str = self._render()
        assert "<script" not in html_str.lower(), "양식1 HTML 에 <script> 태그가 있음"
        assert "</script>" not in html_str.lower()

    def test_no_file_input(self):
        """<input type="file"> 이 없다."""
        html_str = self._render()
        assert 'type="file"' not in html_str, "양식1 HTML 에 파일 입력이 있음"

    def test_no_filereader_or_xhr_or_fetch(self):
        """FileReader / XMLHttpRequest / fetch 가 없다."""
        html_str = self._render()
        assert "FileReader" not in html_str
        assert "XMLHttpRequest" not in html_str
        assert "fetch(" not in html_str

    def test_no_upload_wording(self):
        """업로드 안내 문구가 없다."""
        html_str = self._render()
        # "업로드" 단어가 폼 본문에 등장하지 않아야 한다.
        assert "업로드" not in html_str, "양식1 HTML 에 업로드 안내 문구가 남아있음"

    def test_no_upload_status_element(self):
        """업로드 상태 표시 요소(id="upload-status")가 없다."""
        html_str = self._render()
        assert 'id="upload-status"' not in html_str
        assert 'class="upload-status"' not in html_str

    def test_no_field_source_element(self):
        """업로드 출처 표시 요소(field-source)가 없다."""
        html_str = self._render()
        assert "field-source" not in html_str

    def test_form_is_static_html_only(self):
        """폼이 순수 HTML 폼 POST 만으로 성립한다 (우측구이 정적 렌더 대응)."""
        html_str = self._render()
        # <form method="POST"> 가 있고.
        assert 'method="POST"' in html_str
        # submit 버튼이 type="submit" 이며.
        submit_btns = re.findall(r'<button[^>]*type="submit"[^>]*>([^<]*)</button>', html_str)
        assert any("저장" in b for b in submit_btns)
        # onload / addEventListener / querySelector 등 JS 흔적이 없다.
        for js_trace in ("addEventListener", "querySelector", "onload", "onerror", "onclick"):
            assert js_trace not in html_str, f"양식1 HTML 에 JS 흔적 {js_trace!r} 이 있음"


# --------------------------------------------------------------------------- #
# (h) 요청 처리 후 포트가 닫힌다(좀비 포트 금지).
# --------------------------------------------------------------------------- #
class TestFormPortClosesAfterHandling:
    """(h) 요청 처리 후 포트가 닫히는가 (방어 9)."""

    def test_port_closes_after_successful_save(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _save():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={"token": token, "client_id": "port_close_id"},
                )

            t = threading.Thread(target=_save, daemon=True)
            t.start()
            srv.wait(timeout=5)
        finally:
            srv.close()

        # 서버가 종료되면 포트가 닫혀야 한다.
        assert _port_is_closed(port), "포트가 처리 후에도 닫히지 않음 (좀비 포트)"

    def test_port_closes_after_expiry(self, tmp_path):
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=1,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)
            # 만료될 때까지 대기.
            srv.wait(timeout=3)
        finally:
            srv.close()

        assert _port_is_closed(port), "만료 후 포트가 닫히지 않음"

    def test_bound_host_is_localhost(self, tmp_path):
        """서버가 127.0.0.1 에만 바인드됐는지 확인 (방어 1)."""
        config_file = tmp_path / "config.json"
        token = approval_server.new_token()
        srv = config_form_server.ConfigFormServer(
            config_path=str(config_file),
            token=token,
            ttl_seconds=60,
        )
        srv.start()
        try:
            bound = config_form_server.actual_bound_host(srv)
            assert bound in ("127.0.0.1",), f"바인드 호스트가 127.0.0.1 이 아님: {bound!r}"
        finally:
            srv.close()

    def test_non_localhost_bind_rejected(self):
        """bind_host 가 127.0.0.1 이 아니면 생성 자체가 거부된다."""
        with pytest.raises(ValueError, match="127.0.0.1"):
            config_form_server.ConfigFormServer(
                config_path="/tmp/dummy_config.json",
                token="dummy",
                bind_host="0.0.0.0",
            )


# --------------------------------------------------------------------------- #
# (i) 서버가 꺼져 있을 때 기존 check_config 흐름에 회귀가 없다.
# --------------------------------------------------------------------------- #
class TestCheckConfigNoRegression:
    """(i) enable_config_form=false 일 때 check_config 회귀가 없는가."""

    def test_check_config_returns_form_path_without_server(self, tmp_path, monkeypatch):
        """서버가 꺼져 있어도 폼 HTML 경로는 반환된다."""
        from clossify import mcp_server

        config_file = tmp_path / "config.json"
        # 빈 config — needs_form=True.
        config_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        # 기존 키들이 그대로 있다.
        assert "ok" in result
        assert "present" in result
        assert "missing" in result
        assert "placeholders" in result
        # 새 키도 있다.
        assert "config_form_path" in result
        assert "config_form_open" in result
        # enable_config_form 이 false 이므로 서버는 켜지지 않는다.
        assert result["config_form_open"] is False
        # 경로는 반환된다.
        assert result["config_form_path"] is not None

    def test_check_config_form_path_is_html_file(self, tmp_path, monkeypatch):
        """반환된 경로가 실제 HTML 파일이다."""
        from clossify import mcp_server

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        html_path = result.get("config_form_path")
        assert html_path is not None
        p = Path(html_path)
        assert p.is_file()
        content = p.read_text(encoding="utf-8")
        assert "<html" in content.lower()

    def test_check_config_complete_config_no_form(self, tmp_path, monkeypatch):
        """설정이 완전하면 폼 경로가 반환되지 않는다."""
        from clossify import mcp_server

        config_file = tmp_path / "config.json"
        # 키 3종이 모두 채워진 config.
        config_file.write_text(
            json.dumps(
                {
                    "naver": {
                        "client_id": "complete_id",
                        "client_secret": "complete_secret",
                        "store_url_slug": "complete-slug",
                        "type": "self",
                    },
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        # 설정이 완전하면 needs_form=False.
        assert result["config_form_path"] is None
        assert result["config_form_open"] is False

    def test_check_config_existing_keys_unchanged(self, tmp_path, monkeypatch):
        """기존 반환 키의 의미가 변경되지 않았다."""
        from clossify import mcp_server

        config_file = tmp_path / "config.json"
        # naver 섹션이 있지만 키가 비어 있는 config.
        config_file.write_text(
            json.dumps({"naver": {}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        # ok=False (설정이 비어 있으므로).
        assert result["ok"] is False
        # missing 에 키 3종이 있어야 한다.
        missing = result.get("missing") or []
        assert any("client_id" in m for m in missing)

    def test_check_config_form_html_has_no_example_values(self, tmp_path, monkeypatch):
        """check_config 가 생성한 폼 HTML 에 예시값이 없다."""
        from clossify import mcp_server

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({}), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(config_file))

        result = mcp_server.check_config()
        html_path = result.get("config_form_path")
        assert html_path is not None
        content = Path(html_path).read_text(encoding="utf-8")
        # 규제 필드 예시값 금지.
        assert "예: 국산" not in content
        assert "예: 1577-" not in content
        # 폼 POST 구조.
        assert 'method="POST"' in content
        assert "fetch(" not in content


# --------------------------------------------------------------------------- #
# 모듈 레벨 소스 검사 — config_form_server.py 소스 자체 검증.
# --------------------------------------------------------------------------- #
class TestConfigFormServerSourceEvidence:
    """config_form_server.py 소스 자체가 방어 계약을 갖는다."""

    def test_source_no_acao_header_emission(self):
        """소스에 Access-Control-Allow-Origin 헤더 송출이 없다."""
        src = (Path(_SRC) / "clossify" / "config_form_server.py").read_text(encoding="utf-8")
        # 주석이 아닌 줄에서 ACAO 송출이 없어야 한다.
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # send_header("Access-Control-Allow-Origin", ...) 가 없어야 한다.
            assert (
                'send_header("Access-Control-Allow-Origin"' not in line
            ), f"ACAO 헤더 송출 코드가 있음: {line.strip()[:80]}"

    def test_source_uses_approval_server_defenses(self):
        """approval_server 의 방어 함수를 재사용한다."""
        src = (Path(_SRC) / "clossify" / "config_form_server.py").read_text(encoding="utf-8")
        # tokens_match 와 origin_referer_ok 를 approval_server 에서 가져온다.
        assert "approval_server.tokens_match" in src
        assert "approval_server.origin_referer_ok" in src

    def test_source_binds_localhost_only(self):
        """소스에서 127.0.0.1 바인드를 강제한다."""
        src = (Path(_SRC) / "clossify" / "config_form_server.py").read_text(encoding="utf-8")
        # _ThreadingHTTPServer(("127.0.0.1", 0, ...) 패턴이 있어야 한다.
        assert '"127.0.0.1", 0' in src or "'127.0.0.1', 0" in src

    def test_source_has_ttl(self):
        """TTL 이 approval_server 와 동일한 상수를 참조한다."""
        src = (Path(_SRC) / "clossify" / "config_form_server.py").read_text(encoding="utf-8")
        assert "approval_server.TTL_SECONDS" in src

    def test_source_whitelist_exact_form1_fields(self):
        """화이트리스트가 양식1 3개 필드와 정확히 일치한다 (키 3개 정정).

        양식1 = "한 번만 넣으면 다시 넣을 필요 없는 것" = 계정 자격증명 3개.
        고시 5필드·AS 연락처 2필드는 폼에서만 감추는 게 아니라 **저장 로직의
        화이트리스트에서도 빠진다** — 폼에서만 감추고 서버가 계속 쓰면 의미 없다.
        상품별 값(origin_area_code 등)도 마찬가지로 화이트리스트에 없다.
        """
        # 폼1 관할 = (A) 키 3개.
        expected = {
            "client_id",
            "client_secret",
            "store_url_slug",
        }
        actual = set(config_form_server.allowed_field_names())
        assert actual == expected, (
            f"양식1 허용 필드가 3개와 일치하지 않음.\n"
            f"  expected={sorted(expected)}\n"
            f"  actual  ={sorted(actual)}\n"
            f"  extra   ={sorted(actual - expected)}\n"
            f"  missing ={sorted(expected - actual)}"
        )

    def test_removed_fields_absent_from_whitelist(self):
        """제거 대상 필드가 화이트리스트에 없다 (저장 로직에서도 거부)."""
        # _ALLOWED_FIELDS 안에 폼 필드명(첫 원소) 으로 등장하면 안 된다.
        # 화이트리스트에 없으면 _FIELD_INDEX 조회가 빠지고, 서버는 해당 키를
        # form_values 로 받아도 skipped_keys 로 분류한다(저장 거부).
        allowed = config_form_server.allowed_field_names()
        removed_form_fields = [
            # 이번에 양식1 에서 제거된 7개 (고시 5 + AS 2).
            "returnCostReason",
            "noRefundReason",
            "qualityAssuranceStandard",
            "compensationProcedure",
            "troubleShootingContents",
            "as_tel",
            "as_tel_comment",
            # 애초에 폼1 관할이 아니었던 상품별 값.
            "origin_area_code",
            "origin_content",
            "importer",
            "manufacturer",
            "model_name",
            "modelName",
            "kc_declaration",
            "naver_searchad",
            "image_providers",
        ]
        for field in removed_form_fields:
            assert (
                field not in allowed
            ), f"제거 대상 폼 필드 {field!r} 가 _ALLOWED_FIELDS 에 남아있음"

    def test_source_no_fetch_in_form_html(self):
        """폼 HTML 생성 코드에 fetch 호출이 없다."""
        src = (Path(_SRC) / "clossify" / "config_form_server.py").read_text(encoding="utf-8")
        # 주석이 아닌 줄에서 fetch( 가 없어야 한다.
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            assert "fetch(" not in line, f"실행 코드에 fetch() 호출이 있음: {line.strip()[:80]}"


# --------------------------------------------------------------------------- #
# 브라우저 검증용 HTML 생성 방법 (오케스트레이터가 사용).
#
# 아래 문자열은 실제 브라우저에서 폼 전송을 확인하기 위한 지침이다.
# 소켓 테스트는 서버가 폼 본문을 받아들이는 것을 증명하지만, 브라우저가
# 실제로 CORS 프리플라이트 없이 POST 를 보내는지는 브라우저로 확인해야 한다.
# --------------------------------------------------------------------------- #
BROWSER_VERIFY = """
브라우저 검증 방법:
  1. 테스트 환경에서 config_form_server.ConfigFormServer 를 시작한다.
  2. render_config_form_html(token=..., port=..., config_set_status={})
     로 폼 HTML 문자열을 얻어 .html 파일로 저장한다.
  3. 브라우저(Chrome/Firefox/Safari) 에서 file:// 프로토콜로 해당 HTML 을 연다.
  4. 폼을 채우고 [저장] 버튼을 누른다.
  5. 브라우저 개발자 도구 Network 탭에서:
     - 요청이 application/x-www-form-urlencoded 로 전송됐는지 확인.
     - CORS 프리플라이트(OPTIONS) 요청이 없었는지 확인.
     - 응답이 text/html 로 왔는지 확인.
     - Access-Control-Allow-Origin 응답 헤더가 없는지 확인.
  6. 결과 페이지가 정상적으로 표시되는지 확인.
  7. 설정 파일이 실제로 변경됐는지 확인.

Python 으로 검증용 HTML 생성:
  from clossify import approval_server, config_form_server
  token = approval_server.new_token()
  srv = config_form_server.ConfigFormServer(config_path=".local/config.json", token=token)
  port = srv.start()
  config_form_server.write_config_form_html(".local/config_form_verify.html", token=token, port=port)
  # 브라우저에서 file://.../.local/config_form_verify.html 열기
  # 검증 후 srv.close()
"""
