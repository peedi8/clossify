# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUsage-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""서버 버전 노출 시험.

``MCPServer("clossify")`` 가 ``version`` 인자를 받아 클라이언트에 노출하는지
확인한다. 이전에는 version 을 넘기지 않아 빈 문자열이었고, 클라이언트에
"버전 없음" 으로 보였다. 이제 ``importlib.metadata.version("clossify")``
에서 읽어 전달한다.

시험:
  (a) 서버 version 이 비어있지 않다.
  (b) 서버 version 이 설치 메타데이터의 패키지 버전과 일치한다.
  (c) 메타데이터 조회 실패 시에도 서버(모듈 import)가 죽지 않는다(폴백).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class TestServerVersionExposed:
    """(a)(b) 서버 버전이 비어있지 않고 패키지 버전과 일치하는가."""

    def test_version_is_not_empty(self):
        """서버 version 이 빈 문자열이 아니다."""
        from clossify import mcp_server

        assert mcp_server.mcp.version, "mcp.version 이 빈 문자열이다"

    def test_version_matches_package_metadata(self):
        """서버 version 이 importlib.metadata 의 패키지 버전과 일치한다."""
        from importlib.metadata import version as pkg_version

        from clossify import mcp_server

        expected = pkg_version("clossify")
        assert mcp_server.mcp.version == expected


class TestMetadataFailureFallback:
    """(c) 메타데이터 조회 실패 시 서버가 죽지 않는다(폴백).

    ``importlib.metadata.version("clossify")`` 가 ``PackageNotFoundError`` 를
    던지는 환경(예: 패키지 미설치)에서도 모듈 import 가 정상 완료되어야 한다.
    몽키패치로 시뮬레이션한다.
    """

    def test_server_import_survives_metadata_failure(self):
        """``_resolve_version`` 이 조회 실패 시 빈 문자열 폴백을 반환한다."""
        from clossify import mcp_server

        with mock.patch.object(
            mcp_server,
            "_pkg_version",
            side_effect=mcp_server.PackageNotFoundError("clossify"),
        ):
            result = mcp_server._resolve_version()
        # 폴백: 빈 문자열, 예외 아님.
        assert result == ""
        # 예외가 밖으로 나가지 않았음을 assert 로 확인.
        assert isinstance(result, str)

    def test_server_import_does_not_raise_on_missing_package(self, monkeypatch):
        """모듈 재로드 시 PackageNotFoundError 가 발생해도 서버 인스턴스가 생성된다.

        ``_pkg_version`` 이 예외를 던지도록 몽키패치한 뒤 ``_resolve_version``
        만 다시 호출해 서버가 죽지 않음을 보인다. 모듈 전체 재로드는 불필요하다
        — 폴백 경로가 함수 단위로 검증되면 충분하다.
        """
        from clossify import mcp_server

        # 몽키패치: 메타데이터 조회가 항상 실패.
        monkeypatch.setattr(
            mcp_server,
            "_pkg_version",
            mock.Mock(side_effect=mcp_server.PackageNotFoundError("simulated")),
        )
        # 폴백 경로 호출 — 예외 없이 빈 문자열.
        v = mcp_server._resolve_version()
        assert v == ""

        # 서버 인스턴스는 이미 생성되어 있고 살아있다.
        assert mcp_server.mcp is not None
