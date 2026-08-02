# -*- coding: utf-8 -*-
"""T-105 수정사항 검증 테스트.

이 테스트는 8개의 수정사항 각각에 대해 단위 테스트를 제공한다.
외부 API 호출, 네트워크, 실제 config 파일 의존성을 최소화하도록 설계되었다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원: 프로젝트 루트를 path 에 추가.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client, mcp_server  # noqa: E402


# ============================================================================ #
# Fix 3 — Config loading centralization (CLOSSIFY_CONFIG env)
# ============================================================================ #
class TestFix3ConfigCentralization:
    """naver_client.resolve_config_path() 가 CLOSSIFY_CONFIG 환경변수를 존중하는가."""

    def test_default_path_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("CLOSSIFY_CONFIG", raising=False)
        path = naver_client.resolve_config_path()
        assert path.endswith(os.path.join(".local", "config.json"))

    def test_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom_config.json"
        custom.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(custom))
        assert os.path.normpath(str(custom)) == naver_client.resolve_config_path()

    def test_config_path_alias_matches_resolve(self, monkeypatch):
        monkeypatch.delenv("CLOSSIFY_CONFIG", raising=False)
        assert naver_client.config_path() == naver_client.resolve_config_path()

    def test_load_config_uses_env_path(self, monkeypatch, tmp_path):
        cfg = {"naver": {"client_id": "x"}, "test_marker": 42}
        custom = tmp_path / "env_cfg.json"
        custom.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(custom))
        loaded = naver_client.load_config()
        assert loaded["test_marker"] == 42


# ============================================================================ #
# Fix 4 — Importer field bug
# ============================================================================ #
class TestFix4ImporterField:
    """build_payload 의 originAreaInfo.importer 가 defaults['importer'] 인지."""

    def test_importer_uses_defaults_importer_not_manufacturer(self):
        p = {
            "name": "테스트상품",
            "categoryId": "50002366",
            "salePrice": 10000,
            "importer": "테스트수입사",
            "manufacturer": "테스트제조사",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x/img.png"])
        origin_area = payload["originProduct"]["detailAttribute"]["originAreaInfo"]
        assert origin_area["importer"] == "테스트수입사"
        assert origin_area["importer"] != "테스트제조사"


# ============================================================================ #
# Fix 5 — Name 50-char truncation
# ============================================================================ #
class TestFix5NameTruncation:
    """register_product 가 50자 초과 상품명을 자르고 name_truncated=True 를 반환하는가."""

    def test_long_name_truncated_in_dry_run(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        long_name = "A" * 80
        result = mcp_server.register_product(
            name=long_name,
            price=10000,
            image_urls=["http://x.png"],
            category_id="50002366",
            detail_html="<html></html>",
        )
        assert result["ok"] is True
        assert result.get("name_truncated") is True

    def test_short_name_not_truncated(self, monkeypatch):
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        result = mcp_server.register_product(
            name="짧은이름",
            price=10000,
            image_urls=["http://x.png"],
            category_id="50002366",
            detail_html="<html></html>",
        )
        assert result["ok"] is True
        assert result.get("name_truncated") is False

    def test_max_product_name_len_is_50(self):
        assert naver_client.MAX_PRODUCT_NAME_LEN == 50


# ============================================================================ #
# Fix 6 — Origin area code validation (fail-closed)
# ============================================================================ #
class TestFix6OriginAreaCode:
    """_resolve_origin_area_code 가 화이트리스트 밖 코드를 fail-closed 처리하는가."""

    def test_valid_code_accepted(self):
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            code, ok = naver_client._resolve_origin_area_code(
                {"origin_code": "04"}, {}
            )
        assert code == "04"
        assert ok is True

    def test_invalid_code_falls_back_to_04(self):
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            code, ok = naver_client._resolve_origin_area_code(
                {"origin_code": "ZZ"}, {}
            )
        assert code == "04"
        assert ok is False

    def test_empty_code_falls_back_to_04(self):
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            code, ok = naver_client._resolve_origin_area_code({}, {})
        assert code == "04"
        assert ok is True  # default "04" is in whitelist

    def test_build_payload_uses_validated_code(self):
        p = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "origin_code": "99",  # invalid
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        code = payload["originProduct"]["detailAttribute"]["originAreaInfo"]["originAreaCode"]
        assert code == "04"  # fail-closed


# ============================================================================ #
# Fix 2 — upload_images robustness (context manager + MIME detection)
# ============================================================================ #
class TestFix2UploadRobustness:
    """upload_images 가 파일 핸들을 안전하게 닫고 MIME 을 추정하는가."""

    def test_guess_image_mime_jpg(self):
        assert naver_client._guess_image_mime("photo.jpg") == "image/jpeg"

    def test_guess_image_mime_png(self):
        assert naver_client._guess_image_mime("photo.png") == "image/png"

    def test_guess_image_mime_webp(self):
        assert naver_client._guess_image_mime("photo.webp") == "image/webp"

    def test_guess_image_mime_unknown(self):
        assert naver_client._guess_image_mime("file.xyz") == "application/octet-stream"

    def test_upload_closes_file_handles_on_success(self, monkeypatch):
        closed = []

        class FakeFile:
            def __init__(self, path):
                self.path = path

            def close(self):
                closed.append(self.path)

        def fake_open(path, mode):
            return FakeFile(path)

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"images": [{"url": "http://cdn/x.png"}]}

            @property
            def headers(self):
                return {}

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(naver_client.requests, "post", lambda *a, **kw: FakeResponse())
        monkeypatch.setattr(naver_client, "get_token", lambda: "fake_token")

        naver_client.upload_images(["a.png"])
        assert len(closed) == 1

    def test_upload_closes_file_handles_on_error(self, monkeypatch):
        closed = []

        class FakeFile:
            def __init__(self, path):
                self.path = path

            def close(self):
                closed.append(self.path)

        def fake_open(path, mode):
            return FakeFile(path)

        def boom(*a, **kw):
            raise RuntimeError("network down")

        monkeypatch.setattr("builtins.open", fake_open)
        monkeypatch.setattr(naver_client.requests, "post", boom)
        monkeypatch.setattr(naver_client, "get_token", lambda: "fake_token")

        with pytest.raises(RuntimeError):
            naver_client.upload_images(["a.png", "b.png"])
        assert len(closed) == 2


# ============================================================================ #
# Fix 1 — Image upload validation (extension/size/CLOSSIFY_UPLOAD_ROOT)
# ============================================================================ #
class TestFix1UploadValidation:
    """mcp_server.upload_images 가 확장자/크기/경로를 검증하는가."""

    def test_rejects_empty_paths(self):
        result = mcp_server.upload_images([])
        assert result["ok"] is False
        assert "paths" in result["error"]

    def test_rejects_bad_extension(self, tmp_path):
        bad = tmp_path / "file.txt"
        bad.write_text("not an image")
        result = mcp_server.upload_images([str(bad)])
        assert result["ok"] is False
        assert "확장자" in result["error"]

    def test_accepts_valid_extensions(self, tmp_path):
        ok_file = tmp_path / "photo.png"
        ok_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        # naver_client.upload_images 를 mock 하여 실제 업로드 회피.
        with mock.patch.object(naver_client, "upload_images", return_value=["http://cdn/x.png"]):
            with mock.patch.object(naver_client, "get_token", return_value="t"):
                result = mcp_server.upload_images([str(ok_file)])
        assert result["ok"] is True

    def test_rejects_oversized_file(self, tmp_path, monkeypatch):
        big = tmp_path / "big.png"
        # _MAX_IMAGE_BYTES 보다 큰 파일로 스텁.
        monkeypatch.setattr(mcp_server, "_MAX_IMAGE_BYTES", 10)
        big.write_bytes(b"\x00" * 100)
        result = mcp_server.upload_images([str(big)])
        assert result["ok"] is False
        assert "크기" in result["error"] or "초과" in result["error"]

    def test_clossify_upload_root_relative_resolution(self, monkeypatch, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        monkeypatch.setenv("CLOSSIFY_UPLOAD_ROOT", str(tmp_path))
        resolved = mcp_server._resolve_upload_path("img.png")
        assert os.path.normpath(resolved) == os.path.normpath(str(img))

    def test_nonexistent_file_rejected(self):
        result = mcp_server.upload_images(["/nonexistent/xyz.png"])
        assert result["ok"] is False
        assert "존재하지 않는" in result["error"]


# ============================================================================ #
# Fix 7 — Error sanitization
# ============================================================================ #
class TestFix7ErrorSanitization:
    """mcp_server 가 민감 정보를 마스킹하는가."""

    def test_sanitize_text_removes_openai_key(self):
        text = "error with key sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
        sanitized = mcp_server._sanitize_text(text)
        assert "sk-proj-" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_text_removes_bearer_token(self):
        text = "Auth: Bearer abcdef1234567890"
        sanitized = mcp_server._sanitize_text(text)
        assert "Bearer abcdef" not in sanitized

    def test_sanitize_error_returns_type_and_message(self):
        exc = ValueError("something sk-proj-badkey1234567890")
        sanitized = mcp_server._sanitize_error(exc)
        assert sanitized.startswith("ValueError:")
        assert "sk-proj-" not in sanitized

    def test_get_product_error_sanitized(self, monkeypatch):
        monkeypatch.delenv("CLOSSIFY_CONFIG", raising=False)
        monkeypatch.setattr(
            naver_client, "get_product",
            mock.Mock(side_effect=RuntimeError("token=sk-leaked-secret-key12345"))
        )
        result = mcp_server.get_product("123")
        assert result["ok"] is False
        assert "sk-leaked" not in result["error"]


# ============================================================================ #
# Fix 8 — Packaging (agents + config.example.json in wheel)
# ============================================================================ #
class TestFix8Packaging:
    """pyproject.toml wheel 타겟이 agents 와 config.example.json 을 포함하는가."""

    def test_wheel_force_include_present(self):
        toml_path = _PROJECT_ROOT / "pyproject.toml"
        content = toml_path.read_text(encoding="utf-8")
        assert "force-include" in content
        assert "agents" in content
        assert "config.example.json" in content

    def test_agents_directory_has_nine_files(self):
        agents_dir = _PROJECT_ROOT / "agents"
        md_files = list(agents_dir.glob("*.md"))
        assert len(md_files) == 9, f"Expected 9 agent .md files, found {len(md_files)}"

    def test_config_example_json_exists(self):
        assert (_PROJECT_ROOT / "config.example.json").is_file()


# ============================================================================ #
# Acceptance — 4 MCP tools registered
# ============================================================================ #
class TestAcceptanceTools:
    """MCP 서버가 정확히 4개의 도구를 등록했는가."""

    def test_four_tools_registered(self):
        tools = mcp_server.mcp.list_tools()
        # MCPServer.list_tools() 가 코루틴이면 async 로 실행.
        import asyncio
        if hasattr(tools, "__await__"):
            tools = asyncio.get_event_loop().run_until_complete(tools) \
                if not asyncio.iscoroutinefunction(mcp_server.mcp.list_tools) \
                else asyncio.run(mcp_server.mcp.list_tools())
        # list_tools 의 반환형이 list[MCPTool] 또는 유사 객체.
        # 도구 이름들을 추출.
        names = []
        for t in tools:
            name = getattr(t, "name", None) or getattr(t, "name", None)
            if name:
                names.append(name)
        # 4개 도구: check_config, upload_images, register_product, get_product
        assert len(tools) == 4, f"Expected 4 tools, got {len(tools)}: {names}"
