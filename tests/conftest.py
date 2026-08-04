# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""공용 pytest 픽스처.

미리보기 승인 게이트(``require_preview_confirmation``)는 기본이 켜져 있다.
이전에는 autouse 픽스처가 모든 테스트에서 게이트를 꺼버렸으나, 그렇게 하면
나중에 작성된 테스트가 게이트가 꺼진 상태를 물려받아 게이트 동작을 증명하지
못한다(dry-run bypass 와 같은 결함). 이제 게이트는 **기본 켜져 있고**,
테스트가 명시적으로 게이트를 끄려면 opt-in 픽스처 ``preview_gate_off`` 를
요청하거나, 더 권장되는 방식으로 ``preview_confirmed=True`` 를 직접
전달한다.
"""

from __future__ import annotations

import pytest

from clossify import mcp_server


@pytest.fixture
def preview_gate_off(monkeypatch):
    """미리보기 승인 게이트를 끄는 opt-in 픽스처.

    **autouse 가 아니다** — 테스트가 명시적으로 이 픽스처를 요청해야 한다.
    게이트가 테스트 대상이 아닌 다른 동작을 검증할 때 편의를 위해 제공된다.
    게이트 동작 자체를 검증하는 테스트(``test_preview_gate.py``)는 이
    픽스처를 쓰지 않고 각 테스트에서 명시적으로 게이트를 켠다.

    더 권장되는 방식은 ``register_product(..., preview_confirmed=True)`` 를
    직접 전달하는 것이다 — 그것이 테스트의 의도를 명확히 드러낸다.
    """
    monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: False)
