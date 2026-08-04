"""등록 파이프라인 수정사항 검증 테스트.

이 테스트는 설정 중앙화, 업로드 검증, 이름 절삭 등 핵심 수정사항 각각에 대한
단위 테스트와 fail-closed stock, build_payload name cut, sanitization 강화에
대한 단위 테스트를 제공한다.
외부 API 호출, 네트워크, 실제 config 파일 의존성을 최소화하도록 설계되었다.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원: 프로젝트 루트를 path 에 추가.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client


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
            "origin_code": "05",
            "made_in": "한국",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
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
        # COMMERCE_DRY_RUN=1 모드에서도 컴플라이언스 게이트가 동작한다.
        # 따라서 테스트는 게이트를 진심으로 통과하는 페이로드를 제공해야 한다:
        #   - WEAR 카테고리(50021299, KC 불필요)
        #   - WEAR 고시 필수 13필드를 모두 채운 notice 오버라이드
        #   - 원산지/A/S 정보가 일치하는 _notice_config + common.cfg 목
        # 이 테스트의 본질은 상품명 50자 절삭 여부이며, 게이트 통과는
        # 리허설 모드에서도 동일하게 적용된다는 사실 그 자체다.
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        long_name = "A" * 80
        _notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "as_tel": "070-1234-5678",
            "manufacturer": "테스트제조사",
        }
        _common_cfg = {
            "smartstore_notice_defaults": {"origin_content": "중국"},
        }
        # WEAR 고시 본문 13개 필수필드를 플레이스홀더 없이 모두 채운다.
        _wear_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "returnCostReason": "단순변심 반품비용 구매자부담",
                "noRefundReason": "주문제작 청약철회 제한",
                "qualityAssuranceStandard": "관련법에 따름",
                "compensationProcedure": "소비자분쟁해결기준",
                "troubleShootingContents": "고객센터 문의",
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "manufacturer": "테스트제조사",
                "caution": "물 세탁 가능",
                "packDateText": "2026-01",
                "warrantyPolicy": "구매 후 7일 교환 가능",
                "afterServiceDirector": "테스트제조사 070-1234-5678",
            },
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_notice):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", lambda: _common_cfg):
                    result = mcp_server.register_product(
                        name=long_name,
                        price=10000,
                        image_urls=["http://x.png"],
                        category_id="50021299",
                        detail_html="<html></html>",
                        notice=_wear_notice,
                    )
        assert result["ok"] is True
        assert result.get("name_truncated") is True
        assert result.get("dry_run") is True

    def test_short_name_not_truncated(self, monkeypatch):
        # 게이트가 DRY_RUN 에서도 동작하므로 동일한 컴플라이언스 통과 조건을
        # 제공한다(자세한 사정은 test_long_name_truncated_in_dry_run 참고).
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _notice = {
            "origin_area_code": "04",
            "origin_content": "중국",
            "as_tel": "070-1234-5678",
            "manufacturer": "테스트제조사",
        }
        _common_cfg = {
            "smartstore_notice_defaults": {"origin_content": "중국"},
        }
        _wear_notice = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "returnCostReason": "단순변심 반품비용 구매자부담",
                "noRefundReason": "주문제작 청약철회 제한",
                "qualityAssuranceStandard": "관련법에 따름",
                "compensationProcedure": "소비자분쟁해결기준",
                "troubleShootingContents": "고객센터 문의",
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "manufacturer": "테스트제조사",
                "caution": "물 세탁 가능",
                "packDateText": "2026-01",
                "warrantyPolicy": "구매 후 7일 교환 가능",
                "afterServiceDirector": "테스트제조사 070-1234-5678",
            },
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_notice):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", lambda: _common_cfg):
                    result = mcp_server.register_product(
                        name="짧은이름",
                        price=10000,
                        image_urls=["http://x.png"],
                        category_id="50021299",
                        detail_html="<html></html>",
                        notice=_wear_notice,
                    )
        assert result["ok"] is True
        assert result.get("name_truncated") is False
        assert result.get("dry_run") is True

    def test_max_product_name_len_is_50(self):
        assert naver_client.MAX_PRODUCT_NAME_LEN == 50


# ============================================================================ #
# Fix 6 — Origin area code validation (fail-closed)
# ============================================================================ #
class TestFix6OriginAreaCode:
    """_resolve_origin_area_code 가 화이트리스트 밖 코드를 fail-closed 처리하는가."""

    def test_valid_code_accepted(self):
        # _resolve_origin_area_code 반환형이 문자열로 바뀌었다(코드만 반환).
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            code = naver_client._resolve_origin_area_code({"origin_code": "04"}, {})
        assert code == "04"

    def test_invalid_code_raises(self):
        # 화이트리스트 밖 코드는 "04" 폴백 없이 ValueError(fail-closed).
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with pytest.raises(ValueError):
                naver_client._resolve_origin_area_code({"origin_code": "ZZ"}, {})

    def test_empty_code_raises(self):
        # 빈 코드도 조용한 기본값 없이 ValueError(fail-closed).
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with pytest.raises(ValueError):
                naver_client._resolve_origin_area_code({}, {})

    def test_build_payload_uses_validated_code(self):
        # 유효한 코드는 그대로 payload 에 반영된다.
        p = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "origin_code": "05",
            "made_in": "한국",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        code = payload["originProduct"]["detailAttribute"]["originAreaInfo"]["originAreaCode"]
        assert code == "05"


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
            naver_client,
            "get_product",
            mock.Mock(side_effect=RuntimeError("token=sk-leaked-secret-key12345")),
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
# 검증 — 6 MCP tools registered
# ============================================================================ #
class TestToolRegistration:
    """MCP 서버가 정확히 6개의 도구를 등록했는가."""

    def test_six_tools_registered(self):
        tools = mcp_server.mcp.list_tools()
        # MCPServer.list_tools() 가 코루틴이면 async 로 실행.
        import asyncio

        if hasattr(tools, "__await__"):
            tools = (
                asyncio.get_event_loop().run_until_complete(tools)
                if not asyncio.iscoroutinefunction(mcp_server.mcp.list_tools)
                else asyncio.run(mcp_server.mcp.list_tools())
            )
        # list_tools 의 반환형이 list[MCPTool] 또는 유사 객체.
        # 도구 이름들을 추출.
        names = []
        for t in tools:
            name = getattr(t, "name", None) or getattr(t, "name", None)
            if name:
                names.append(name)
        # 6개 도구: check_config, upload_images, register_product, get_product,
        # prepare_listing, submit_reviews
        assert len(tools) == 6, f"Expected 6 tools, got {len(tools)}: {names}"


# ============================================================================ #
# Fail-closed option stock (Counterexample 1)
# ============================================================================ #
class TestOptionStockFailClosed:
    """_option_stock 이 누락/불가 stock 에 대해 ValueError 를 발생시키는가.

    이전 버전은 stock 이 없거나 파싱 불가능할 때 가짜 기본값 99 를
    조용히 반환했다. 이는 재고 0 인 상품을 99개 있는 것처럼 등록하는
    심각한 결함이다. 이를 fail-closed 로 수정한다.
    """

    def test_missing_stock_raises_value_error(self):
        """counterexample: _option_stock({}) -> before: 99, after: ValueError"""
        with pytest.raises(ValueError):
            naver_client._option_stock({})

    def test_bad_stock_string_raises_value_error(self):
        """counterexample: _option_stock({"stock":"bad"}) -> before: 99, after: ValueError"""
        with pytest.raises(ValueError):
            naver_client._option_stock({"stock": "bad"})

    def test_valid_stock_int_returns_value(self):
        assert naver_client._option_stock({"stock": 3}) == 3

    def test_valid_stock_string_int_returns_value(self):
        assert naver_client._option_stock({"stockQuantity": "5"}) == 5

    def test_none_stock_raises_value_error(self):
        with pytest.raises(ValueError):
            naver_client._option_stock({"stock": None})

    def test_build_payload_propagates_stock_error(self):
        """build_payload 가 옵션 재고 누락 시 ValueError 를 전파하는가."""
        p = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "options": [{"name": "블랙", "price": 0}],  # stock 누락
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with pytest.raises(ValueError):
                naver_client.build_payload(p, "<html></html>", ["http://x.png"])

    def test_build_payload_stock_zero_is_valid(self):
        """stock=0 은 유효한 값이다 (ValueError 가 아님)."""
        p = {
            "name": "테스트",
            "categoryId": "50002366",
            "salePrice": 5000,
            "origin_code": "05",
            "made_in": "한국",
            "options": [{"name": "블랙", "stock": 0, "price": 0}],
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        assert payload["originProduct"]["stockQuantity"] == 0

    def test_mcp_register_catches_stock_error_sanitized(self, monkeypatch):
        """MCP register_product 가 stock ValueError 를 sanitized 에러로 변환."""
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        result = mcp_server.register_product(
            name="테스트",
            price=10000,
            image_urls=["http://x.png"],
            category_id="50002366",
            detail_html="<html></html>",
            options=[{"name": "블랙", "price": 0}],  # stock 누락
        )
        assert result["ok"] is False
        assert "error" in result
        assert result["error"] is not None


# ============================================================================ #
# build_payload name 50-char enforcement (Counterexample 2)
# ============================================================================ #
class TestBuildPayloadNameCut:
    """build_payload 가 50자 초과 상품명을 자르는가.

    mcp_server.register_product 에서는 이미 자르고 있었지만,
    build_payload 를 직접 호출하는 경로에서는 50자 초과 이름이
    그대로 payload 에 들어가 API 400 거절을 유발했다.
    """

    def test_build_payload_truncates_long_name(self):
        """counterexample: 60-char name -> before: len=60, after: len=50"""
        p = {
            "name": "A" * 60,
            "categoryId": "50002366",
            "salePrice": 10000,
            "origin_code": "05",
            "made_in": "한국",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        name = payload["originProduct"]["name"]
        assert len(name) == 50

    def test_build_payload_short_name_unchanged(self):
        p = {
            "name": "짧은이름",
            "categoryId": "50002366",
            "salePrice": 10000,
            "origin_code": "05",
            "made_in": "한국",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        assert payload["originProduct"]["name"] == "짧은이름"

    def test_build_payload_exact_50_chars(self):
        p = {
            "name": "B" * 50,
            "categoryId": "50002366",
            "salePrice": 10000,
            "origin_code": "05",
            "made_in": "한국",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        assert len(payload["originProduct"]["name"]) == 50

    def test_build_payload_51_chars_truncated_to_50(self):
        p = {
            "name": "C" * 51,
            "categoryId": "50002366",
            "salePrice": 10000,
            "origin_code": "05",
            "made_in": "한국",
        }
        with mock.patch.object(naver_client, "_notice_config", return_value={}):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(p, "<html></html>", ["http://x.png"])
        assert len(payload["originProduct"]["name"]) == 50


# ============================================================================ #
# Sanitization strengthening (Counterexample 3)
# ============================================================================ #
class TestSanitizationStrengthened:
    """_sanitize_text 가 POSIX 경로, key=value 시크릿, traceback 을 마스킹하는가.

    이전 버전은 Windows C:\\Users\\.. 경로와 /home/user/ 경로만 매칭했고,
    POSIX 시스템 경로(/etc/, /var/), key=value 형태 시크릿, traceback
    헤더를 누락했다.
    """

    def test_sanitize_posix_etc_path(self):
        """counterexample: /etc/passwd -> before: leaked, after: [REDACTED]"""
        sanitized = mcp_server._sanitize_text("error in /etc/passwd file")
        assert "/etc/passwd" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_posix_var_path(self):
        sanitized = mcp_server._sanitize_text("error in /var/log/app.log")
        assert "/var/log/app.log" not in sanitized

    def test_sanitize_api_key_equals(self):
        """counterexample: api_key=abcd1234 -> before: leaked, after: [REDACTED]"""
        sanitized = mcp_server._sanitize_text("config: api_key=abcd1234efgh5678")
        assert "abcd1234efgh5678" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_client_secret_colon(self):
        """counterexample: client_secret: xyz -> before: leaked, after: [REDACTED]"""
        sanitized = mcp_server._sanitize_text("client_secret: xyz123abc456")
        assert "xyz123abc456" not in sanitized

    def test_sanitize_token_equals(self):
        sanitized = mcp_server._sanitize_text("token=abcd1234efgh5678")
        assert "abcd1234efgh5678" not in sanitized

    def test_sanitize_password_equals(self):
        sanitized = mcp_server._sanitize_text("password=mypassword123")
        assert "mypassword123" not in sanitized

    def test_sanitize_traceback_header(self):
        """counterexample: Traceback header -> before: leaked, after: [REDACTED]"""
        sanitized = mcp_server._sanitize_text("Traceback (most recent call last)")
        assert "Traceback" not in sanitized

    def test_sanitize_file_line_frame(self):
        file_line = '  File "C:\\src\\app.py", line 42, in run'
        sanitized = mcp_server._sanitize_text(file_line)
        assert 'File "C:' not in sanitized

    def test_sanitize_windows_private_path(self):
        """counterexample: H:\\private\\secret.json -> before: leaked, after: [REDACTED]"""
        path = "H:" + chr(92) + "private" + chr(92) + "secret.json"
        sanitized = mcp_server._sanitize_text(f"file at {path}")
        assert path not in sanitized

    def test_sanitize_short_secret_value_not_triggered(self):
        """4자 이하 값은 매칭하지 않음(과잉 마스킹 방지)."""
        sanitized = mcp_server._sanitize_text("api_key=abc")
        # 3 char value < 5 threshold -> not matched
        assert sanitized == "api_key=abc"

    def test_sanitize_body_prunes_error_response(self):
        """_sanitize_body 가 에러 응답에서 화이트리스트 키만 남기는가."""
        body = {
            "code": "BAD_REQUEST",
            "message": "Invalid name",
            "invalidInputs": [{"name": "originProduct.name", "type": "Length"}],
            "internalDebugInfo": "secret_token=sk-leaked12345678",
            "requestId": "req-abc-123",
        }
        pruned = mcp_server._sanitize_body(body)
        assert "code" in pruned
        assert "message" in pruned
        assert "invalidInputs" in pruned
        assert "internalDebugInfo" not in pruned
        assert "requestId" not in pruned

    def test_sanitize_body_preserves_ok_response(self):
        """200 OK 응답은 가지치기하지 않음."""
        body = {"originProductNo": "123", "extra": {"detail": "ok"}}
        result = mcp_server._sanitize_body(body)
        assert result == body

    def test_sanitize_body_nested_string_sanitized(self):
        """에러 응답 내 문자열 값도 _sanitize_text 통과."""
        body = {
            "code": "ERROR",
            "message": "failed at /etc/passwd",
        }
        pruned = mcp_server._sanitize_body(body)
        assert "/etc/passwd" not in pruned["message"]
