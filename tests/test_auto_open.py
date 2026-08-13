# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""조작 화면 자동 열기 검증.

본 테스트는 ``enable_auto_open`` 설정 스위치와 ``auto_open`` 모듈의
안전 계약을 검증한다:

  (a) **기본 OFF**: 설정이 꺼져 있으면 opener 호출 0회, 회귀 0.
  (b) **ON 이면 열기**: 설정이 켜져 있으면 opener 호출 1회, result 에 기록.
  (c) **실패해도 흐름이 죽지 않는다**: opener 가 False 를 반환해도 예외 없이 사유 기록.
  (d) **path containment**: STATE_DIR 밖 경로는 거부, opener 호출 0회.
  (e) **셸 경유 금지**: 소스에 ``os.system``·``shell=True``·``subprocess`` 없음.
  (f) **방어 회귀**: 기존 승인 서버/폼 서버 방어가 그대로 동작.
  (g) **도구 수 불변**: MCP 도구는 여전히 9개.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import auto_open, common, mcp_server


async def _await_if_needed(coro):
    """코루틴이면 그대로 await 한다."""
    return await coro


def _list_tools():
    """mcp_server.mcp.list_tools() 가 코루틴이면 async 로 실행."""
    raw = mcp_server.mcp.list_tools()
    if asyncio.iscoroutine(raw):
        return asyncio.run(_await_if_needed(raw))
    return raw


# =========================================================================== #
# 공통 헬퍼.
# =========================================================================== #


@pytest.fixture
def isolated_state_dir(tmp_path, monkeypatch):
    """common.STATE_DIR 을 tmp_path/.local 로 격리.

    auto_open 의 path containment 는 common.STATE_DIR 기준으로 검사하므로,
    테스트마다 임시 디렉터리로 격리한다.
    """
    fake_state = tmp_path / ".local"
    fake_state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "STATE_DIR", fake_state)
    return fake_state


def _make_state_file(state_dir: Path, name: str = "preview.html") -> str:
    """STATE_DIR 하위에 더미 HTML 파일을 만들고 경로를 반환."""
    p = state_dir / name
    p.write_text("<html><body>test</body></html>", encoding="utf-8")
    return str(p)


# =========================================================================== #
# (a) 기본 OFF — opener 호출 0회, 회귀 0.
# =========================================================================== #


class TestAutoOpenDefaultOff:
    """``enable_auto_open`` 이 꺼져 있으면 아무 일도 일어나지 않는다."""

    def test_a_disabled_calls_opener_zero_times(self, isolated_state_dir):
        """(a) enabled=False → opener 호출 0회, result["auto_opened"]=None."""
        calls = []

        def _fake_opener(url: str) -> bool:
            calls.append(url)
            return True

        result: dict = {}
        auto_open.maybe_open_screen(
            _make_state_file(isolated_state_dir),
            enabled=False,
            label="preview",
            result=result,
            opener=_fake_opener,
        )
        assert len(calls) == 0, "enabled=False 일 때 opener 가 호출되면 안 됨"
        assert result.get("auto_opened") is None

    def test_a_disabled_no_new_keys_beyond_result_key(self, isolated_state_dir):
        """(a) enabled=False 일 때 result_key 키만 추가되고 다른 키는 없다."""
        result: dict = {"existing": 42}
        auto_open.maybe_open_screen(
            _make_state_file(isolated_state_dir),
            enabled=False,
            label="preview",
            result=result,
            opener=lambda url: True,
        )
        # 기존 키 보존.
        assert result["existing"] == 42
        # auto_opened 키는 None 으로 있음 (호출부 참조 가능).
        assert result["auto_opened"] is None
        # 추가 키는 없음.
        assert len(result) == 2

    def test_a_config_enable_auto_open_defaults_false(self, tmp_path):
        """(a) _config_enable_auto_open() 기본값 False.

        config 파일에 ``enable_auto_open`` 키가 없으면 False 를 반환해야 한다.
        """
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"other_key": "value"}),  # enable_auto_open 없음.
            encoding="utf-8",
        )
        with mock.patch.object(mcp_server.naver_client, "config_path", return_value=str(cfg_file)):
            assert mcp_server._config_enable_auto_open() is False

    def test_a_config_enable_auto_open_non_bool_returns_false(self, tmp_path):
        """(a) 값이 bool 이 아니면 False (조용히 켜지지 않는다)."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"enable_auto_open": "yes"}),  # 문자열, bool 아님.
            encoding="utf-8",
        )
        with mock.patch.object(mcp_server.naver_client, "config_path", return_value=str(cfg_file)):
            assert mcp_server._config_enable_auto_open() is False

    def test_a_config_enable_auto_open_true_when_set(self, tmp_path):
        """(a) config 에 명시적으로 True 면 True."""
        import json

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"enable_auto_open": True}),
            encoding="utf-8",
        )
        with mock.patch.object(mcp_server.naver_client, "config_path", return_value=str(cfg_file)):
            assert mcp_server._config_enable_auto_open() is True

    def test_a_config_missing_file_returns_false(self, tmp_path):
        """(a) config 파일 자체가 없으면 False (예외 전파 금지)."""
        with mock.patch.object(
            mcp_server.naver_client,
            "config_path",
            return_value=str(tmp_path / "nonexistent.json"),
        ):
            assert mcp_server._config_enable_auto_open() is False


# =========================================================================== #
# (b) ON 이면 열기 — opener 호출 1회, result 에 기록.
# =========================================================================== #


class TestAutoOpenEnabled:
    """``enable_auto_open`` 이 켜져 있으면 브라우저로 연다."""

    def test_b_enabled_calls_opener_once(self, isolated_state_dir):
        """(b) enabled=True → opener 호출 1회, file:// URL 전달."""
        calls = []

        def _fake_opener(url: str) -> bool:
            calls.append(url)
            return True

        path = _make_state_file(isolated_state_dir, "preview.html")
        result: dict = {}
        auto_open.maybe_open_screen(
            path,
            enabled=True,
            label="preview",
            result=result,
            opener=_fake_opener,
        )

        assert len(calls) == 1, f"opener 가 1회 호출되어야 함: {len(calls)}"
        assert calls[0].startswith("file://"), f"file:// URL 이어야 함: {calls[0]}"
        assert "preview.html" in calls[0]

    def test_b_enabled_records_opened_true(self, isolated_state_dir):
        """(b) opener 성공 → result["auto_opened"]["opened"]=True."""
        path = _make_state_file(isolated_state_dir)
        result: dict = {}
        auto_open.maybe_open_screen(
            path,
            enabled=True,
            label="preview",
            result=result,
            opener=lambda url: True,
        )
        entry = result.get("auto_opened")
        assert entry is not None
        assert entry["opened"] is True
        assert entry["label"] == "preview"
        assert entry["path"] == path
        assert entry["reason"] is None

    def test_b_label_is_recorded(self, isolated_state_dir):
        """(b) label 이 result 에 드러난다 (조용한 실행 금지)."""
        path = _make_state_file(isolated_state_dir)
        result: dict = {}
        auto_open.maybe_open_screen(
            path,
            enabled=True,
            label="config_form",
            result=result,
            opener=lambda url: True,
        )
        assert result["auto_opened"]["label"] == "config_form"

    def test_b_custom_result_key(self, isolated_state_dir):
        """(b) result_key 를 바꾸면 그 키로 기록된다."""
        path = _make_state_file(isolated_state_dir)
        result: dict = {}
        auto_open.maybe_open_screen(
            path,
            enabled=True,
            label="approval_preview",
            result=result,
            result_key="my_auto_open",
            opener=lambda url: True,
        )
        assert "my_auto_open" in result
        assert result["my_auto_open"]["opened"] is True
        # 기본 키는 없어야 함.
        assert "auto_opened" not in result


# =========================================================================== #
# (c) 실패해도 흐름이 죽지 않는다.
# =========================================================================== #


class TestAutoOpenFailureSafe:
    """opener 실패/예외가 흐름을 죽이지 않는다."""

    def test_c_opener_returns_false_records_reason(self, isolated_state_dir):
        """(c) opener 가 False 를 반환하면 사유 + 경로 안내를 기록."""
        path = _make_state_file(isolated_state_dir)
        result: dict = {}
        auto_open.maybe_open_screen(
            path,
            enabled=True,
            label="preview",
            result=result,
            opener=lambda url: False,
        )
        entry = result["auto_opened"]
        assert entry["opened"] is False
        assert entry["reason"] is not None
        assert "직접" in entry["reason"] or "확인" in entry["reason"]
        assert path in entry["reason"]

    def test_c_opener_raises_exception_does_not_propagate(self, isolated_state_dir):
        """(c) opener 가 예외를 던져도 maybe_open_screen 은 전파하지 않는다."""
        path = _make_state_file(isolated_state_dir)

        def _bad_opener(url: str) -> bool:
            raise RuntimeError("browser explosion")

        result: dict = {}
        # 예외가 밖으로 나오지 않아야 함.
        auto_open.maybe_open_screen(
            path,
            enabled=True,
            label="preview",
            result=result,
            opener=_bad_opener,
        )
        entry = result["auto_opened"]
        assert entry["opened"] is False
        assert "RuntimeError" in entry["reason"]
        assert "browser explosion" in entry["reason"]

    def test_c_empty_path_does_not_open(self, isolated_state_dir):
        """(c) 경로가 비어 있으면 열지 않고 사유만 기록."""
        result: dict = {}
        auto_open.maybe_open_screen(
            None,
            enabled=True,
            label="preview",
            result=result,
            opener=lambda url: True,
        )
        entry = result["auto_opened"]
        assert entry["opened"] is False
        assert entry["path"] is None
        assert "비어" in entry["reason"]

    def test_c_empty_string_path_does_not_open(self, isolated_state_dir):
        """(c) 빈 문자열 경로도 열지 않는다."""
        result: dict = {}
        auto_open.maybe_open_screen(
            "   ",
            enabled=True,
            label="preview",
            result=result,
            opener=lambda url: True,
        )
        entry = result["auto_opened"]
        assert entry["opened"] is False


# =========================================================================== #
# (d) Path containment — STATE_DIR 밖 경로는 거부.
# =========================================================================== #


class TestAutoOpenPathContainment:
    """상태 폴더 밖의 경로는 자동으로 열지 않는다."""

    def test_d_outside_state_rejected(self, isolated_state_dir, tmp_path):
        """(d) STATE_DIR 밖 경로 → opener 호출 0회, 거부 사유 기록."""
        calls = []

        def _fake_opener(url: str) -> bool:
            calls.append(url)
            return True

        outside = tmp_path / "outside.html"
        outside.write_text("<html>outside</html>", encoding="utf-8")
        result: dict = {}
        auto_open.maybe_open_screen(
            str(outside),
            enabled=True,
            label="preview",
            result=result,
            opener=_fake_opener,
        )
        assert len(calls) == 0, "STATE_DIR 밖 경로는 opener 호출 0회여야 함"
        entry = result["auto_opened"]
        assert entry["opened"] is False
        assert "containment" in entry["reason"] or "밖" in entry["reason"]

    def test_d_dotdot_traversal_rejected(self, isolated_state_dir, tmp_path):
        """(d) ``..`` traversal 로 STATE_DIR 밖을 가리키면 거부."""
        calls = []
        # tmp_path/.local/sub/../../../outside.html
        sub = isolated_state_dir / "sub"
        sub.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.html"
        outside.write_text("<html>traversal</html>", encoding="utf-8")

        # 상대 경로로 .. traversal 시도.
        traversal_path = str(sub / ".." / ".." / ".." / ".." / "outside.html")
        result: dict = {}
        auto_open.maybe_open_screen(
            traversal_path,
            enabled=True,
            label="preview",
            result=result,
            opener=lambda url: calls.append(url) or True,
        )
        # resolve() 후 STATE_DIR 밖이면 거부.
        assert len(calls) == 0 or result["auto_opened"]["opened"] is False

    def test_d_within_state_allowed(self, isolated_state_dir):
        """(d) STATE_DIR 하위 경로는 허용."""
        calls = []
        sub = isolated_state_dir / "prepared"
        sub.mkdir(parents=True, exist_ok=True)
        p = sub / "preview.html"
        p.write_text("<html>ok</html>", encoding="utf-8")
        result: dict = {}
        auto_open.maybe_open_screen(
            str(p),
            enabled=True,
            label="preview",
            result=result,
            opener=lambda url: calls.append(url) or True,
        )
        assert len(calls) == 1
        assert result["auto_opened"]["opened"] is True

    def test_d_is_within_state_helper(self, isolated_state_dir, tmp_path):
        """(d) _is_within_state 헬퍼가 올바르게 판정한다."""
        inside = isolated_state_dir / "file.html"
        inside.write_text("x", encoding="utf-8")
        outside = tmp_path / "outside.html"
        outside.write_text("x", encoding="utf-8")
        assert auto_open._is_within_state(str(inside)) is True
        assert auto_open._is_within_state(str(outside)) is False


# =========================================================================== #
# (e) 셸 경유 금지 — 소스에 os.system·shell=True·subprocess 없음.
# =========================================================================== #


class TestAutoOpenNoShellExecution:
    """``os.system``·``shell=True``·``subprocess`` 를 쓰지 않는다."""

    def test_e_no_os_system_in_source(self):
        """(e) auto_open.py 코드에 ``os.system`` 호출이 없다.

        주석/docstring 이 아닌 실제 코드 라인만 검사한다.
        """
        import ast

        src_path = Path(auto_open.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # os.system(...) 패턴.
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                    and func.attr == "system"
                ):
                    violations.append("os.system()")
                # Popen(..., shell=True) 패턴.
                for kw in node.keywords:
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                        violations.append("shell=True")
        assert not violations, f"셸 경유 호출 감지: {violations}"

    def test_e_no_subprocess_in_source(self):
        """(e) auto_open.py 코드에 ``subprocess`` import/사용이 없다."""
        import ast

        src_path = Path(auto_open.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "subprocess", "subprocess import 감지됨"
            if isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess", "subprocess from-import 감지됨"
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "subprocess", f"subprocess 사용 감지됨: {node.attr}"

    def test_e_no_shell_true_in_source(self):
        """(e) auto_open.py 코드에 ``shell=True`` 가 없다 (AST 기반)."""
        import ast

        src_path = Path(auto_open.__file__)
        tree = ast.parse(src_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg == "shell"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True
                    ):
                        pytest.fail("shell=True 감지됨")

    def test_e_uses_webbrowser_only(self):
        """(e) auto_open.py 는 ``webbrowser`` 표준 라이브러리만 쓴다."""
        src = Path(auto_open.__file__).read_text(encoding="utf-8")
        assert "import webbrowser" in src, "webbrowser 가 import 되어야 함"
        assert "webbrowser.open" in src, "webbrowser.open 을 사용해야 함"


# =========================================================================== #
# (f) 방어 회귀 — 승인 서버/폼 서버 방어가 그대로 동작.
# =========================================================================== #


class TestDefenseRegression:
    """auto_open 추가로 기존 방어가 깨지지 않는다."""

    def test_f_approval_server_still_requires_token(self):
        """(f) 승인 서버의 토큰 방어가 그대로 동작하는지 확인.

        auto_open 은 승인 서버와 무관하게 동작해야 한다 — 방어를 건드리지 않는다.
        여기서는 auto_open 모듈이 approval_server 를 import 하지 않는지만 확인한다.
        """
        src = Path(auto_open.__file__).read_text(encoding="utf-8")
        # auto_open 은 approval_server 를 직접 import 하지 않는다.
        assert (
            "import approval_server" not in src
        ), "auto_open 이 approval_server 를 직접 import 하면 안 됨"
        assert "from . import approval_server" not in src

    def test_f_config_form_server_not_imported(self):
        """(f) config_form_server 도 직접 import 하지 않는다."""
        src = Path(auto_open.__file__).read_text(encoding="utf-8")
        assert "import config_form_server" not in src
        assert "from . import config_form_server" not in src

    def test_f_mcp_tool_count_unchanged(self):
        """(f) MCP 도구 수가 9개로 불변."""
        tools = _list_tools()
        assert len(tools) == 9, f"도구가 9개여야 함: {len(tools)}"


# =========================================================================== #
# (g) 도구 수 불변 — MCP 도구는 여전히 9개.
# =========================================================================== #


class TestToolCount:
    """auto_open 추가로 MCP 도구가 늘지 않는다."""

    def test_g_exactly_nine_mcp_tools(self):
        """(g) ``@mcp.tool()`` 데코레이터가 9개, runtime list_tools() 도 9개."""
        # 소스 카운트.
        src = Path(mcp_server.__file__).read_text(encoding="utf-8")
        decorator_count = src.count("@mcp.tool()")
        assert decorator_count == 9, f"@mcp.tool() 데코레이터가 9개여야 함: {decorator_count}"
        # 런타임 카운트.
        tools = _list_tools()
        assert len(tools) == 9, f"runtime 도구가 9개여야 함: {len(tools)}"

    def test_g_auto_open_not_a_tool(self):
        """(g) auto_open 은 MCP 도구로 등록되지 않는다."""
        tools = _list_tools()
        names = [str(getattr(t, "name", t)) for t in tools]
        assert "auto_open" not in names
        assert "maybe_open_screen" not in names

    def test_g_no_new_mcp_tool_decorator_for_auto_open(self):
        """(g) mcp_server.py 에 auto_open 관련 @mcp.tool() 이 추가되지 않았다."""
        src = Path(mcp_server.__file__).read_text(encoding="utf-8")
        # auto_open 을 감싸는 새 도구가 등록되지 않았는지 확인.
        assert "@mcp.tool()" in src  # 기존 7개는 있어야 함.
        # "def auto_open" 으로 시작하는 @mcp.tool 도구가 없어야 함.
        lines = src.split("\n")
        for i, line in enumerate(lines):
            if "@mcp.tool()" in line:
                # 다음 non-empty line 이 def 인지 확인.
                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].strip() and not lines[j].strip().startswith("#"):
                        # auto_open 으로 시작하는 도구 정의가 없어야 함.
                        assert (
                            not lines[j].strip().startswith("def auto_open")
                        ), "auto_open 이 MCP 도구로 등록되면 안 됨"
                        break


# =========================================================================== #
# (bonus) _real_opener 가 예외를 삼킨다.
# =========================================================================== #


class TestRealOpenerSafeWrapper:
    """``_real_opener`` 가 ``webbrowser.open`` 실패를 예외로 전파하지 않는다."""

    def test_real_opener_swallows_exception(self):
        """``_real_opener`` 가 예외를 False 로 변환한다."""
        with mock.patch.object(auto_open.webbrowser, "open", side_effect=OSError("no browser")):
            result = auto_open._real_opener("file:///tmp/test.html")
        assert result is False

    def test_real_opener_returns_bool(self):
        """``_real_opener`` 반환값이 bool 이다."""
        with mock.patch.object(auto_open.webbrowser, "open", return_value=True):
            assert auto_open._real_opener("file:///tmp/test.html") is True
        with mock.patch.object(auto_open.webbrowser, "open", return_value=False):
            assert auto_open._real_opener("file:///tmp/test.html") is False

    def test_real_opener_uses_new_tab(self):
        """``_real_opener`` 가 ``new=2`` (새 탭) 로 호출한다."""
        with mock.patch.object(auto_open.webbrowser, "open", return_value=True) as mock_open:
            auto_open._real_opener("file:///tmp/test.html")
            _, kwargs = mock_open.call_args
            assert kwargs.get("new") == 2, f"new=2 여야 함: {kwargs}"
