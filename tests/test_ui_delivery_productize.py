# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""위젯 배달 요령의 제품화 테스트 — check_config 반환에 HTML+지시문 싣기.

수용 조건 (a)~(g) 대응 — 모두 임시 설정/로컬 프로세스, 사용자 실설정 접근 없음:

  (a) 미완료 → ui_html 비어있지 않고 cid/csec 입력 2개 포함.
  (b) 미완료 → ui_instructions 에 렌더·어댑터·브라우저 개념 포함.
  (c) 완료 → ui_html/ui_instructions 부재.
  (d) 기존 키 전부 보존(회귀).
  (e) stdio initialize 응답의 instructions 필드에 안내 반영.
  (f) ui_html 이 src/clossify/ui/setup.html 파일과 바이트 동일.
  (g) 기존 테스트 전부 통과(pytest 실행으로 증명).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import bcrypt

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server
from clossify.ui.loader import load_ui


def _write_config(tmp_path, naver: dict | None) -> Path:
    """임시 config.json 경로. naver=None 이면 파일을 안 쓴다(부재 경로)."""
    if naver is None:
        return tmp_path / "absent_config.json"
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"naver": naver}), encoding="utf-8")
    return config_file


def _complete_naver() -> dict:
    return {
        "client_id": "test_client_id",
        "client_secret": bcrypt.gensalt(rounds=4).decode(),
        "store_url_slug": "test-slug",
    }


# ============================================================================ #
# (a)/(b) 미완료 → ui_html · ui_instructions
# ============================================================================ #
class TestIncompleteDelivery:
    def test_ui_html_nonempty_with_two_inputs(self, tmp_path, monkeypatch):
        """(a) 미완료 → ui_html 이 setup.html 전문이고 입력 2개를 담는다."""
        cfg_path = _write_config(
            tmp_path,
            {
                "client_id": "REPLACE_WITH_CLIENT_ID",
                "client_secret": "REPLACE_WITH_SECRET",
                "store_url_slug": "REPLACE_WITH_SLUG",
            },
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        html = result["ui_html"]
        assert isinstance(html, str) and html, "ui_html 이 비어 있음"
        assert 'id="cid"' in html, "ui_html 에 cid 입력 없음"
        assert 'id="csec"' in html, "ui_html 에 csec 입력 없음"

    def test_ui_instructions_concepts(self, tmp_path, monkeypatch):
        """(b) 미완료 → 지시문에 렌더·어댑터·브라우저 개념이 모두 담긴다."""
        cfg_path = _write_config(tmp_path, None)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        instructions = result["ui_instructions"]
        assert isinstance(instructions, str) and instructions
        for concept in ("렌더", "어댑터", "브라우저"):
            assert concept in instructions, f"ui_instructions 에 {concept!r} 개념 없음"

    def test_missing_file_path_also_carries_delivery(self, tmp_path, monkeypatch):
        """파일 부재(조기 반환 경로) → ui_html/ui_instructions 있다."""
        cfg_path = _write_config(tmp_path, None)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        assert result.get("ui_html"), "파일 부재 경로에서 ui_html 없음"
        assert result.get("ui_instructions"), "파일 부재 경로에서 ui_instructions 없음"

    def test_no_secret_in_ui_html(self, tmp_path, monkeypatch):
        """★ ui_html 은 정적 파일 그대로 — 사용자 값·시크릿 절대 불혼합."""
        canary = "CANARY-" + bcrypt.gensalt(rounds=4).decode()
        cfg_path = _write_config(
            tmp_path,
            {"client_id": "REPLACE_WITH_CLIENT_ID", "client_secret": canary},
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        assert canary not in result["ui_html"], "ui_html 에 client_secret 값 혼합됨"
        assert "cid-canary" not in result["ui_html"], "ui_html 에 client_id 값 혼합됨"


# ============================================================================ #
# (c) 완료 → 두 키 부재 (기존 게이팅과 동일 조건)
# ============================================================================ #
class TestCompleteOmitsDelivery:
    def test_complete_config_has_no_delivery_keys(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, _complete_naver())
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        assert result["ok"] is True
        assert "ui_html" not in result, "완료 경로에서 ui_html 이 반환됨"
        assert "ui_instructions" not in result, "완료 경로에서 ui_instructions 반환됨"


# ============================================================================ #
# (d) 기존 키 보존 (회귀)
# ============================================================================ #
class TestLegacyKeysPreserved:
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
            "resource_uri",
            "ui_hint",
            "config_form_path",
            "config_form_open",
        }
    )

    def test_incomplete_result_preserves_legacy_keys(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, {"client_id": "x"})
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        for key in self._LEGACY_KEYS:
            assert key in result, f"기존 반환 키 {key!r} 이 미완료 경로에서 사라짐"

    def test_tool_count_unchanged(self):
        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            tools = asyncio.run(tools)
        assert len(tools) == 12, f"도구 수 변경됨 (11 유지 계약 위반): {len(tools)}"


# ============================================================================ #
# (e) stdio initialize 응답의 instructions 필드
# ============================================================================ #
class TestServerInstructionsOnInitialize:
    def test_initialize_instructions_carry_widget_guidance(self, tmp_path, monkeypatch):
        """실제 stdio initialize 응답의 instructions 에 위젯 안내가 있다."""
        import contextlib
        import os

        import pytest

        stdio_mod = pytest.importorskip("mcp.client.stdio")
        session_mod = pytest.importorskip("mcp.client.session")
        pytest.importorskip("mcp")

        # 임시 설정 — 사용자 실설정 접근 금지. 환경을 물려주되 CLOSSIFY_CONFIG 만 교체.
        cfg_path = _write_config(tmp_path, None)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        env = dict(os.environ)

        server_cmd = [
            sys.executable,
            "-c",
            "import sys;"
            f"sys.path.insert(0, {str(_SRC)!r});"
            "from clossify import mcp_server; mcp_server.main()",
        ]

        async def _run() -> str | None:
            from mcp import StdioServerParameters

            params = StdioServerParameters(command=server_cmd[0], args=server_cmd[1:], env=env)
            async with contextlib.AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_mod.stdio_client(params))
                session = await stack.enter_async_context(session_mod.ClientSession(read, write))
                init = await session.initialize()
                return init.instructions

        instructions = asyncio.run(_run())
        assert instructions, "initialize 응답에 instructions 필드가 없거나 비어 있음"
        assert "check_config" in instructions
        for concept in ("ui_html", "ui_instructions", "렌더", "어댑터", "브라우저"):
            assert concept in instructions, f"서버 instructions 에 {concept!r} 없음"


# ============================================================================ #
# (f) ui_html 이 파일과 바이트 동일 (중복 사본이 아님을 증명)
# ============================================================================ #
class TestUiHtmlByteIdentical:
    def test_ui_html_equals_file_bytes(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, None)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        html = result["ui_html"]
        # 로더(=importlib.resources, 배포 자산의 단일 진실) 와 비교.
        assert html == load_ui("setup.html"), "ui_html 이 로더 원문과 다름 (사본 의심)"
        # 소스 트리 파일과도 바이트 동일 — 배포·개발 트리 동일성.
        src_file = _SRC / "clossify" / "ui" / "setup.html"
        assert html == src_file.read_text(
            encoding="utf-8"
        ), "ui_html 이 src/clossify/ui/setup.html 파일과 바이트 동일하지 않음"
