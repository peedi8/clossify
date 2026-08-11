# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""포트 대기/닫힘 확인 공용 헬퍼.

왜 공용인가: 이 함수들이 두 시험 파일에 각각 사본으로 있었는데, 한쪽만
조용히 갈라져 재시도 간격 차이로 원격 검사에서 빨간불을 만들었다
(2026-08-11). 사본이 둘이면 다시 갈라진다 — 한 곳에서 유지한다.
"""

from __future__ import annotations

import socket
import time


def wait_for_port(port: int, timeout: float = 3.0) -> bool:
    """포트가 실제로 열려있는지 소켓으로 확인."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def port_is_closed(port: int, timeout: float = 3.0) -> bool:
    """포트가 닫혔는지 확인(연결 실패 = 닫힘)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                time.sleep(0.1)  # 아직 열려 있음.
        except OSError:
            return True  # 닫힘.
    return False
