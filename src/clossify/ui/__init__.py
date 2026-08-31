# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""대화창 인라인 렌더용 UI 조각(``ui://`` 리소스) 패키지.

HTML 원문은 파이썬 문자열 리터럴이 아니라 **파일**(``setup.html`` 등) 로 둔다 —
수정·리뷰가 파일 단위로 이루어지도록. 로더(``clossify.ui.loader``) 는
``importlib.resources`` 로 이 파일들을 읽는다.
"""

from __future__ import annotations

from .loader import load_ui

__all__ = ["load_ui"]
