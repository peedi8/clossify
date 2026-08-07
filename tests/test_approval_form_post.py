# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""승인 다리 "브라우저 폼 POST" 경로 테스트.

**왜 별도 파일인가**: 기존 ``test_approval_server.py`` 는 전부 ``application/json``
소켓 호출이다 — 서버가 완벽히 검증됐지만 **브라우저가 서버에 닿는지는 한 번도
확인되지 않았다** (이것이 본 결함의 원인이었다). 본 파일은 실제 브라우저가 보내는
형태(``application/x-www-form-urlencoded``, 커스텀 헤더 없음)를 **실제 소켓**으로
재현해, 서버가 폼 본문을 받아들이고 **사람이 읽을 HTML 결과**를 반환하는지를 검증한다.

테스트 목록(티켓 a~j 대응, 폼 경로 중심):
  (a) 폼 POST(커스텀 헤더 없음) + 유효 토큰 + product_key -> 승인 처리, HTML 반환.
  (b) 토큰 없음/틀림 -> 거부 + 사람이 읽을 HTML 로 사유 표시.
  (c) product_key 누락/불일치 -> 거부(방어 7 회귀 없음).
  (d) 같은 토큰 재사용 -> 거부(1회 소진 유지).
  (e) 만료 후 -> 거부, 서버 종료.
  (f) Origin: https://evil.example(단일/중복) -> 거부.
  (g) 응답에 Access-Control-Allow-Origin 없음(전 경로).
  (h) 기존 JSON 경로 회귀 없음.
  (i) 생성된 미리보기 HTML 에 커스텀 헤더를 쓰는 fetch 승인 경로가 남아 있지 않음.
  (j) 미리보기에 "결과를 모르면서 보냈다고 단정하는 문구"가 없음.

**주의**: 본 파일은 소켓 테스트로 **서버가 폼 본문을 받아들이는지**를 증명한다.
실제 브라우저에서 폼이 전송되는지(CORS 프리플라이트 회피)는 오케스트레이터가
브라우저로 확인한다 — 워커는 아래 검증용 HTML 생성 스크립트를 제공한다
(하단 ``BROWSER_VERIFY`` 문자열).
"""

from __future__ import annotations

import http.client
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

from clossify import approval_server, naver_client, preview


# --------------------------------------------------------------------------- #
# 실제 폼 POST 요청을 보내는 헬퍼 (http.client 로 커스텀 헤더 없이).
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
    """``application/x-www-form-urlencoded`` 폼 본문을 보내고 (status, body_text, headers).

    커스텀 헤더는 기본으로 일절 보내지 않는다 — 실제 브라우저 폼 제출을 재현.
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
        headers.append(("X-Approval-Token", token_header))
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


# --------------------------------------------------------------------------- #
# (a) 폼 POST + 유효 토큰 + product_key -> 승인 처리, HTML 반환.
# --------------------------------------------------------------------------- #
class TestFormPostApproves:
    """(a) 브라우저 폼 POST 경로가 승인을 처리하고 HTML 을 반환하는가."""

    def test_form_post_approves_and_returns_html(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="form0abc01", token=token)
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _approve():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={"token": token, "product_key": "form0abc01"},
                )

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
        finally:
            srv.close()

    def test_form_post_response_is_html(self):
        """성공 응답이 HTML 이다(text/html)."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="html0abc01", token=token)
        port = srv.start()
        try:
            status, body, headers = _send_form(
                port,
                fields={"token": token, "product_key": "html0abc01"},
            )
            assert status == 200
            ctype = [v for k, v in headers if k.lower() == "content-type"]
            assert any(
                "text/html" in v.lower() for v in ctype
            ), f"성공 응답 Content-Type 이 text/html 이 아님: {ctype}"
            # HTML 이며 승인 접수 사실을 사람이 읽을 수 있어야 한다.
            assert "<html" in body.lower()
            assert "승인이 접수" in body or "접수" in body
        finally:
            srv.close()

    def test_form_post_no_custom_headers_used(self):
        """커스텀 헤더 없이도 승인이 처리된다(프리플라이트 회피 조건)."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="nocus0abc1", token=token)
        port = srv.start()
        try:
            # 오직 Content-Type + Content-Length 만. X-Approval-Token 없음.
            status, body, _ = _send_form(
                port,
                fields={"token": token, "product_key": "nocus0abc1"},
            )
            assert status == 200
            assert "접수" in body
            assert srv.outcome is not None
            assert srv.outcome.approved is True
        finally:
            srv.close()

    def test_form_post_edits_carried_to_outcome(self):
        """edits[<field>] 폼 키가 outcome.decisions.edits 로 펼쳐진다."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="edit0abc01", token=token)
        port = srv.start()
        try:
            status, _, _ = _send_form(
                port,
                fields=[
                    ("token", token),
                    ("product_key", "edit0abc01"),
                    ("edits[상품명]", "새 이름"),
                    ("edits[판매가]", "12000"),
                ],
            )
            assert status == 200
            outcome = srv.outcome
            assert outcome is not None
            assert outcome.approved is True
            edits = outcome.decisions.get("edits") or {}
            assert edits.get("상품명") == "새 이름"
            assert edits.get("판매가") == "12000"
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (b) 토큰 없음/틀림 -> 거부 + 사람이 읽을 HTML 로 사유.
# --------------------------------------------------------------------------- #
class TestFormTokenRejectionShowsReason:
    """(b) 폼 경로 토큰 거부가 사유를 HTML 로 표시하는가."""

    def test_form_no_token_rejected_with_html_reason(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="notok0ab01", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            status, body, headers = _send_form(
                port,
                fields={"product_key": "notok0ab01"},  # token 누락
            )
            assert status in (401, 403)
            ctype = [v for k, v in headers if k.lower() == "content-type"]
            assert any("text/html" in v.lower() for v in ctype)
            # 거부 사유가 사람이 읽을 수 있게 HTML 안에 있어야 한다.
            assert "거부" in body or "토큰" in body
            assert srv.outcome is None
        finally:
            srv.close()

    def test_form_wrong_token_rejected_with_html_reason(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="wrtok0ab01", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": "totally-wrong-token", "product_key": "wrtok0ab01"},
            )
            assert status == 403
            assert "<html" in body.lower()
            assert "거부" in body or "토큰" in body
            assert srv.outcome is None
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (c) product_key 누락/불일치 -> 거부(방어 7 회귀 없음).
# --------------------------------------------------------------------------- #
class TestFormProductKeyGuard:
    """(c) 폼 경로 product_key 검사가 유지되는가."""

    def test_form_missing_product_key_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="mpk0abc001", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            status, body, _ = _send_form(port, fields={"token": token})
            assert status == 400
            assert "<html" in body.lower()
            assert "product_key" in body
            assert srv.outcome is None
        finally:
            srv.close()

    def test_form_wrong_product_key_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="correct0ab1", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": token, "product_key": "different0a"},
            )
            assert status == 403
            assert "<html" in body.lower()
            assert "상품" in body or "product" in body.lower()
            assert srv.outcome is None
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (d) 같은 토큰 재사용 -> 거부(1회 소진 유지).
# --------------------------------------------------------------------------- #
class TestFormSingleUseToken:
    """(d) 폼 경로 1회 소진이 유지되는가."""

    def test_form_token_consumed_after_first_use(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="sng0abc001", token=token)
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _approve():
                time.sleep(0.1)
                _send_form(port, fields={"token": token, "product_key": "sng0abc001"})

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
            assert srv.is_consumed() is True
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (e) 만료 후 -> 거부, 서버 종료.
# --------------------------------------------------------------------------- #
class TestFormExpiry:
    """(e) 폼 경로 만료 검사."""

    def test_form_expired_rejected_with_html(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="exp0abc001", token=token, ttl_seconds=1)
        port = srv.start()
        try:
            time.sleep(1.5)
            status, body, _ = _send_form(
                port,
                fields={"token": token, "product_key": "exp0abc001"},
            )
            assert status == 410
            assert "<html" in body.lower()
            assert "만료" in body or "expired" in body.lower()
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (f) Origin: https://evil.example (단일/중복) -> 거부.
# --------------------------------------------------------------------------- #
class TestFormOriginGuard:
    """(f) 폼 경로 Origin 검사가 유지되는가."""

    def test_form_evil_origin_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="org0abc001", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            status, body, _ = _send_form(
                port,
                fields={"token": token, "product_key": "org0abc001"},
                origin="https://evil.example",
            )
            assert status == 403
            assert "<html" in body.lower()
            assert "Origin" in body or "origin" in body.lower()
            assert srv.outcome is None
        finally:
            srv.close()

    def test_form_null_origin_allowed(self):
        """file:// 폼 POST 가 보내는 Origin: null 은 허용."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="null0ab001", token=token)
        port = srv.start()
        try:

            def _approve():
                time.sleep(0.1)
                _send_form(
                    port,
                    fields={"token": token, "product_key": "null0ab001"},
                    origin="null",
                )

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
        finally:
            srv.close()

    def test_form_duplicate_origin_second_evil_rejected(self):
        """Origin: null + Origin: https://evil -> 거부 (get_all 회귀 없음)."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="duporg001a", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            status, body = _send_raw_form(
                port,
                raw_headers=[
                    ("Origin", "null"),
                    ("Origin", "https://evil.example"),
                ],
                fields={"token": token, "product_key": "duporg001a"},
            )
            assert status == 403
            assert "Origin" in body or "origin" in body.lower()
            assert srv.outcome is None
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (g) 응답에 Access-Control-Allow-Origin 없음(전 경로).
# --------------------------------------------------------------------------- #
class TestFormNoCorsHeader:
    """(g) 폼 경로 응답에 ACAO 가 없는가 (방어 6)."""

    def test_form_success_has_no_acao(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="naca0ab001", token=token)
        port = srv.start()
        try:
            _, _, headers = _send_form(
                port,
                fields={"token": token, "product_key": "naca0ab001"},
            )
            acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao == [], f"폼 성공 응답에 ACAO 가 있음: {acao}"
        finally:
            srv.close()

    def test_form_rejection_has_no_acao(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="nacar0ab01", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            _, _, headers = _send_form(port, fields={"product_key": "nacar0ab01"})
            acao = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao == []
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (h) 기존 JSON 경로 회귀 없음 — JSON 호출이 여전히 JSON 응답을 받는다.
# --------------------------------------------------------------------------- #
class TestJsonPathNoRegression:
    """(h) JSON 경로 회귀가 없는가."""

    def test_json_path_still_returns_json(self):
        import json as _json

        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="jsn0abc001", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = _json.dumps({"token": token, "product_key": "jsn0abc001"}).encode()
            conn.request(
                "POST",
                "/",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8")
            ctype = resp.getheader("Content-Type") or ""
            conn.close()
            assert "application/json" in ctype.lower(), f"JSON 경로 응답이 JSON 이 아님: {ctype}"
            parsed = _json.loads(resp_body)
            assert parsed.get("ok") is True
            assert parsed.get("approved") is True
        finally:
            srv.close()

    def test_json_path_wrong_token_returns_json(self):
        import json as _json

        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="jsnr0ab01a", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = _json.dumps({"token": "wrong", "product_key": "jsnr0ab01a"}).encode()
            conn.request(
                "POST",
                "/",
                body=body,
                headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8")
            ctype = resp.getheader("Content-Type") or ""
            conn.close()
            assert "application/json" in ctype.lower()
            parsed = _json.loads(resp_body)
            assert parsed.get("approved") is not True
        finally:
            srv.close()

    def test_unknown_content_type_rejected(self):
        """지원하지 않는 Content-Type 은 거부된다 (415)."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(product_key="uct0abc001a", token=token, ttl_seconds=60)
        port = srv.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            body = b"raw bytes"
            conn.request(
                "POST",
                "/",
                body=body,
                headers={"Content-Type": "text/plain", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            status = resp.status
            resp.read()
            conn.close()
            assert status == 415
        finally:
            srv.close()


# --------------------------------------------------------------------------- #
# (i) 미리보기 HTML 에 커스텀 헤더를 쓰는 fetch 승인 경로가 남아 있지 않음.
# (j) 미리보기에 "결과를 모르면서 보냈다고 단정하는 문구"가 없음.
# --------------------------------------------------------------------------- #
class TestPreviewNoFetchApprovalPath:
    """(i) (j) preview.py 렌더 결과가 새 폼 POST 만 쓰는가."""

    @pytest.fixture
    def notice_config_mock(self, monkeypatch):
        """render_preview_html 이 config 조회에 실패하지 않도록."""
        monkeypatch.setattr(naver_client, "_notice_config", lambda: {"origin_area_code": "04"})

    def _render_with_approval(self):
        payload = {
            "product": {
                "name": "폼전환상품",
                "salePrice": 39000,
                "categoryId": "50021299",
            },
            "images": {"listing_urls": []},
            "detail_html": "",
            "status": "SALE",
        }
        # **조작 모드 명시** — 승인 바/폼 POST 는 보기 전용 모드에서 나오지
        # 않는다(패널은 폼을 제출할 수 없기 때문). render_preview_html 의
        # 기본값이 보기 전용으로 바뀌었으므로, 본 승인 폼 회귀 검증 테스트는
        # 반드시 조작 모드를 명시해야 한다.
        return preview.render_preview_html(
            payload,
            product_key="previewkey1",
            approval_token="dummytoken",
            approval_port=54321,
            mode="interactive",
        )

    def test_no_fetch_approval_call(self, notice_config_mock):
        """승인 전송에 fetch(...) 가 사용되지 않는다."""
        html = self._render_with_approval()
        # fetch( ... ) 호출이 승인 스크립트에 없어야 한다.
        # (collectChanges/폼 채우기 JS 는 있어도 되지만, fetch 자체는 없어야)
        # 허용: fetch 가 미리보기 스크립트 어디에도 등장하면 안 된다.
        assert "fetch(" not in html, "미리보기에 fetch() 호출이 남아 있음"

    def test_no_custom_approval_header(self, notice_config_mock):
        """X-Approval-Token 헤더 사용이 승인 경로에 없다."""
        html = self._render_with_approval()
        # 커스텀 헤더 키 이름 자체가 폼 전송 코드에 없어야 한다.
        assert "X-Approval-Token" not in html, "커스텀 헤더 X-Approval-Token 이 잔존"
        assert 'headers:{"Content-Type":"application/json"' not in html.replace(
            " ", ""
        ), "application/json 헤더를 쓰는 fetch 경로가 잔존"

    def test_approval_form_present(self, notice_config_mock):
        """승인 바가 순수 HTML 폼 으로 렌더된다."""
        html = self._render_with_approval()
        assert '<form id="approval-form"' in html
        assert 'method="POST"' in html
        # enctype 이 명시되지 않았거나 폼 인코딩이어야 한다 (multipart/JSON 금지).
        assert 'enctype="multipart/form-data"' not in html
        # 토큰이 hidden 필드로 있다.
        assert 'name="token"' in html
        assert 'name="product_key"' in html

    def test_submit_buttons_are_real_submit(self, notice_config_mock):
        """[승인] / [수정 후 승인] 이 type=submit 이다 (JS 없이 전송)."""
        html = self._render_with_approval()
        # 두 버튼 모두 type="submit".
        submit_btns = re.findall(
            r'<button[^>]*type="submit"[^>]*>(승인|수정 후 승인)</button>', html
        )
        assert "승인" in submit_btns, "[승인] 버튼이 type=submit 이 아님"
        assert "수정 후 승인" in submit_btns, "[수정 후 승인] 버튼이 type=submit 이 아님"

    def test_no_false_success_phrase(self, notice_config_mock):
        """(j) "보냈습니다" 식의 거짓 성공 문구가 없다."""
        html = self._render_with_approval()
        # 이전 스크립트의 두 콜백이 모두 표시하던 거짓 성공 문구.
        assert (
            "승인 요청을 보냈습니다" not in html
        ), "결과를 모르면서 '보냈습니다' 라 단정하는 문구가 잔존"


# --------------------------------------------------------------------------- #
# 모듈 레벨 grep 증거 (스캐너용) — preview.py 소스 자체 검사.
# --------------------------------------------------------------------------- #
class TestPreviewSourceGrepEvidence:
    """preview.py 소스 자체가 더 이상 fetch 승인 경로를 갖지 않는다.

    의도: **실행 코드**가 fetch 를 쓰지 않는 것을 증명. 주석에 "이전 설계의
    결함" 을 설명하기 위해 ``fetch``/``X-Approval-Token`` 단어가 등장할 수 있으므로,
    본 테스트는 실행 가능한 JS 문자열 안의 패턴만 검사한다 — 즉 따옴표로 감싸진
    fetch 호출 또는 헤더 키 리터럴.
    """

    def test_preview_source_has_no_fetch_call_in_js(self):
        src = (Path(_SRC) / "clossify" / "preview.py").read_text(encoding="utf-8")
        # JS 안의 fetch( 호출 패턴 — 따옴표 안의 "fetch(" 리터럴이 없어야 한다.
        # (주석/문자열 외에 소스 어디에도 fetch( 가 없는 것이 가장 단순한 증거)
        # 주석은 # 로 시작하므로, # 가 아닌 줄에서 fetch( 가 없는지 확인.
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            assert "fetch(" not in line, f"실행 코드에 fetch() 호출이 있음: {line.strip()[:80]}"

    def test_preview_source_has_no_custom_header_in_js(self):
        src = (Path(_SRC) / "clossify" / "preview.py").read_text(encoding="utf-8")
        # JS 안의 헤더 키 리터럴 "X-Approval-Token" 이 없어야 한다 (따옴표/역따옴표).
        # 주석 안에서는 결함 설명을 위해 단어가 나올 수 있다.
        for line in src.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # 따옴표/역따옴표로 감싸진 헤더 키 리터럴만 금지.
            assert (
                '"X-Approval-Token"' not in line
            ), f"실행 코드에 X-Approval-Token 헤더 리터럴이 있음: {line.strip()[:80]}"
            assert (
                "'X-Approval-Token'" not in line
            ), f"실행 코드에 X-Approval-Token 헤더 리터럴이 있음: {line.strip()[:80]}"
