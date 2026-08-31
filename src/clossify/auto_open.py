# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""우측구이 조작 레인 — 조작 화면 자동 열기.

노선: **조작은 브라우저 창, 확인은 패널.** 승인·설정·이관 폼은 전부
브라우저에서 여는 전제로 만들어져 있다. 그런데 **여는 사람이 사용자**다 —
지금까지는 반환값의 파일 경로를 사람이 읽고 탐색기에서 찾아 더블클릭해야
했다. 이 모듈이 그 마지막 조각을 매운다: 준비·승인·폼 산출 직후 기본
브라우저로 그 화면을 자동으로 연다.

안전 기본값(불변):
  - **기본 OFF.** 설정 스위치(``enable_auto_open``) 가 켜져 있을 때만 동작.
    미설정 시 기존 거동(경로만 반환) 그대로 — 부작용 0.
  - **상태 폴더 밖 경로는 거부.** 우리가 만든 로컬 파일만 연다(path
    containment). 임의 URL 을 열 수 있는 통로를 만들지 않는다.
  - **셸 경유 금지.** ``os.system``·``shell=True`` 를 쓰지 않는다. 표준
    라이브러리 ``webbrowser`` 만 쓴다(새 의존성 금지).
  - **실패해도 흐름이 죽지 않는다.** 브라우저가 없거나 열기가 실패하면
    사유를 반환에 담고 경로 안내로 갈음한다.
  - **블로킹 금지.** 열기 호출이 도구 호출을 붙잡지 않는다.

테스트 주입: ``_DEFAULT_OPENER`` 를 ``mock.patch.object`` 로 교체하면
실제 브라우저 창을 띄우지 않고 호출을 셀 수 있다. 테스트는 이 경로로
열기 호출 횟수를 검증한다.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import common

# ---------------------------------------------------------------------------
# 도구 호출을 붙잡지 않기 위한 기본 opener.
#
# webbrowser.open() 은 일반적으로 빠르지만, 일부 환경(xdg-open 대기 등)에서는
# 블로킹될 수 있다. daemon 스레드에서 실행해 도구 호출을 붙잡지 않는다.
# 실패해도 예외를 밖으로 전파하지 않는다 — 호출부가 사유를 반환에 담는다.
# ---------------------------------------------------------------------------
_DefaultOpener = Callable[[str], bool]


def _real_opener(url: str) -> bool:
    """``webbrowser.open`` 을 감싼 기본 opener.

    Returns:
        ``webbrowser.open`` 의 반환값(성공 여부).
    """

    try:
        return bool(webbrowser.open(url, new=2))  # new=2: 새 탭
    except Exception:
        return False


# 테스트가 교체할 수 있는 opener 주입점. 모듈 수준 단일 변수.
# mock.patch.object(auto_open, "_DEFAULT_OPENER", ...) 로 교체한다.
_DEFAULT_OPENER: _DefaultOpener = _real_opener


# ---------------------------------------------------------------------------
# Path containment — 우리가 만든 로컬 파일만 연다.
#
# 임의 URL 이나 상태 폴더 밖 경로를 열 수 있는 통로를 만들지 않는다.
# ``file://`` URL 을 조합하기 전에 경로가 ``STATE_DIR`` 하위인지 확인한다.
# ---------------------------------------------------------------------------
def _is_within_state(path: str | os.PathLike[str]) -> bool:
    """``path`` 가 ``STATE_DIR`` 하위인지 확인한다.

    symlink·``..`` traversal 을 포함한 모든 우회를 막는다 — ``resolve()``
    로 정규화한 뒤 접두 검사를 한다.

    Returns:
        ``path`` 의 정규화 결과가 ``STATE_DIR`` 하위면 ``True``.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        state = Path(str(common.STATE_DIR)).resolve()
        try:
            resolved.relative_to(state)
            return True
        except ValueError:
            return False
    except (OSError, RuntimeError):
        return False


# ---------------------------------------------------------------------------
# 공개 API.
# ---------------------------------------------------------------------------


def maybe_open_screen(
    path: str | None,
    *,
    enabled: bool,
    label: str,
    result: dict[str, Any],
    result_key: str = "auto_opened",
    opener: _DefaultOpener | None = None,
) -> None:
    """설정이 켜져 있으면 ``path`` 를 기본 브라우저로 열고 결과를 ``result`` 에 기록.

    **기본 OFF** — ``enabled`` 가 ``False`` 면 아무것도 하지 않는다(회귀 0).
    ``enabled`` 가 ``True`` 면:

      1. ``path`` 가 비어 있으면 사유만 기록하고 연다 시도하지 않는다.
      2. ``path`` 가 ``STATE_DIR`` 밖이면 **거부** 하고 사유를 기록한다.
      3. daemon 스레드에서 opener 를 호출한다(블로킹 금지).
      4. 실패하면 사유 + 경로 안내를 기록한다 — 조용한 실패 금지.

    ``label`` 은 "무엇을 열었는지" 반환에 드러내는 용도(예: "preview",
    "config_form", "template_migration_form").

    ``result_key`` 의 기본값은 ``"auto_opened"``. 같은 반환에 여러 화면을
    열 일이 거의 없으므로 단일 키로 충분하다. 호출부가 다른 키를 원하면
    넘길 수 있다(예: 승인 대기 모드에서 기존 키를 피하고 싶을 때).

    Args:
        path: 열 로컬 파일 경로. ``None``/빈 문자열이면 열지 않는다.
        enabled: 자동 열기 설정 스위치. ``False`` 면 미동작(기본값 관례).
        label: 무엇을 열었는지 식별하는 짧은 문자열.
        result: 결과를 담을 dict. 새 키만 추가한다(기존 키 변경 금지).
        result_key: ``result`` 에 기록할 키 이름.
        opener: 테스트 주입용 opener. ``None`` 이면 ``_DEFAULT_OPENER``.

    NOTE:
        이 함수는 **절대 예외를 밖으로 전파하지 않는다.** 어떤 실패가
        있어도 반환 dict 의 새 키로 사유를 남긴다 — 흐름이 죽지 않는다.
    """
    # enabled 가 False 면 완전 미동작. 새 키도 남기지 않는다 — 기존 반환
    # 키·거동이 그대로임을 보장(회귀 0). 단, 호출부가 result_key 를
    # 항상 참조할 수 있도록 ``None`` 만 남긴다.
    if not enabled:
        result[result_key] = None
        return

    # 켜져 있으면 무엇을 열려 하는지 반환에 드러낸다(조용한 실행 금지).
    if not path or not str(path).strip():
        result[result_key] = {
            "opened": False,
            "label": label,
            "path": None,
            "reason": "경로가 비어 있어 열지 않았습니다.",
        }
        return

    # Path containment — 상태 폴더 밖 경로는 거부.
    if not _is_within_state(path):
        result[result_key] = {
            "opened": False,
            "label": label,
            "path": str(path),
            "reason": (
                "상태 폴더 밖의 경로는 자동으로 열 수 없습니다 " "(path containment). 직접 여세요."
            ),
        }
        return

    # daemon 스레드에서 열기 — 도구 호출을 붙잡지 않는다.
    op = opener if opener is not None else _DEFAULT_OPENER
    outcome_box: dict[str, Any] = {}

    def _run() -> None:
        try:
            url = Path(path).resolve().as_uri()
            ok = bool(op(url))
            outcome_box["ok"] = ok
            if not ok:
                outcome_box["reason"] = "opener 가 False 를 반환했습니다."
        except Exception as exc:  # 사유만 기록, 전파 금지.
            outcome_box["ok"] = False
            outcome_box["reason"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # 스레드가 끝나기를 잠시 기다린다 — opener 가 mock 이면 즉시 끝나고,
    # 실제 webbrowser.open 도 보통 즉시 반환한다. 길어지면 기다리지 않고
    # "진행 중" 으로 기록한다(블로킹 금지).
    t.join(timeout=2.0)
    if t.is_alive():
        # 여전히 실행 중 — 브라우저가 느리게 뜨는 중일 수 있다. 흐름은
        # 놓아주되, 반환에는 "진행 중" 을 남긴다(사용자가 기다릴지 결정).
        result[result_key] = {
            "opened": None,
            "label": label,
            "path": str(path),
            "reason": "브라우저 열기가 진행 중입니다(2초 초과).",
        }
        return

    if outcome_box.get("ok"):
        result[result_key] = {
            "opened": True,
            "label": label,
            "path": str(path),
            "reason": None,
        }
    else:
        reason = outcome_box.get("reason") or "알 수 없는 실패."
        result[result_key] = {
            "opened": False,
            "label": label,
            "path": str(path),
            "reason": (f"{reason} 브라우저를 직접 열어 경로를 확인하세요: {path}"),
        }
