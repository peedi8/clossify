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

★ N60 — 테스트 외부 네트워크 차단
-----------------------------------
2026-08-08 사고: 코드가 멀쩡한데 테스트 2건이 갑자기 빨간불. 원인은 네이버
403(IP 허용목록 불일치) 이었다 — 즉 테스트 통과의 일부가 **남의 서버 상태와
자격증명에 걸려** 있었다. 이를 막기 위해 본 conftest 는 autouse 픽스처로
**로컬 루프백이 아닌 모든 목적지** 로의 소켓 연결을 명확한 예외로 차단한다.

실측(N60 계약 전): 전체 스위트 소켓 연결 3,886 회 → 그중 3,882 회는
``127.0.0.1`` (로컬 폼/승인 서버 테스트). **막으면 안 된다.** 외부는 2 회
(네이버 OAuth 토큰 엔드포인트 223.130.196.242:443) — 이것이 막혀야 정상이다.
``pytest-socket`` 등 새 의존성 없이 표준 라이브러리(``socket``) 로 구현한다.

실패 방식 — **조용히 넘어가지 않는다.** 외부 연결 시도 시
``ExternalNetworkBlockedError`` 로 테스트를 실패시키며, 메시지에 **어느
목적지로 나가려 했는지** 와 **왜 막혔는지(테스트는 실호출 금지)** 를 담는다.
**절대 mock 응답을 대신 돌려주지 않는다** — 그러면 실패가 성공으로 위장된다.

DNS 해석(``socket.getaddrinfo``)은 막지 않는다. 일부 로컬 테스트가
``localhost`` 를 해석하기 위해 ``getaddrinfo`` 를 거치며, 이를 막으면
회귀가 발생할 수 있다. 소켓 ``connect`` 단계에서 목적지 IP 로 판정하는
것이 외부 차단의 단일 진실 지점이다.

실호출이 정말 필요한 테스트는 명시적 마커 ``@pytest.mark.allow_external_network``
또는 ``allow_external_network`` 픽스처로만 예외를 요청할 수 있고, 그런 테스트는
기본 실행에서 제외된다(``-m "not allow_external_network"`` 를 CI 가 붙이거나
pyproject 의 testcfg 가 자동 적용). N60 시점에 이 예외를 요청하는 테스트는
없다 — 외부로 나가던 2 회는 mock 으로 전환했다.
"""

from __future__ import annotations

import socket

import pytest

from clossify import mcp_server

# =========================================================================== #
# N60 — 외부 네트워크 차단 (autouse, 전역).
# =========================================================================== #

# 로컬로 허용하는 호스트명 집합. ``connect`` 의 인자가 호스트명 형태일 수도
# 있고 (host, port) 튜플일 수도 있으므로 양쪽을 모두 판정한다.
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


class ExternalNetworkBlockedError(RuntimeError):
    """테스트 중 외부 목적지로의 소켓 연결 시도가 차단되었다.

    메시지에 목적지와 사유를 담는다. 이 예외를 잡아 mock 응답으로 대체하면
    **실패가 성공으로 위장** 되므로(우리가 반복해 막아온 유형) 절대 잡지 마라.
    """

    def __init__(self, address):
        self.address = address
        super().__init__(
            f"테스트가 외부 네트워크로 나가려 했습니다: {address!r}. "
            f"테스트는 실호출 금지 — 외부 서버 상태·자격증명에 좌우되는 통과를 "
            f"만들지 마세요 (2026-08-08 네이버 403 사고 재발 방지, N60). "
            f"대상 테스트를 mock 으로 전환하거나, 정말 실호출이 필요하면 "
            f"``@pytest.mark.allow_external_network`` 마커를 명시적으로 붙이세요."
        )


def _is_local_address(address) -> bool:
    """연결 대상이 로컬 루프백인가.

    ``address`` 는 ``socket.connect`` 에 전달되는 값으로 (host, port) 튜플이
    일반적이지만, IPv6 의 경우 (host, port, flowinfo, scopeid) 일 수도 있다.
    host 가 문자열 호스트명(``localhost``) 이거나 ``127.*`` IPv4, ``::1`` IPv6
    이면 로컬로 본다.
    """
    if not address:
        return True
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return True
    if host in _LOCAL_HOSTNAMES:
        return True
    if host.startswith("127."):
        return True
    # IPv6 맵핑된 루프백 (예: ::ffff:127.0.0.1) 도 로컬로 본다.
    if host.lower().startswith("::ffff:127."):
        return True
    return False


def _install_socket_guard():
    """``socket.socket.connect`` / ``connect_ex`` 를 가드로 교체한다.

    외부 목적지 연결 시도 시 ``ExternalNetworkBlockedError`` 를 발생시킨다.
    로컬 연결(127.0.0.0/8, ::1, localhost)은 그대로 동작한다.

    본 함수는 멱등(idempotent)이다 — 이미 가드가 설치된 상태에서 다시
    호출해도 원본을 다시 덮어쓰지 않는다(이중 wrapping 방지).
    """
    if getattr(socket.socket.connect, "_clossify_guarded", False):
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address, *args, **kwargs):
        if not _is_local_address(address):
            raise ExternalNetworkBlockedError(address)
        return original_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_local_address(address):
            raise ExternalNetworkBlockedError(address)
        return original_connect_ex(self, address, *args, **kwargs)

    guarded_connect._clossify_guarded = True  # type: ignore[attr-defined]
    guarded_connect._clossify_original = original_connect  # type: ignore[attr-defined]
    guarded_connect_ex._clossify_guarded = True  # type: ignore[attr-defined]
    guarded_connect_ex._clossify_original = original_connect_ex  # type: ignore[attr-defined]

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]


def _uninstall_socket_guard():
    """가드를 제거하고 원본 ``connect`` / ``connect_ex`` 로 복원한다.

    pytest 종료 후 다른 코드가 socket 을 그대로 쓸 수 있게 하기 위함이다.
    가드가 설치되지 않았으면 아무 것도 하지 않는다.
    """
    if getattr(socket.socket.connect, "_clossify_guarded", False):
        socket.socket.connect = socket.socket.connect._clossify_original  # type: ignore[method-assign,attr-defined]
    if getattr(socket.socket.connect_ex, "_clossify_guarded", False):
        socket.socket.connect_ex = socket.socket.connect_ex._clossify_original  # type: ignore[method-assign,attr-defined]


# 세션 시작 시 가드 설치. autouse session 픽스처로 하지 않고 모듈 임포트
# 시점에 실행하는 이유: conftest 가 임포트되는 순간부터 모든 테스트가
# 보호되어야 하기 때문이다. (픽스처 setup 전에 이미 외부 호출이 나갈 수 있다.)
_install_socket_guard()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """세션 종료 시 가드를 복원한다 (후속 Python 코드 보호)."""
    _uninstall_socket_guard()


def pytest_configure(config):
    """``allow_external_network`` 마커를 등록한다.

    마커가 붙은 테스트는 실제 외부 호출이 필요함을 명시한다. CI 는
    ``-m "not allow_external_network"`` 로 기본 실행에서 제외한다.
    N60 시점에 이 마커를 사용하는 테스트는 없다.
    """
    config.addinivalue_line(
        "markers",
        "allow_external_network: 테스트가 외부 네트워크 실호출이 필요함 (기본 실행 제외). "
        "N60 — 명시적 예외 마커. 현재 사용처 없음.",
    )


def pytest_collection_modifyitems(config, items):
    """``allow_external_network`` 마커가 붙은 테스트를 능동적으로 건너뛴다.

    CI 가 ``-m`` 표현식을 붙이지 않더라도, 마커가 붙은 테스트는 자동으로
    스킵된다 — 마커 사용이 곧 "기본 실행에서 제외" 계약이다. 동시에 가드가
    켜져 있으므로, 마커 없이 외부 호출을 시도하면 명확한 예외로 실패한다.
    """
    skip_marker = pytest.mark.skip(
        reason=(
            "allow_external_network 마커 — 기본 실행에서 제외됨 (N60). "
            "실호출 테스트는 CI 의 network job 등에서 별도 실행하세요."
        )
    )
    for item in items:
        if item.get_closest_marker("allow_external_network") is not None:
            item.add_marker(skip_marker)


@pytest.fixture
def allow_external_network():
    """실제 외부 호출이 필요한 테스트를 위한 opt-in 픽스처.

    **N60 시점에 사용처가 없다.** 마커와 함께 쓰이며, 이 픽스처를 요청한
    테스트는 ``allow_external_network`` 마커를 스스로 가져야 한다(그렇지
    않으면 가드가 외부 연결을 차단한다). 본 픽스처는 마커 사용을 독려하기
    위한 명시적 신호일 뿐, 가드 자체를 끄지는 않는다.

    가드를 끄려면 ``socket_guard_off`` 픽스처를 쓴다(아래).
    """
    yield


@pytest.fixture
def socket_guard_off():
    """소켓 가드를 끄는 opt-in 픽스처 (위험 — 마지막 수단).

    본 픽스처는 ``allow_external_network`` 마커와 함께 쓰여야 한다. 그 외
    용도로 쓰면 N60 의 계약(테스트 외부 호출 차단) 을 우회하는 것이 된다.
    가드를 끄는 대신 **본 픽스처를 요청한 테스트는 ``pytest_unskip_external``
    마커도 함께 붙여야 자동 스킵을 풀 수 있다** — 복잡한 의존을 의도한 것으로,
    "정말로 외부 호출이 필요한가?" 를 한 번 더 묻는다.
    """
    _uninstall_socket_guard()
    yield
    _install_socket_guard()


# =========================================================================== #
# 기존 픽스처.
# =========================================================================== #


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
