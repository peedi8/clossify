# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""``pick_images`` 네이티브 파일 선택창 도구 검증.

모든 테스트는 tkinter 를 **모킹**한다 — 실제 선택창을 띄우지 않는다.

  (a) tools/list 노출 + 총 13개.
  (b) 선택 → 경로 반환 / 취소 → cancelled / tkinter 불가 → 명확한 error.
  (c) max_files 초과 시 truncated.
  (d) 실행 중 파일 내용 읽기 없음(builtins.open 호출 차단으로 증명).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server


def _list_tools():
    """mcp_server.mcp.list_tools() 가 코루틴이면 async 로 실행."""
    raw = mcp_server.mcp.list_tools()
    if asyncio.iscoroutine(raw):

        async def _await(coro):
            return await coro

        return asyncio.run(_await(raw))
    return raw


class _FakeRoot:
    """tk.Tk() 가독 — topmost/destroy 호출만 기록한다."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def withdraw(self):
        self.calls.append(("withdraw", (), {}))

    def attributes(self, *args, **kwargs):
        self.calls.append(("attributes", args, kwargs))

    def destroy(self):
        self.calls.append(("destroy", (), {}))


class _FakeFileDialog(types.SimpleNamespace):
    """tkinter.filedialog 가독 — askopenfilenames 반환값을 주입받는다."""

    def __init__(self, result=()):
        super().__init__()
        self.askopenfilenames_calls: list[dict] = []
        self._result = result

    def askopenfilenames(self, **kwargs):
        self.askopenfilenames_calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _install_fake_tk(monkeypatch, *, tk_factory=None, filedialog=None):
    """가짜 tkinter/tkinter.filedialog 모듈을 sys.modules 에 심는다."""
    tk_factory = tk_factory or _FakeRoot

    fake_root_holder: list[_FakeRoot] = []

    def _tk_factory():
        root = tk_factory()
        fake_root_holder.append(root)
        return root

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = _tk_factory
    fake_fd_mod = types.ModuleType("tkinter.filedialog")
    fake_fd_mod.askopenfilenames = filedialog.askopenfilenames
    fake_tk.filedialog = fake_fd_mod

    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_fd_mod)
    return fake_root_holder


# =========================================================================== #
# (a) 도구 노출 — tools/list 에 보이고 총 13개.
# =========================================================================== #


class TestToolExposure:
    def test_a_pick_images_listed_and_total_twelve(self):
        """(a) pick_images 가 tools/list 에 있고 총 13개다."""
        tools = _list_tools()
        names = [str(getattr(t, "name", t)) for t in tools]
        assert "pick_images" in names
        assert len(tools) == 13, f"도구가 13개여야 함: {len(tools)}"


# =========================================================================== #
# (b) 3경로 — 선택 / 취소 / tkinter 불가.
# =========================================================================== #


class TestPickImagesPaths:
    def test_b_selection_returns_absolute_paths(self, monkeypatch):
        """(b) 선택 결과가 paths/count 로 그대로 돌아온다."""
        fd = _FakeFileDialog(result=("C:/img/a.jpg", "C:/img/한글파일.png"))
        roots = _install_fake_tk(monkeypatch, filedialog=fd)
        result = mcp_server.pick_images()
        assert result["ok"] is True
        assert result["paths"] == ["C:/img/a.jpg", "C:/img/한글파일.png"]
        assert result["count"] == 2
        assert result["cancelled"] is False
        assert result["truncated"] is False
        assert result["error"] is None
        # topmost 지정 + 호출당 정확히 1회 선택창.
        assert len(fd.askopenfilenames_calls) == 1
        topmost = [c for c in roots[0].calls if c[0] == "attributes"]
        assert topmost and topmost[0][1] == ("-topmost", True)
        # 필터 — 이미지 + 모든 파일.
        ftypes = fd.askopenfilenames_calls[0]["filetypes"]
        assert any("*.jpg" in pats for _, pats in ftypes)
        assert ("모든 파일", "*.*") in ftypes

    def test_b_cancel_is_not_error(self, monkeypatch):
        """(b) 빈 선택 → ok=True, cancelled=True, paths=[]."""
        fd = _FakeFileDialog(result=())
        _install_fake_tk(monkeypatch, filedialog=fd)
        result = mcp_server.pick_images()
        assert result["ok"] is True
        assert result["cancelled"] is True
        assert result["paths"] == []
        assert result["count"] == 0
        assert result["error"] is None

    def test_b_tk_unavailable_error_guides_manual_input(self, monkeypatch):
        """(b) tkinter import 불가 → ok=False + '경로를 직접 입력하라'."""
        # sys.modules 에 None 을 심으면 import 가 ImportError 를 낸다.
        monkeypatch.setitem(sys.modules, "tkinter", None)
        monkeypatch.setitem(sys.modules, "tkinter.filedialog", None)
        result = mcp_server.pick_images()
        assert result["ok"] is False
        assert result["paths"] == []
        assert result["error"] is not None
        assert "경로를 직접 입력하라" in result["error"]

    def test_b_tk_init_failure_error_guides_manual_input(self, monkeypatch):
        """(b) 디스플레이 없음 등 Tk() 실패 → 명확한 error."""

        class _BrokenTk:
            def __init__(self):
                raise RuntimeError("no display")

        fd = _FakeFileDialog(result=("x",))
        _install_fake_tk(monkeypatch, tk_factory=_BrokenTk, filedialog=fd)
        result = mcp_server.pick_images()
        assert result["ok"] is False
        assert result["error"] is not None
        assert "경로를 직접 입력하라" in result["error"]

    def test_b_dialog_exception_returns_error(self, monkeypatch):
        """(b) 선택창 자체가 예외를 던져도 명확한 error 로 돌아온다."""
        fd = _FakeFileDialog(result=RuntimeError("dialog boom"))
        _install_fake_tk(monkeypatch, filedialog=fd)
        result = mcp_server.pick_images()
        assert result["ok"] is False
        assert "dialog boom" in result["error"]
        assert "경로를 직접 입력하라" in result["error"]

    def test_b_bad_max_files_rejected(self, monkeypatch):
        """(b) max_files<1 또는 비정수는 조용히 무시되지 않는다."""
        fd = _FakeFileDialog(result=("a",))
        _install_fake_tk(monkeypatch, filedialog=fd)
        assert mcp_server.pick_images(max_files=0)["ok"] is False
        assert mcp_server.pick_images(max_files="x")["ok"] is False
        # 거부 시 선택창을 열지 않는다.
        assert fd.askopenfilenames_calls == []


# =========================================================================== #
# (c) max_files 초과 — 앞에서 자르고 truncated=true.
# =========================================================================== #


class TestMaxFilesTruncation:
    def test_c_over_limit_truncated(self, monkeypatch):
        """(c) 5개 선택 + max_files=3 → 앞 3개, truncated=true."""
        fd = _FakeFileDialog(result=tuple(f"C:/img/{i}.jpg" for i in range(5)))
        _install_fake_tk(monkeypatch, filedialog=fd)
        result = mcp_server.pick_images(max_files=3)
        assert result["ok"] is True
        assert result["paths"] == ["C:/img/0.jpg", "C:/img/1.jpg", "C:/img/2.jpg"]
        assert result["count"] == 3
        assert result["truncated"] is True

    def test_c_within_limit_not_truncated(self, monkeypatch):
        """(c) 이하 선택 → truncated=false."""
        fd = _FakeFileDialog(result=("C:/img/a.jpg",))
        _install_fake_tk(monkeypatch, filedialog=fd)
        result = mcp_server.pick_images(max_files=10)
        assert result["truncated"] is False


# =========================================================================== #
# (d) 파일 내용 읽기 없음 — builtins.open 호출 차단으로 증명.
# =========================================================================== #


class TestNoFileReads:
    def test_d_no_open_calls_during_selection(self, monkeypatch):
        """(d) 선택 경로 전체 파이프라인에서 open() 이 한 번도 불리지 않는다."""
        fd = _FakeFileDialog(result=("C:/img/a.jpg", "C:/img/b.webp"))
        _install_fake_tk(monkeypatch, filedialog=fd)

        opened = []

        def _guard_open(file, *a, **k):
            opened.append(str(file))
            raise AssertionError(f"pick_images 는 파일을 열면 안 됨: {file}")

        with mock.patch("builtins.open", side_effect=_guard_open):
            result = mcp_server.pick_images(max_files=10, title="테스트")
        assert result["ok"] is True
        assert opened == []

    def test_d_source_has_no_read_calls(self):
        """(d) 코드 검토 근거 — pick_images 본문에 read()/read_bytes() 없음."""
        src = Path(mcp_server.__file__).read_text(encoding="utf-8")
        body = src.split("def pick_images(", 1)[1].split("\ndef ", 1)[0]
        assert ".read(" not in body
        assert "read_bytes" not in body
        assert "read_text" not in body
        assert "getsize" not in body
