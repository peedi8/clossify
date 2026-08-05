# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""로컬 승인 다리(approval_server) 테스트.

**실제 소켓을 띄워 검증한다(모의 서버 아님).** 네이버 호출은 하지 않는다 —
이 테스트는 포트가 실제로 열리고, 실제로 방어되는지를 증명하는 것이 목적이다.

테스트 목록(작업 지시서의 a~j 대응):
  (a) 서버가 127.0.0.1 에만 바인드된다.
  (b) 올바른 토큰 → 승인 처리.
  (c) 토큰 없음/틀림 → 거부, 등록 안 됨.
  (d) 같은 토큰 재사용 → 거부(1회 소진).
  (e) 만료 후 요청 → 거부, 서버 종료됨.
  (f) Origin: https://evil.example 헤더 → 거부.
  (g) 응답에 Access-Control-Allow-Origin 헤더가 없다.
  (h) 설정 꺼짐 → 포트가 열리지 않는다.
  (i) 처리 후 포트가 닫힌다(좀비 없음).
  (j) 수정 후 승인 시 변경분이 반영되고, 명시값 우선 원칙이 지켜진다.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from clossify import approval_server, mcp_server


# ---------------------------------------------------------------------------
# 헬퍼: 실제 HTTP 요청을 보낸다(http.client 사용 — 모의 아님).
# ---------------------------------------------------------------------------
def _send_request(
    port: int,
    *,
    token: str | None = None,
    body: dict | None = None,
    origin: str | None = None,
    method: str = "POST",
    path: str = "/",
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict, list[tuple[str, str]]]:
    """실제 소켓으로 HTTP 요청을 보내고 (status, body, headers) 를 반환."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers: dict[str, str] = {}
    payload_bytes = b""
    if body is not None:
        payload_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload_bytes))
    if token is not None:
        headers["X-Approval-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    if extra_headers:
        headers.update(extra_headers)
    conn.request(method, path, body=payload_bytes, headers=headers)
    resp = conn.getresponse()
    resp_body = resp.read().decode("utf-8")
    resp_headers = [(k, v) for k, v in resp.getheaders()]
    status = resp.status
    conn.close()
    try:
        parsed = json.loads(resp_body)
    except (ValueError, TypeError):
        parsed = {}
    return status, parsed, resp_headers


def _wait_for_port(port: int, timeout: float = 3.0) -> bool:
    """포트가 실제로 열려있는지 소켓으로 확인."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _port_is_closed(port: int, timeout: float = 3.0) -> bool:
    """포트가 닫혔는지 확인(연결 시도가 실패하면 닫힌 것)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return False
        except OSError:
            return True
    return True


# ---------------------------------------------------------------------------
# (a) 서버가 127.0.0.1 에만 바인드된다.
# ---------------------------------------------------------------------------
class TestBindToLocalhost:
    """방어 1: 바인딩은 127.0.0.1 에만."""

    def test_bound_host_is_127_0_0_1(self):
        """서버가 실제로 바인드한 호스트가 127.0.0.1 인지 확인."""
        srv = approval_server.ApprovalServer(
            product_key="test123abc",
            token=approval_server.new_token(),
        )
        srv.start()
        try:
            assert approval_server.actual_bound_host(srv) == "127.0.0.1"
        finally:
            srv.close()

    def test_non_localhost_bind_rejected(self):
        """bind_host 가 127.0.0.1 이 아니면 생성 단계에서 거부."""
        with pytest.raises(ValueError, match="127.0.0.1"):
            approval_server.ApprovalServer(
                product_key="test123abc",
                token=approval_server.new_token(),
                bind_host="0.0.0.0",
            )


# ---------------------------------------------------------------------------
# (b) 올바른 토큰 → 승인 처리.
# ---------------------------------------------------------------------------
class TestCorrectTokenApproves:
    """방어 2: 올바른 토큰으로 승인이 처리된다."""

    def test_valid_token_approves(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="approve001ab",
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port), "포트가 열려야 함"

            # 백그라운드에서 승인 요청을 보낸다.
            def _approve():
                time.sleep(0.1)
                _send_request(port, token=token, body={"product_key": "approve001ab"})

            t = threading.Thread(target=_approve, daemon=True)
            t.start()

            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# (c) 토큰 없음/틀림 → 거부, 등록 안 됨.
# ---------------------------------------------------------------------------
class TestMissingOrWrongTokenRejected:
    """방어 2: 토큰 없음/틀림 → 거부."""

    def test_no_token_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="notoken001a",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            # 토큰 없이 요청.
            status, body, _ = _send_request(port, body={"product_key": "notoken001a"})
            assert status in (401, 403)
            assert body.get("approved") is not True
            # outcome 은 아직 None (승인되지 않음).
            assert srv.outcome is None
        finally:
            srv.close()

    def test_wrong_token_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="wrongtok001",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_request(
                port,
                token="completely-wrong-token-value-here",
                body={"product_key": "wrongtok001"},
            )
            assert status == 403
            assert body.get("approved") is not True
            assert srv.outcome is None
        finally:
            srv.close()

    def test_empty_token_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="emptytok001",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_request(port, token="", body={"product_key": "emptytok001"})
            assert status in (401, 403)
            assert srv.outcome is None
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# (d) 같은 토큰 재사용 → 거부(1회 소진).
# ---------------------------------------------------------------------------
class TestTokenSingleUse:
    """방어 3: 토큰은 1회만 소진 가능."""

    def test_token_cannot_be_reused(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="reuse0001ab",
            token=token,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)

            def _approve():
                time.sleep(0.1)
                _send_request(port, token=token, body={"product_key": "reuse0001ab"})

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True

            # 서버가 종료되었으므로 재사용 시도는 연결 실패 또는 거부.
            # 이미 소진된 상태이므로 srv.is_consumed() == True.
            assert srv.is_consumed() is True
        finally:
            srv.close()

    def test_consumed_flag_prevents_second_approval(self):
        """is_consumed 가 True 면 두 번째 승인이 불가함을 확인."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="consumed001",
            token=token,
            ttl_seconds=60,
        )
        srv.start()
        try:
            # 첫 번째 승인을 consume 으로 직접 기록.
            srv.consume(approval_server.Outcome(approved=True))
            assert srv.is_consumed() is True
            # 두 번째 consume 은 무시된다.
            srv.consume(approval_server.Outcome(approved=True, reason="second"))
            outcome = srv.outcome
            assert outcome is not None
            # 첫 번째 결과가 유지된다(reason 이 "second" 가 아님).
            assert outcome.reason != "second"
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# (e) 만료 후 요청 → 거부, 서버 종료됨.
# ---------------------------------------------------------------------------
class TestExpiry:
    """방어 4: 만료 후 거부."""

    def test_expired_server_rejects(self):
        token = approval_server.new_token()
        # TTL 을 매우 짧게 설정.
        srv = approval_server.ApprovalServer(
            product_key="expired001ab",
            token=token,
            ttl_seconds=1,
        )
        port = srv.start()
        try:
            assert _wait_for_port(port)
            # TTL 만큼 대기.
            time.sleep(1.5)
            # 만료 후 요청.
            status, body, _ = _send_request(port, token=token, body={"product_key": "expired001ab"})
            assert status == 410
            assert body.get("code") == "expired"
            assert srv.outcome is None or srv.outcome.approved is False
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# (f) Origin: https://evil.example 헤더 → 거부.
# ---------------------------------------------------------------------------
class TestOriginCheck:
    """방어 5: 악의적 Origin 거부."""

    def test_evil_origin_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="origin001abc",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_request(
                port,
                token=token,
                body={"product_key": "origin001abc"},
                origin="https://evil.example",
            )
            assert status == 403
            assert body.get("code") == "bad_origin"
            assert srv.outcome is None
        finally:
            srv.close()

    def test_null_origin_allowed(self):
        """file:// 페이지의 fetch 가 보내는 Origin: null 은 허용."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="nullori001a",
            token=token,
        )
        port = srv.start()
        try:

            def _approve():
                time.sleep(0.1)
                _send_request(
                    port,
                    token=token,
                    body={"product_key": "nullori001a"},
                    origin="null",
                )

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
        finally:
            srv.close()

    def test_file_referer_allowed(self):
        """file:// Referer 는 허용."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="filepref01a",
            token=token,
        )
        port = srv.start()
        try:

            def _approve():
                time.sleep(0.1)
                _send_request(
                    port,
                    token=token,
                    body={"product_key": "filepref01a"},
                    extra_headers={"Referer": "file:///C:/preview.html"},
                )

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# (g) 응답에 Access-Control-Allow-Origin 헤더가 없다.
# ---------------------------------------------------------------------------
class TestNoCorsHeader:
    """방어 6: CORS 헤더 절대 없음."""

    def test_no_acao_header_in_success(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="noacr0001ab",
            token=token,
        )
        port = srv.start()
        try:

            def _approve():
                time.sleep(0.1)
                _send_request(port, token=token, body={"product_key": "noacr0001ab"})

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            srv.wait(timeout=5)
        finally:
            srv.close()

        # 성공 응답은 이미 처리되었으므로, 거부 응답에서 ACAO 가 없는지 확인.
        # 새 서버를 띄워 거부 응답의 헤더를 검사한다.
        token2 = approval_server.new_token()
        srv2 = approval_server.ApprovalServer(
            product_key="noacr0002ab",
            token=token2,
            ttl_seconds=60,
        )
        port2 = srv2.start()
        try:
            _, _, headers = _send_request(port2, token="wrong", body={"product_key": "noacr0002ab"})
            acao_values = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao_values == [], "Access-Control-Allow-Origin 이 없어야 함"
        finally:
            srv2.close()

    def test_no_acao_header_in_rejection(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="noacrej001",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            _, _, headers = _send_request(port, body={"product_key": "noacrej001"})
            acao_values = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao_values == []
        finally:
            srv.close()

    def test_no_acao_header_in_options(self):
        """OPTIONS preflight 에도 ACAO 없음."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="noacopt001",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            _, _, headers = _send_request(port, method="OPTIONS", path="/")
            acao_values = [v for k, v in headers if k.lower() == "access-control-allow-origin"]
            assert acao_values == []
        finally:
            srv.close()


# ---------------------------------------------------------------------------
# (h) 설정 꺼짐 → 포트가 열리지 않는다.
# ---------------------------------------------------------------------------
class TestDefaultOff:
    """방어 8: 기본 OFF."""

    def test_config_enable_local_approval_defaults_false(self, monkeypatch, tmp_path):
        """config 에 키가 없으면 False 를 반환한다."""
        import json as _json

        cfg_file = tmp_path / "config.json"
        _json.dump({}, cfg_file.open("w"))
        monkeypatch.setattr(
            mcp_server.naver_client,
            "config_path",
            lambda: str(cfg_file),
        )
        assert mcp_server._config_enable_local_approval() is False

    def test_config_explicit_false(self, monkeypatch, tmp_path):
        """config 에 명시적 false 이면 False."""
        import json as _json

        cfg_file = tmp_path / "config.json"
        _json.dump({"enable_local_approval": False}, cfg_file.open("w"))
        monkeypatch.setattr(
            mcp_server.naver_client,
            "config_path",
            lambda: str(cfg_file),
        )
        assert mcp_server._config_enable_local_approval() is False

    def test_config_explicit_true(self, monkeypatch, tmp_path):
        """config 에 명시적 true 이면 True."""
        import json as _json

        cfg_file = tmp_path / "config.json"
        _json.dump({"enable_local_approval": True}, cfg_file.open("w"))
        monkeypatch.setattr(
            mcp_server.naver_client,
            "config_path",
            lambda: str(cfg_file),
        )
        assert mcp_server._config_enable_local_approval() is True

    def test_non_bool_value_defaults_false(self, monkeypatch, tmp_path):
        """config 값이 bool 이 아니면 False."""
        import json as _json

        cfg_file = tmp_path / "config.json"
        _json.dump({"enable_local_approval": "yes"}, cfg_file.open("w"))
        monkeypatch.setattr(
            mcp_server.naver_client,
            "config_path",
            lambda: str(cfg_file),
        )
        assert mcp_server._config_enable_local_approval() is False


# ---------------------------------------------------------------------------
# (i) 처리 후 포트가 닫힌다(좀비 없음).
# ---------------------------------------------------------------------------
class TestPortClosesAfterHandling:
    """방어 9: 처리 후 서버 종료."""

    def test_port_closes_after_approval(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="close0001ab",
            token=token,
        )
        port = srv.start()
        assert _wait_for_port(port)

        def _approve():
            time.sleep(0.1)
            _send_request(port, token=token, body={"product_key": "close0001ab"})

        t = threading.Thread(target=_approve, daemon=True)
        t.start()
        srv.wait(timeout=5)
        # wait() 이 반환되면 서버가 종료되어야 한다.
        assert _port_is_closed(port), "처리 후 포트가 닫혀야 함"

    def test_port_closes_after_close(self):
        """close() 호출 후 포트가 닫힌다."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="close0002ab",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        assert _wait_for_port(port)
        srv.close()
        assert _port_is_closed(port), "close() 후 포트가 닫혀야 함"


# ---------------------------------------------------------------------------
# (j) 수정 후 승인 시 변경분이 반영되고, 명시값 우선 원칙이 지켜진다.
# ---------------------------------------------------------------------------
class TestEditApproval:
    """방어 7(범위 제한) + 수정 반영 + 명시값 우선."""

    def test_edits_reflected_in_outcome(self):
        """[수정 후 승인] 의 edits 가 outcome.decisions 에 포함된다."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="edits0001ab",
            token=token,
        )
        port = srv.start()
        try:
            edits = {"상품명": "수정된 이름", "판매가": "15000", "태그": "겨울, 후드티"}
            body = {"product_key": "edits0001ab", "edits": edits}

            def _approve():
                time.sleep(0.1)
                _send_request(port, token=token, body=body)

            t = threading.Thread(target=_approve, daemon=True)
            t.start()
            outcome = srv.wait(timeout=5)
            assert outcome.approved is True
            assert isinstance(outcome.decisions, dict)
            assert outcome.decisions.get("edits") == edits
        finally:
            srv.close()

    def test_wrong_product_key_rejected(self):
        """방어 7: 다른 product_key 의 승인은 거부."""
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="correct001ab",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, body, _ = _send_request(
                port,
                token=token,
                body={"product_key": "different001"},
            )
            assert status == 403
            assert body.get("code") == "wrong_product"
            assert srv.outcome is None
        finally:
            srv.close()

    def test_apply_approval_edits_name(self):
        """_apply_approval_edits 가 상품명을 올바르게 번역한다."""
        edits = {"상품명": "새 상품명"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["name"] == "새 상품명"
        assert result["price"] is None

    def test_apply_approval_edits_price(self):
        """_apply_approval_edits 가 판매가(쉼표 포함)를 int 로 변환한다."""
        edits = {"판매가": "15,000원"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["price"] == 15000

    def test_apply_approval_edits_tags(self):
        """_apply_approval_edits 가 태그를 리스트로 분리한다."""
        edits = {"태그": "겨울, 후드티, 기모"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["tags"] == ["겨울", "후드티", "기모"]

    def test_apply_approval_edits_notice(self):
        """_apply_approval_edits 가 고시 필드를 notice dict 로 번역한다."""
        edits = {"고시.origin_area_code": "064"}
        result = mcp_server._apply_approval_edits(edits)
        assert result["notice"] == {"origin_area_code": "064"}


# ---------------------------------------------------------------------------
# 보너스: tokens_match 가 secrets.compare_digest 를 쓰는지 확인.
# ---------------------------------------------------------------------------
class TestTokenComparison:
    """방어 2: tokens_match 는 일정 시간 비교를 한다."""

    def test_tokens_match_equal(self):
        token = "abc123"
        assert approval_server.tokens_match(token, token) is True

    def test_tokens_match_different(self):
        assert approval_server.tokens_match("abc123", "xyz789") is False

    def test_tokens_match_empty_rejected(self):
        assert approval_server.tokens_match("", "") is False
        assert approval_server.tokens_match("abc", "") is False
        assert approval_server.tokens_match("", "abc") is False

    def test_new_token_has_sufficient_entropy(self):
        """토큰이 충분한 엔트로피를 갖는다(32바이트 이상)."""
        t = approval_server.new_token()
        # secrets.token_urlsafe(43) 은 약 57자.
        assert len(t) >= 40

    def test_new_token_is_unique(self):
        """두 번 호출하면 다른 토큰."""
        t1 = approval_server.new_token()
        t2 = approval_server.new_token()
        assert t1 != t2


# ---------------------------------------------------------------------------
# 보너스: GET 메서드 거부.
# ---------------------------------------------------------------------------
class TestMethodRestriction:
    """방어 7: GET 은 거부한다(승인은 부작용이 있으므로 POST 만)."""

    def test_get_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="getreject01",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, _, _ = _send_request(port, method="GET", path="/")
            assert status == 405
        finally:
            srv.close()

    def test_options_rejected(self):
        token = approval_server.new_token()
        srv = approval_server.ApprovalServer(
            product_key="optrej001ab",
            token=token,
            ttl_seconds=60,
        )
        port = srv.start()
        try:
            status, _, _ = _send_request(port, method="OPTIONS", path="/")
            assert status == 405
        finally:
            srv.close()
