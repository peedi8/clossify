# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""정책 자동 읽기 온보딩 검증 (check_config 의 read_existing 경로).

검증 시나리오 (티켓 계약):
  (a) 기본 호출(read_existing 미지정)은 외부 API 호출 0회, 반환 형태는 기존과 호환.
  (b) read_existing=True + mock 상품 1건 → 정책값이 출처와 함께 제안된다.
  (c) 상품 0개 → 제안이 비어 있고 그 사실이 반환에 드러난다.
  (d) 설정에 값이 있고 기존 상품과 다르면 차이가 보고되고 설정 파일은 변경되지 않는다.
  (e) 검색 실패해도 기존 진단 키들은 정상 반환된다.
  (f) 어떤 경로에서도 check_config 가 설정 파일을 쓰지 않는다.

모든 테스트는 COMMERCE_DRY_RUN 을 끈 상태에서 HTTP 를 mock 한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client

# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #

# 필수 자격증명이 모두 채워진 정상 config (플레이스홀더 아님).
_BASE_CFG = {
    "naver": {
        "client_id": "real-id",
        "client_secret": "real-secret",
        "store_url_slug": "real-slug",
    },
    "smartstore_notice_defaults": {
        # 정책값은 비어 있다 — 온보딩이 제안할 자리.
        "origin_area_code": "",
        "origin_content": "",
        "as_tel": "",
        "as_guide": "",
        "manufacturer": "",
        "importer": "",
        "returnCostReason": "",
        "noRefundReason": "",
        "qualityAssuranceStandard": "",
        "compensationProcedure": "",
        "troubleShootingContents": "",
    },
}


def _write_cfg(tmp_path: Path, cfg: dict) -> Path:
    """임시 config 파일을 쓰고 경로를 반환. check_config 가 읽게 한다."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return cfg_file


# 기존 상품 1건의 get_product 응답 본문 (정책값이 모두 채워져 있음).
_PRODUCT_BODY_WITH_POLICIES = {
    "originProduct": {
        "originProductNo": "existing-001",
        "name": "테스트 상품",
        "detailAttribute": {
            "afterServiceInfo": {
                "afterServiceTelephoneNumber": "070-9999-8888",
                "afterServiceGuideContent": "A/S는 고객센터로 문의해주세요",
            },
            "originAreaInfo": {
                "originAreaCode": "04",
                "content": "중국",
                "importer": "테스트수입사",
            },
            "productInfoProvidedNotice": {
                "productInfoProvidedNoticeType": "ETC",
                "etc": {
                    "manufacturer": "테스트제조사",
                    "returnCostReason": "단순변심 반품비 구매자부담",
                    "noRefundReason": "주문제작 청약철회 불가",
                    "qualityAssuranceStandard": "관련 법령에 따름",
                    "compensationProcedure": "소비자분쟁해결기준",
                    "troubleShootingContents": "고객센터 문의",
                },
            },
        },
    },
}


def _mock_search_one_product(*args, **kwargs):
    """search_products 가 상품 1건을 반환하도록 mock."""
    return 200, {"products": [{"originProductNo": "existing-001", "name": "테스트 상품"}]}


def _mock_search_no_products(*args, **kwargs):
    """search_products 가 빈 목록(신규 셀러)을 반환하도록 mock."""
    return 200, {"products": []}


def _mock_get_product_with_policies(*args, **kwargs):
    """get_product 가 정책값이 채워진 상품을 반환하도록 mock."""
    return 200, _PRODUCT_BODY_WITH_POLICIES


def _mock_search_failure(*args, **kwargs):
    """search_products 가 실패(권한/네트워크)하도록 mock."""
    return 403, {"message": "forbidden"}


# --------------------------------------------------------------------------- #
# 실측 응답 형태 (2026-08-05 녹화) — search_products 의 실제 응답 본문.
# 과거에 ``products`` 라고 추측해 읽던 자리와 달리, 본 응답은 상품 목록을
# ``contents`` 에 담고 각 항목의 채널 수준값은 ``channelProducts`` 배열 안에
# 중첩한다. 다음 스키마 회귀는 이 고정된 형태에 대해 측정한다.
# (번호는 녹음 그대로 정수 — 셀러 실제 상품번호 아님, 응답 형태 예시.)
# --------------------------------------------------------------------------- #
_LIVE_SEARCH_SHAPE_ONE = {
    "contents": [
        {
            "originProductNo": 13638045156,
            "channelProducts": [
                {
                    "channelProductNo": 13698323110,
                    "name": "테스트 상품",
                    "statusType": "SALE",
                }
            ],
        }
    ],
    "totalElements": 1,
}

_LIVE_SEARCH_SHAPE_EMPTY_CONTENTS = {
    "contents": [],
    "totalElements": 0,
}

# 실측 형태의 origin 번호에 맞춘 get_product 응답 — _PRODUCT_BODY_WITH_POLICIES
# 의 originProductNo 를 녹화된 번호(문자열화)로 교체한 복사본.
_LIVE_PRODUCT_BODY_WITH_POLICIES = json.loads(json.dumps(_PRODUCT_BODY_WITH_POLICIES))
_LIVE_PRODUCT_BODY_WITH_POLICIES["originProduct"]["originProductNo"] = "13638045156"


def _mock_search_live_shape_one(*args, **kwargs):
    """search_products 가 실측 응답 형태(contents) 로 상품 1건을 반환."""
    return 200, _LIVE_SEARCH_SHAPE_ONE


def _mock_search_live_shape_empty_contents(*args, **kwargs):
    """search_products 가 실측 형태의 빈 contents(신규 셀러)를 반환."""
    return 200, _LIVE_SEARCH_SHAPE_EMPTY_CONTENTS


def _mock_get_product_live_shape(*args, **kwargs):
    """get_product 가 실측 origin 번호의 정책값을 반환."""
    return 200, _LIVE_PRODUCT_BODY_WITH_POLICIES


# --------------------------------------------------------------------------- #
# (a) 기본 호출 — read_existing 미지정 → 외부 호출 0회, 기존 호환.
# --------------------------------------------------------------------------- #
class TestDefaultNoExternalCalls:
    """read_existing 미지정 시 외부 API 호출 0회, 반환 키 호환."""

    def test_zero_external_calls_by_default(self, tmp_path, monkeypatch):
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        search_calls = []
        get_calls = []

        def search_spy(*a, **kw):
            search_calls.append((a, kw))
            return 200, {"products": []}

        def get_spy(*a, **kw):
            get_calls.append((a, kw))
            return 200, {}

        with mock.patch.object(naver_client, "search_products", side_effect=search_spy):
            with mock.patch.object(naver_client, "get_product", side_effect=get_spy):
                mcp_server.check_config()

        assert len(search_calls) == 0, "read_existing 미지정 시 search_products 호출 금지"
        assert len(get_calls) == 0, "read_existing 미지정 시 get_product 호출 금지"

    def test_return_keys_backward_compatible(self, tmp_path, monkeypatch):
        """기존 반환 키가 모두 존재한다 (호환). 새 키도 존재한다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()

        # 기존 키 — 의미 변경 금지.
        for key in (
            "ok",
            "config_path",
            "present",
            "missing",
            "placeholders",
            "origin_configured",
            "as_tel_configured",
            "error",
        ):
            assert key in result, f"기존 반환 키 누락: {key}"
        # 새 키.
        assert "policy_gaps" in result
        assert "suggested_from_existing" in result
        assert "drift_from_existing" in result
        assert "existing_read_error" in result

    def test_default_suggested_empty(self, tmp_path, monkeypatch):
        """read_existing 미지정 시 제안이 비어 있다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        assert result["suggested_from_existing"] == {}
        assert result["drift_from_existing"] == []
        assert result["existing_read_error"] is None


# --------------------------------------------------------------------------- #
# (b) read_existing=True + mock 상품 1건 → 정책값이 출처와 함께 제안.
# --------------------------------------------------------------------------- #
class TestReadExistingSuggestsValues:
    """read_existing=True 일 때 기존 상품에서 읽은 값이 출처와 함께 제안된다."""

    def test_suggests_values_with_source(self, tmp_path, monkeypatch):
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                result = mcp_server.check_config(read_existing=True)

        suggested = result["suggested_from_existing"]
        # config 가 비어 있으므로, 읽은 값이 모두 제안되어야 한다.
        assert "smartstore_notice_defaults.as_tel" in suggested
        as_tel_suggestion = suggested["smartstore_notice_defaults.as_tel"]
        assert as_tel_suggestion["value"] == "070-9999-8888"
        assert as_tel_suggestion["source_product_no"] == "existing-001"
        assert as_tel_suggestion["config_key"] == "smartstore_notice_defaults.as_tel"

    def test_suggested_items_have_source_product_no(self, tmp_path, monkeypatch):
        """모든 제안 항목은 출처 상품번호를 갖는다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                result = mcp_server.check_config(read_existing=True)

        suggested = result["suggested_from_existing"]
        assert len(suggested) > 0, "제안이 하나도 없음 — config 가 비어있으므로 제안이 있어야 함"
        for key, item in suggested.items():
            assert "source_product_no" in item, f"출처 누락: {key}"
            assert item["source_product_no"] == "existing-001"

    def test_policy_gaps_always_present(self, tmp_path, monkeypatch):
        """policy_gaps 는 read_existing 여부와 무관하게 채워진다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        gaps = result["policy_gaps"]
        # _BASE_CFG 의 정책값이 모두 빈 문자열이므로 전부 gap 이어야 한다.
        assert "smartstore_notice_defaults.as_tel" in gaps
        assert "smartstore_notice_defaults.origin_content" in gaps


# --------------------------------------------------------------------------- #
# (c) 상품 0개 → 제안이 비어 있고 그 사실이 반환에 드러난다.
# --------------------------------------------------------------------------- #
class TestZeroProductsNewSeller:
    """신규 셀러(상품 0개) — 제안이 비어 있고 사실이 드러난다."""

    def test_empty_suggestions_for_new_seller(self, tmp_path, monkeypatch):
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_no_products
        ):
            with mock.patch.object(naver_client, "get_product") as get_mock:
                result = mcp_server.check_config(read_existing=True)

        suggested = result["suggested_from_existing"]
        assert suggested == {}, "상품 0개면 제안도 빈 dict"
        # 상품이 없으므로 get_product 도 호출되지 않아야 한다.
        assert get_mock.call_count == 0
        # 에러는 아니다 — 부재가 실패는 아님.
        assert result["existing_read_error"] is None


# --------------------------------------------------------------------------- #
# (d) 설정에 값이 있고 다르면 차이 보고, 파일은 변경되지 않는다.
# --------------------------------------------------------------------------- #
class TestDriftReportedNotOverwritten:
    """설정값과 기존 상품값이 다르면 차이를 보고하고 파일은 변경하지 않는다."""

    def test_drift_reported_and_file_unchanged(self, tmp_path, monkeypatch):
        # config 의 as_guide 에 폐기된 문구가 남아 있고, 기존 상품에는 다른 문구.
        stale_cfg = json.loads(json.dumps(_BASE_CFG))
        stale_cfg["smartstore_notice_defaults"]["as_guide"] = "해외구매대행 폐기문구"
        stale_cfg["smartstore_notice_defaults"]["as_tel"] = "070-1111-2222"
        cfg_file = _write_cfg(tmp_path, stale_cfg)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        before_mtime = cfg_file.stat().st_mtime_ns
        before_content = cfg_file.read_bytes()

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                result = mcp_server.check_config(read_existing=True)

        # 파일이 변경되지 않아야 한다.
        after_content = cfg_file.read_bytes()
        assert before_content == after_content, "check_config 가 설정 파일을 변경함"
        # mtime 도 그대로.
        after_mtime = cfg_file.stat().st_mtime_ns
        assert before_mtime == after_mtime, "check_config 가 설정 파일 mtime 을 바꿈"

        # 차이가 보고되어야 한다 (as_guide).
        drift = result["drift_from_existing"]
        drift_keys = [d["config_key"] for d in drift]
        assert "smartstore_notice_defaults.as_guide" in drift_keys, "as_guide 드리프트 미보고"

    def test_no_drift_when_values_match(self, tmp_path, monkeypatch):
        """설정값과 기존 상품값이 같으면 드리프트가 없다."""
        match_cfg = json.loads(json.dumps(_BASE_CFG))
        match_cfg["smartstore_notice_defaults"]["as_tel"] = "070-9999-8888"
        cfg_file = _write_cfg(tmp_path, match_cfg)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                result = mcp_server.check_config(read_existing=True)

        drift_keys = [d["config_key"] for d in result["drift_from_existing"]]
        assert "smartstore_notice_defaults.as_tel" not in drift_keys, "같은 값인데 드리프트 보고됨"
        # as_tel 은 값이 있으므로 제안에서도 빠진다.
        assert "smartstore_notice_defaults.as_tel" not in result["suggested_from_existing"]


# --------------------------------------------------------------------------- #
# (e) 검색 실패해도 기존 진단 키들은 정상 반환된다.
# --------------------------------------------------------------------------- #
class TestSearchFailureGraceful:
    """search_products 실패 시 기존 진단 키는 정상, 에러 사유는 반환에 담긴다."""

    def test_existing_diagnostics_on_search_failure(self, tmp_path, monkeypatch):
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(naver_client, "search_products", side_effect=_mock_search_failure):
            result = mcp_server.check_config(read_existing=True)

        # 기존 진단 키는 정상 동작.
        assert "ok" in result
        assert "present" in result
        assert "missing" in result
        assert result["error"] is None  # config 자체는 정상이므로.
        # 읽기 실패 사유가 담겨 있다.
        assert result["existing_read_error"] is not None
        assert "실패" in result["existing_read_error"] or "403" in result["existing_read_error"]

    def test_exception_during_read_does_not_break_diagnostics(self, tmp_path, monkeypatch):
        """read_existing 중 예외가 발생해도 기존 진단은 살아있다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=RuntimeError("network down")
        ):
            result = mcp_server.check_config(read_existing=True)

        assert "ok" in result
        assert "present" in result
        assert result["existing_read_error"] is not None
        assert "RuntimeError" in result["existing_read_error"]


# --------------------------------------------------------------------------- #
# (f) 어떤 경로에서도 check_config 가 설정 파일을 쓰지 않는다.
# --------------------------------------------------------------------------- #
class TestNeverWritesConfig:
    """check_config 는 어떤 경로에서도 설정 파일을 쓰지 않는다."""

    @pytest.mark.parametrize("read_existing", [False, True])
    def test_config_file_not_written(self, tmp_path, monkeypatch, read_existing):
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        before = cfg_file.read_bytes()

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                mcp_server.check_config(read_existing=read_existing)

        after = cfg_file.read_bytes()
        assert before == after, f"check_config(read_existing={read_existing}) 가 파일을 변경함"

    def test_config_not_written_on_failure(self, tmp_path, monkeypatch):
        """검색 실패 경로에서도 파일을 쓰지 않는다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        before = cfg_file.read_bytes()

        with mock.patch.object(naver_client, "search_products", side_effect=_mock_search_failure):
            mcp_server.check_config(read_existing=True)

        after = cfg_file.read_bytes()
        assert before == after

    def test_no_open_in_write_mode(self, tmp_path, monkeypatch):
        """check_config 호출 중 open(... 'w'/'a'/...) 이 발생하지 않는다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        original_open = open
        write_calls = []

        def spy_open(path, mode="r", *args, **kwargs):
            if isinstance(mode, str) and any(m in mode for m in ("w", "a", "x", "+")):
                write_calls.append((str(path), mode))
            return original_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=spy_open):
            with mock.patch.object(
                naver_client, "search_products", side_effect=_mock_search_one_product
            ):
                with mock.patch.object(
                    naver_client, "get_product", side_effect=_mock_get_product_with_policies
                ):
                    mcp_server.check_config(read_existing=True)

        # check_config 자체는 config 를 쓰지 않는다. (mcp_server 모듈의 open 만 감시하므로
        # 다른 모듈의 open 은 별개. 여기서는 check_config 경로에서 쓰기가 없어야 한다.)
        cfg_writes = [c for c in write_calls if "config" in c[0]]
        assert cfg_writes == [], f"check_config 가 config 파일을 쓰기 모드로 엶: {cfg_writes}"


# --------------------------------------------------------------------------- #
# 도구 7개 유지 — 새 도구 추가 없음.
# --------------------------------------------------------------------------- #
class TestToolCountPreserved:
    """정책 온보딩 추가에도 MCP 도구가 7개로 유지된다.

    delete_product 가 추가되면서 도구 수가 6 → 7 로 늘었다.
    """

    def test_six_tools_registered(self):
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        # 7개 도구: check_config, upload_images, register_product, get_product,
        # prepare_listing, submit_reviews, delete_product. delete_product 는
        # 파괴적 능력이라 별도 도구로 분리했다.
        assert len(tools) == 7, f"도구가 7개여야 함: {len(tools)}"

    def test_check_config_takes_read_existing(self):
        """check_config 가 read_existing 키워드 인자를 받는다."""
        import inspect

        sig = inspect.signature(mcp_server.check_config)
        assert "read_existing" in sig.parameters
        param = sig.parameters["read_existing"]
        assert param.default is False, "read_existing 기본값은 False 여야 함 (외부 호출 0회)"


# --------------------------------------------------------------------------- #
# 무동작 금지 — 본 테스트 클래스가 실제 단언을 수행한다.
# --------------------------------------------------------------------------- #
class TestNoNoOp:
    """본 검증이 무동작이 아님을 보인다."""

    def test_suggested_vs_empty_differ(self, tmp_path, monkeypatch):
        """read_existing=True/False 가 다른 결과를 낸다 → 검증이 유효하다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                with_read = mcp_server.check_config(read_existing=True)

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                without_read = mcp_server.check_config(read_existing=False)

        assert with_read["suggested_from_existing"] != {}
        assert without_read["suggested_from_existing"] == {}


# --------------------------------------------------------------------------- #
# (g) 실측 응답 형태(contents + 중첩 channelProducts) 회귀.
# 과거에 ``products`` 키를 추측해 읽다가, 실제 API 가 ``contents`` 를
# 반환하는 바람에 22개 상품을 가진 셀러가 신규 셀러로 둔갑하는(조용한 빈
# 결과) 회귀가 있었다. 본 클래스는 녹화된 실측 형태에 대해 측정한다 —
# 다음 스키마 변경은 이 고정된 형태에 대해 판정된다.
# --------------------------------------------------------------------------- #
class TestLiveSearchShapeRegression:
    """실측 응답 형태(contents/channelProducts)에 대한 회귀 검증."""

    def test_live_shape_yields_suggestions_with_origin_provenance(self, tmp_path, monkeypatch):
        """``contents`` + 중첩 ``channelProducts`` 형태가 출처(origin)번호와
        함께 제안을 산출한다. 키가 ``products`` 였던 옛 코드는 여기서 빈
        결과를 반환했을 것이다 — 그 회귀를 잡는다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_live_shape_one
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_live_shape
            ):
                result = mcp_server.check_config(read_existing=True)

        suggested = result["suggested_from_existing"]
        # 신규 셀러로 둔갑하지 않았는지 — 제안이 하나 이상 있어야 한다.
        assert suggested != {}, "실측 응답(contents)에서 제안이 비어 있음 — products 키 회귀 가능성"
        # 출처 provenance 가 녹화된 origin 번호(문자열화)로 찍혀야 한다.
        for key, item in suggested.items():
            assert (
                item["source_product_no"] == "13638045156"
            ), f"출처 origin 번호 불일치: {key} -> {item['source_product_no']!r}"
        # as_tel 값이 실측 origin 번호에서 읽힌다.
        assert "smartstore_notice_defaults.as_tel" in suggested
        assert suggested["smartstore_notice_defaults.as_tel"]["value"] == "070-9999-8888"

    def test_live_shape_empty_contents_reports_new_seller_honestly(self, tmp_path, monkeypatch):
        """빈 ``contents`` 리스트는 신규 셀러로 정직히 보고된다 — 에러가 아니다."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_live_shape_empty_contents
        ):
            with mock.patch.object(naver_client, "get_product") as get_mock:
                result = mcp_server.check_config(read_existing=True)

        assert result["suggested_from_existing"] == {}, "빈 contents 면 제안도 빈 dict"
        # 상품이 없으므로 get_product 는 호출되지 않는다.
        assert get_mock.call_count == 0
        # 부재는 실패가 아니다 — 에러 키는 None.
        assert result["existing_read_error"] is None

    def test_legacy_products_key_still_supported(self, tmp_path, monkeypatch):
        """``products`` 키(옛 형태)도 폴백으로 여전히 읽힌다 — 양방향 스키마 호환."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_one_product
        ):
            with mock.patch.object(
                naver_client, "get_product", side_effect=_mock_get_product_with_policies
            ):
                result = mcp_server.check_config(read_existing=True)

        suggested = result["suggested_from_existing"]
        assert suggested != {}, "products 키 폴백이 깨짐 — 양방향 호환 실패"
        for item in suggested.values():
            assert item["source_product_no"] == "existing-001"

    def test_normalize_search_listing_reads_nested_channel_values(self):
        """``_normalize_search_listing`` 이 중첩 channelProducts[0] 의 채널
        수준값(name/statusType/channelProductNo)을 평탄화해 꺼낸다."""
        entry = {
            "originProductNo": 13638045156,
            "channelProducts": [
                {
                    "channelProductNo": 13698323110,
                    "name": "라이브 상품",
                    "statusType": "SALE",
                }
            ],
        }
        flat = mcp_server._normalize_search_listing(entry)
        assert flat["originProductNo"] == 13638045156
        assert flat["channelProductNo"] == 13698323110
        assert flat["name"] == "라이브 상품"
        assert flat["statusType"] == "SALE"
        # channelProducts 원본은 평탄화 결과에 남지 않는다.
        assert "channelProducts" not in flat

    def test_normalize_search_listing_origin_wins_on_conflict(self):
        """최상위(origin) 값이 채널값과 충돌하면 origin 이 이긴다."""
        entry = {
            "originProductNo": 1,
            "name": "origin-name",
            "channelProducts": [{"name": "channel-name"}],
        }
        flat = mcp_server._normalize_search_listing(entry)
        assert flat["name"] == "origin-name"

    def test_normalize_search_listing_handles_non_dict(self):
        """dict 가 아닌 입력은 빈 dict 로 떨어진다 (추정·합성 금지)."""
        assert mcp_server._normalize_search_listing(None) == {}
        assert mcp_server._normalize_search_listing([]) == {}
        assert mcp_server._normalize_search_listing("not-a-dict") == {}


# --------------------------------------------------------------------------- #
# (h) FIX-P3: 스키마 이상과 "진짜 신규 셀러" 구분.
#
# 과거에는 ``contents``/``products`` 키가 아예 없거나 값이 list 가 아닌
# 응답을 "신규 셀러"로 둔갑시켰다 (existing_read_error=None, 제안={}). 이제
# 스키마 이상으로 판정되면 error 에 사유를 담아 반환한다. 빈 리스트는
# 여전히 진짜 신규 셀러로 취급한다 (회귀 금지).
# --------------------------------------------------------------------------- #
class TestFixP3SchemaAnomalyDistinguished:
    """FIX-P3: ``contents``/``products`` 키 부재 = 스키마 이상 (신규 셀러 아님)."""

    def test_unknown_response_keys_flagged_as_error(self, tmp_path, monkeypatch):
        """``{"unexpected": [...]}`` 처럼 키가 아예 없으면 error.

        과거: ``existing_read_error=None``, ``suggested={}`` (신규 셀러로 둔갑).
        FIX-P3: ``existing_read_error`` 에 사유, ``suggested={}``.
        """
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        def _mock_search_unknown_schema(*args, **kwargs):
            return 200, {"unexpected": [{"id": 1}], "meta": {"page": 1}}

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_unknown_schema
        ):
            result = mcp_server.check_config(read_existing=True)

        assert result["suggested_from_existing"] == {}
        # 핵심: 에러 사유가 명시되어, "제안 없음 = 신규 셀러" 오해 방지.
        assert result["existing_read_error"] is not None
        assert "스키마" in result["existing_read_error"] or "키" in result["existing_read_error"]

    def test_contents_not_a_list_flagged_as_error(self, tmp_path, monkeypatch):
        """``contents`` 가 dict 등 list 가 아니면 error (스키마 이상)."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        def _mock_search_contents_not_list(*args, **kwargs):
            return 200, {"contents": {"wrong": "shape"}, "totalElements": 1}

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_contents_not_list
        ):
            result = mcp_server.check_config(read_existing=True)

        assert result["suggested_from_existing"] == {}
        assert result["existing_read_error"] is not None
        assert "list" in result["existing_read_error"]

    def test_empty_list_still_new_seller_no_error(self, tmp_path, monkeypatch):
        """회귀: 빈 ``contents``/``products`` 리스트는 여전히 신규 셀러 (에러 아님)."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_no_products
        ):
            result = mcp_server.check_config(read_existing=True)

        assert result["suggested_from_existing"] == {}
        # 진짜 신규 셀러 — 부재는 실패가 아니다.
        assert result["existing_read_error"] is None

    def test_empty_contents_live_shape_still_new_seller(self, tmp_path, monkeypatch):
        """회귀: 실측 형태의 빈 contents 도 여전히 신규 셀러."""
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_live_shape_empty_contents
        ):
            result = mcp_server.check_config(read_existing=True)

        assert result["suggested_from_existing"] == {}
        assert result["existing_read_error"] is None

    def test_first_listing_missing_origin_product_no_flagged(self, tmp_path, monkeypatch):
        """첫 listing 엔트리에 originProductNo 가 없으면 스키마 이상.

        과거: ``existing_read_error=None`` (조용한 신규 셀러 둔갑).
        FIX-P3: error 에 사유 명시.
        """
        cfg_file = _write_cfg(tmp_path, _BASE_CFG)
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        def _mock_search_no_origin_no(*args, **kwargs):
            # contents 키는 있고 원소도 있지만 originProductNo 가 없음.
            return 200, {"contents": [{"name": "고아 상품"}]}

        with mock.patch.object(
            naver_client, "search_products", side_effect=_mock_search_no_origin_no
        ):
            with mock.patch.object(naver_client, "get_product") as get_mock:
                result = mcp_server.check_config(read_existing=True)

        assert result["suggested_from_existing"] == {}
        assert result["existing_read_error"] is not None
        assert "originProductNo" in result["existing_read_error"]
        # origin 번호를 모르므로 get_product 도 호출되지 않는다.
        assert get_mock.call_count == 0
