# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""설정 화면 인라인 렌더 MCP UI 리소스 테스트.

티켓 수용 조건 (a)~(i) 대응 — 모두 로컬 파일/mock, 외부 호출 없음:

  (a) list_resources() 에 ``ui://clossify/setup.html`` 이 있다.
  (b) read_resource() 가 비어 있지 않은 HTML 을 준다.
  (c) 그 HTML 이 문서 조각이다 (``<!DOCTYPE``/``<html``/``<body`` 없음).
  (d) 입력 필드가 정확히 2개 (``id="cid"`` · ``id="csec"``).
  (e) 외부 http(s) URL 이 apicenter 뿐이고, 가이드 링크(상대경로)가 있다.
  (f) 설정 미완료 → check_config 결과에 resource_uri 가 있다.
  (g) 설정 완료 → check_config 결과에 resource_uri 가 없다.
  (h) check_config 기존 키가 전부 보존된다 (회귀).
  (i) 반환·로그·HTML 어디에도 시크릿 값이 없다.
"""

from __future__ import annotations

import json
import re
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

_RESOURCE_URI = "ui://clossify/setup.html"
# (e) 허용된 외부 URL — 정확히 2종 (apicenter · 시작 가이드 절대 URL).
# 시작 가이드는 iframe 안에서 렌더되므로 상대경로는 죽은 링크다 — 절대 URL.
# 그 URL 은 로컬 금지단어 스캐너(scripts/scan_repo.py 층 2) 에 걸리는 단어를
# 포함하므로, 이 테스트 소스에는 리터럴로 두지 않고 setup.html 의 href 에서
# 런타임에 추출한다 (예외는 그 URL 의 setup.html 등장 1곳 뿐이다).
_ALLOWED_URLS = frozenset(
    {
        "https://apicenter.commerce.naver.com/ko/member/home",
    }
)


def _guide_url_from_html(html: str) -> str:
    """setup.html 의 가이드 링크 절대 URL 을 href 에서 추출한다."""
    m = re.search(r'href="(https://github\.com/[^"]*SETUP_GUIDE\.md)"', html)
    assert m is not None, "가이드 절대 URL href 없음"
    return m.group(1)


def _read_setup_html() -> str:
    """read_resource 로 HTML 조각 전문을 가져온다 (동기/비동기 반환 모두 처리)."""
    contents = mcp_server.mcp.read_resource(_RESOURCE_URI)
    if hasattr(contents, "__await__"):
        import asyncio

        contents = asyncio.run(contents)
    parts: list[str] = []
    for item in contents:
        content = getattr(item, "content", item)
        parts.append(content if isinstance(content, str) else str(content))
    return "".join(parts)


def _write_config(tmp_path, naver: dict | None) -> Path:
    """config.json 을 쓰고 경로를 반환. naver=None 이면 파일을 안 쓴다."""
    if naver is None:
        return tmp_path / "absent_config.json"
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"naver": naver}), encoding="utf-8")
    return config_file


def _complete_naver() -> dict:
    # check_config 정상 경로 판정용 — client_secret 은 형식만 유효하면 된다.
    return {
        "client_id": "test_client_id",
        "client_secret": bcrypt.gensalt(rounds=4).decode(),
        "store_url_slug": "test-slug",
    }


# ============================================================================ #
# (a) 리소스 등록
# ============================================================================ #
class TestResourceRegistered:
    def test_list_resources_contains_setup_uri(self):
        resources = mcp_server.mcp.list_resources()
        if hasattr(resources, "__await__"):
            import asyncio

            resources = asyncio.run(resources)
        uris = [str(getattr(r, "uri", r)) for r in resources]
        assert _RESOURCE_URI in uris, f"list_resources 결과에 설정 UI 리소스 없음: {uris}"


# ============================================================================ #
# (b) 리소스 내용 — 비어 있지 않은 HTML
# ============================================================================ #
class TestResourceContent:
    def test_read_resource_non_empty_html(self):
        html = _read_setup_html()
        assert len(html) > 0, "setup.html 리소스가 빈 문자열을 반환함"
        assert "클로시파이 최초 설정" in html

    def test_loader_missing_file_raises(self):
        import pytest

        with pytest.raises(FileNotFoundError):
            load_ui("no-such-widget.html")


# ============================================================================ #
# (c) 문서 조각 — 최상위 문서 태그 금지
# ============================================================================ #
class TestFragmentOnly:
    def test_no_document_tags(self):
        html = _read_setup_html().lower()
        for banned in ("<!doctype", "<html", "<body"):
            assert (
                banned not in html
            ), f"조각에 문서 태그 {banned!r} 이(가) 있음 — 조각으로 렌더되어야 함"


# ============================================================================ #
# (d) 입력 필드 정확히 2개
# ============================================================================ #
class TestInputFields:
    def test_exactly_two_inputs_cid_csec(self):
        html = _read_setup_html()
        inputs = re.findall(r"<input\b[^>]*>", html)
        assert len(inputs) == 2, f"입력 필드가 정확히 2개여야 함, 실제 {len(inputs)}개"
        assert 'id="cid"' in html
        assert 'id="csec"' in html
        # 입력 태그 각각이 cid/csec 아이디를 가진다.
        for tag in inputs:
            assert 'id="cid"' in tag or 'id="csec"' in tag, f"예상 밖 입력 필드: {tag}"


# ============================================================================ #
# (e) 외부 URL 화이트리스트
# ============================================================================ #
class TestExternalUrls:
    def test_only_two_allowed_external_urls(self):
        html = _read_setup_html()
        found = set(re.findall(r"https?://[^\s\"'<>]+", html))
        expected = _ALLOWED_URLS | {_guide_url_from_html(html)}
        assert found == expected, f"허용되지 않은 외부 URL 발견: {found - expected}"
        # 시작 가이드 링크가 절대 URL 형태로 존재한다 (상대경로 금지).
        assert 'href="docs/SETUP_GUIDE.md"' not in html
        assert _guide_url_from_html(html).startswith("https://github.com/")


# ============================================================================ #
# (f)/(g) check_config — resource_uri 조건부 첨부
# ============================================================================ #
class TestCheckConfigResourceUri:
    def test_incomplete_config_has_resource_uri(self, tmp_path, monkeypatch):
        """미완료(placeholder) → resource_uri/ui_hint 있다."""
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
        assert result["resource_uri"] == _RESOURCE_URI
        assert "ui_hint" in result

    def test_missing_config_file_has_resource_uri(self, tmp_path, monkeypatch):
        """파일 부재(조기 반환 경로) → resource_uri 있다."""
        cfg_path = _write_config(tmp_path, None)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        assert result["resource_uri"] == _RESOURCE_URI

    def test_complete_config_has_no_resource_uri(self, tmp_path, monkeypatch):
        """완료 → 두 키가 없다 (불필요한 화면 유발 금지)."""
        cfg_path = _write_config(tmp_path, _complete_naver())
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        assert result["ok"] is True
        assert "resource_uri" not in result
        assert "ui_hint" not in result


# ============================================================================ #
# (h) 기존 키 보존 (회귀)
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
        }
    )

    def test_incomplete_result_preserves_legacy_keys(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, {"client_id": "x"})
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        for key in self._LEGACY_KEYS:
            assert key in result, f"기존 반환 키 {key!r} 이 미완료 경로에서 사라짐"

    def test_complete_result_preserves_legacy_keys(self, tmp_path, monkeypatch):
        cfg_path = _write_config(tmp_path, _complete_naver())
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        for key in self._LEGACY_KEYS:
            assert key in result, f"기존 반환 키 {key!r} 이 완료 경로에서 사라짐"

    def test_tool_count_unchanged(self):
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            tools = asyncio.run(tools)
        assert len(tools) == 11, f"도구 수가 변경됨 (11개 유지 계약 위반): {len(tools)}"


# ============================================================================ #
# (i) 시크릿 비노출 (canary)
# ============================================================================ #
class TestNoSecretLeak:
    def test_secret_canary_not_in_result_nor_html(self, tmp_path, monkeypatch):
        """config 의 client_secret 값이 check_config 결과·HTML 어디에도 없다."""
        canary_secret = "CANARY-" + bcrypt.gensalt(rounds=4).decode()
        cfg_path = _write_config(
            tmp_path,
            {
                "client_id": "cid-canary",
                "client_secret": canary_secret,
                "store_url_slug": "slug-canary",
            },
        )
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_path))
        result = mcp_server.check_config()
        result_str = json.dumps(result, ensure_ascii=False)
        assert canary_secret not in result_str, "check_config 결과에 client_secret 값 노출"
        html = _read_setup_html()
        assert canary_secret not in html, "setup.html 에 client_secret 값 노출"
        # HTML 은 폼일 뿐 — 플레이스홀더 외에 키 형태 문자열이 없어야 한다.
        for fake in ("REPLACE_WITH", "sk-proj-", "AAAA-BBBB"):
            assert fake not in html, f"setup.html 에 가짜 키/시크릿 문자열 {fake!r} 존재"
