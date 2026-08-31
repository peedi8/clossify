# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""``ui://`` 리소스로 제공할 HTML 조각 로더.

계약:

- HTML 원문은 ``src/clossify/ui/`` 안의 파일이다(문자열 리터럴 금지).
- ``importlib.resources`` 로 읽는다 — 소스 트리·editable 설치·wheel 설치 모두
  동일한 경로로 자산을 찾는다(common.package_data_path 와 같은 원칙).
- **파일 부재 시 명시적으로 예외를 던진다.** 조용히 빈 문자열을 반환하면
  호스트에 빈 화면이 뜨고 원인을 알 수 없다(조용한 실패 금지).
"""

from __future__ import annotations

from importlib.resources import files

_PKG = "clossify.ui"


def load_ui(filename: str) -> str:
    """``clossify.ui`` 패키지의 UI 조각 파일을 읽어 HTML 문자열을 반환한다.

    Args:
        filename: 패키지 안의 파일명(예: ``"setup.html"``).

    Returns:
        파일 전문(UTF-8 텍스트).

    Raises:
        FileNotFoundError: 파일이 패키지에 없을 때. 조용한 빈 문자열 반환 금지 —
            빈 화면의 원인을 호출자가 알 수 있어야 한다.
    """
    resource = files(_PKG).joinpath(filename)
    if not resource.is_file():
        raise FileNotFoundError(
            f"UI 리소스 파일이 패키지에 없습니다: {_PKG}/{filename}. "
            "wheel/설치본에 포함되었는지 확인하세요 (pyproject 패키지 데이터)."
        )
    return resource.read_text(encoding="utf-8")
