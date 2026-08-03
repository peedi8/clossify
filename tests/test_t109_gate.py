"""T-109 — MCP 표면의 QA 게이트 우회 차단 검증 테스트.

작업지시(T-109)가 요구하는 시나리오:
  1. 차단 반례: 의류 카테고리 + 고시 필수 필드(material, size 등) 누락 →
     register_product 가 네이버를 호출하지 않고 거부.
  2. 차단 반례2: KC 필요 카테고리(50000151) + KC 정보 없음 → 거부.
  3. 통과 반례: 의류 + 필수 필드 완비 + config 원산지 일치 → 등록 경로 진입.
  4. 미차단 확인: LLM 판단 미회신만 있는 경우 → 차단되지 않고 pending_reviews 표기.
  5. check_config 가 원산지 설정 여부를 보고하되 값은 반환하지 않음.
  6. 도구 4개 등록 유지.

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로 네트워크 차단.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

# 의류 카테고리 (KC 불필요, WEAR 고시 타입).
_CLOTHING_CATEGORY = "50021299"

# 고데기 카테고리 (KC 필요, HOME_APPLIANCES 고시 타입).
_KC_CATEGORY = "50000151"

# notice_config mock: origin 이 설정된 정상 config.
_NOTICE_CFG_WITH_ORIGIN = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
}


def _mock_naver_register_success(*args, **kwargs):
    """naver_client.register_product 를 성공 응답으로 mock."""
    return (200, {"originProductNo": "test-origin-no-123"})


def _mock_naver_register_called_recorder(call_log: list):
    """naver_client.register_product 호출을 기록하는 mock factory."""

    def _recorder(*args, **kwargs):
        call_log.append({"args": args, "kwargs": kwargs})
        return (200, {"originProductNo": "test-origin-no-123"})

    return _recorder


# --------------------------------------------------------------------------- #
# 차단 반례 1: 의류 카테고리 + 고시 필수 필드 누락.
# --------------------------------------------------------------------------- #
class TestBlockClothingMissingFields:
    """의류 카테고리에서 material, size 등 필수 필드가 누락되면 차단."""

    def test_blocks_clothing_without_material_and_size(self):
        """register_product 가 네이버를 호출하지 않고 거부하는가."""
        naver_calls = []
        # COMMERCE_DRY_RUN 이 설정되어 있지 않아야 게이트가 동작한다.
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    naver_client,
                    "register_product",
                    side_effect=_mock_naver_register_called_recorder(naver_calls),
                ):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        # 등록이 거부되어야 한다.
        assert result["ok"] is False
        assert result.get("blocked_by") == "compliance"
        # 네이버 API 가 호출되지 않아야 한다.
        assert len(naver_calls) == 0, f"네트워크 호출이 발생했습니다: {naver_calls}"

    def test_blocked_response_has_violations(self):
        """거부 응답에 violations 배열이 있는가."""
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product", return_value=(200, {})):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        assert result["ok"] is False
        violations = result.get("violations")
        assert isinstance(violations, list)
        assert len(violations) > 0
        # 고시 필수필드 위반이 있어야 한다.
        rules = [v.get("rule") for v in violations]
        assert "고시 필수필드" in rules

    def test_blocked_response_has_needs_user_with_material_and_size(self):
        """needs_user 에 material 과 size 가 포함되어 있는가."""
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product", return_value=(200, {})):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        needs_user = result.get("needs_user")
        assert isinstance(needs_user, list)
        assert len(needs_user) > 0
        field_names = [n["field"] for n in needs_user]
        assert "material" in field_names, f"material 이 needs_user 에 없음: {field_names}"
        assert "size" in field_names, f"size 가 needs_user 에 없음: {field_names}"
        # 각 needs_user 항목은 field, label, why 를 가져야 한다.
        for item in needs_user:
            assert "field" in item
            assert "label" in item
            assert "why" in item

    def test_blocked_response_has_message(self):
        """거부 응답에 사용자가 읽을 수 있는 message 가 있는가."""
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product", return_value=(200, {})):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        message = result.get("message")
        assert isinstance(message, str)
        assert len(message) > 0
        # 한국어 안내여야 한다.
        assert "컴플라이언스" in message or "거부" in message


# --------------------------------------------------------------------------- #
# 차단 반례 2: KC 필요 카테고리 + KC 정보 없음.
# --------------------------------------------------------------------------- #
class TestBlockKcMissing:
    """KC 인증이 필요한 카테고리에서 KC 정보가 없으면 차단."""

    def test_blocks_kc_category_without_kc_info(self):
        """고데기 카테고리(50000151) + KC 정보 없음 → 거부."""
        naver_calls = []
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    naver_client,
                    "register_product",
                    side_effect=_mock_naver_register_called_recorder(naver_calls),
                ):
                    result = mcp_server.register_product(
                        name="테스트고데기",
                        price=50000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_KC_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        assert result["ok"] is False
        assert result.get("blocked_by") == "compliance"
        assert len(naver_calls) == 0, "네이버 API 가 호출되었습니다"

    def test_kc_violation_in_result(self):
        """거부 응답의 violations 에 KC 관련 위반이 있는가."""
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product", return_value=(200, {})):
                    result = mcp_server.register_product(
                        name="테스트고데기",
                        price=50000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_KC_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        violations = result.get("violations", [])
        kc_violations = [
            v
            for v in violations
            if "KC" in str(v.get("rule") or "") or "인증" in str(v.get("rule") or "")
        ]
        assert len(kc_violations) > 0, f"KC 위반이 없음: {violations}"


# --------------------------------------------------------------------------- #
# 통과 반례: 의류 + 필수 필드 완비 + config 원산지 일치 → 등록 경로 진입.
# --------------------------------------------------------------------------- #
class TestPassClothingComplete:
    """의류 카테고리에서 필수 필드가 완비되면 등록 경로로 진입."""

    def test_passes_and_calls_naver(self):
        """필수 필드 완비 시 네이버 API 가 호출되는가."""
        naver_calls = []
        # WEAR 필수 필드를 notice override 로 제공.
        # naver_client._merge_notice 은 non-FURNITURE 타입의 body 를
        # etc 키 아래에 병합하므로, etc 키로 전달한다.
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "etc": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 이내 교환 가능",
            },
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                # _compliance_code_check 가 common.cfg().get(
                # "smartstore_notice_defaults") 를 직접 읽기 때문에,
                # CI(config.example.json)의 플레이스홀더 원산지와 충돌한다.
                # _notice_config mock 값과 일치하도록 common.cfg 도 함께 덮어쓴다.
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=_mock_naver_register_called_recorder(naver_calls),
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=notice_override,
                        )

        assert result["ok"] is True, f"등록 실패: {result}"
        # 네이버 API 가 실제로 호출되었는지 확인.
        assert len(naver_calls) == 1, f"네이버 API 호출 횟수가 예상과 다름: {len(naver_calls)}"

    def test_pass_response_has_pending_reviews(self):
        """통과 응답에 pending_reviews 가 표기되어 있는가."""
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "etc": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 이내 교환 가능",
            },
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                # _compliance_code_check 가 common.cfg().get(
                # "smartstore_notice_defaults") 를 직접 읽기 때문에,
                # CI(config.example.json)의 플레이스홀더 원산지와 충돌한다.
                # _notice_config mock 값과 일치하도록 common.cfg 도 함께 덮어쓴다.
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client, "register_product", side_effect=_mock_naver_register_success
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=notice_override,
                        )

        pending = result.get("pending_reviews")
        assert isinstance(pending, list)
        assert len(pending) > 0, "pending_reviews 가 비어 있음 (조용한 생략 금지)"
        # copy_qa 와 image_qa 가 적어도 하나 이상 언급되어야 한다.
        joined = " ".join(pending)
        assert "copy" in joined.lower() or "카피" in joined
        assert "image" in joined.lower() or "이미지" in joined

    def test_no_needs_user_when_passing(self):
        """통과 시 needs_user 가 비어 있는가."""
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "etc": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 이내 교환 가능",
            },
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    naver_client, "register_product", side_effect=_mock_naver_register_success
                ):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                        notice=notice_override,
                    )

        # 통과 시 needs_user 가 없거나 빈 리스트.
        needs = result.get("needs_user")
        if needs is not None:
            assert needs == [], f"통과 시 needs_user 가 비어있지 않음: {needs}"


# --------------------------------------------------------------------------- #
# 미차단 확인: LLM 판단 미회신은 차단하지 않고 pending_reviews 에 표기.
# --------------------------------------------------------------------------- #
class TestLlmPendingNotBlocked:
    """LLM 판단 미회신 상태는 등록을 차단하지 않는다."""

    def test_pending_reviews_present_on_success(self):
        """결정론 게이트 통과 시 pending_reviews 가 응답에 포함되는가.

        LLM 판단(카피/이미지 QA)은 위임 왕복(T-201c) 연동 전까지 항상
        미회신 상태다. 작업지시는 이것이 등록을 차단하지 않되,
        응답에 표기하라고 요구한다.
        """
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "etc": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 이내 교환 가능",
            },
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                # _compliance_code_check 가 common.cfg().get(
                # "smartstore_notice_defaults") 를 직접 읽기 때문에,
                # CI(config.example.json)의 플레이스홀더 원산지와 충돌한다.
                # _notice_config mock 값과 일치하도록 common.cfg 도 함께 덮어쓴다.
                with mock.patch.object(
                    common,
                    "cfg",
                    return_value={
                        "smartstore_notice_defaults": {
                            "origin_area_code": "04",
                            "origin_content": "중국",
                        },
                    },
                ):
                    with mock.patch.object(
                        naver_client, "register_product", side_effect=_mock_naver_register_success
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=notice_override,
                        )

        # LLM 미회신만 있는 경우 차단되지 않는다.
        assert result["ok"] is True
        pending = result.get("pending_reviews", [])
        assert (
            len(pending) >= 2
        ), f"pending_reviews 에 copy_qa/image_qa 가 모두 있어야 함: {pending}"

    def test_blocked_response_has_empty_pending_reviews(self):
        """결정론 FAIL 로 차단된 경우 pending_reviews 가 빈 리스트인가.

        차단된 경우 LLM 판단 대기 항목이 무의미하므로 빈 리스트를 반환한다.
        """
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product", return_value=(200, {})):
                    result = mcp_server.register_product(
                        name="테스트니트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                    )

        assert result["ok"] is False
        assert result.get("pending_reviews", []) == []


# --------------------------------------------------------------------------- #
# check_config 원산지 설정 여부 보고.
# --------------------------------------------------------------------------- #
class TestCheckConfigOrigin:
    """check_config 가 원산지 설정 여부를 보고하는가."""

    def test_origin_configured_true_when_set(self, tmp_path, monkeypatch):
        """config 에 원산지가 설정되어 있으면 origin_configured=True."""
        cfg = {
            "naver": {
                "client_id": "test_id",
                "client_secret": "test_secret",
                "store_url_slug": "test-slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "origin_content": "중국",
            },
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        assert result.get("origin_configured") is True

    def test_origin_configured_false_when_missing(self, tmp_path, monkeypatch):
        """config 에 원산지가 없으면 origin_configured=False."""
        cfg = {
            "naver": {
                "client_id": "test_id",
                "client_secret": "test_secret",
                "store_url_slug": "test-slug",
            },
            "smartstore_notice_defaults": {},
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        assert result.get("origin_configured") is False

    def test_origin_hint_present_when_not_configured(self, tmp_path, monkeypatch):
        """원산지 미설정 시 origin_hint 안내가 포함되는가."""
        cfg = {
            "naver": {
                "client_id": "test_id",
                "client_secret": "test_secret",
                "store_url_slug": "test-slug",
            },
            "smartstore_notice_defaults": {},
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        hint = result.get("origin_hint")
        assert isinstance(hint, str)
        assert "원산지" in hint
        # 등록 거부 안내가 포함되어야 한다.
        assert "거부" in hint or "차단" in hint or "register_product" in hint

    def test_origin_value_not_leaked(self, tmp_path, monkeypatch):
        """원산지 값 자체가 반환값에 노출되지 않는가 (작업지시: 값 반환 금지)."""
        secret_origin = "매우특정한원산지값_노출되면안됨"
        cfg = {
            "naver": {
                "client_id": "test_id",
                "client_secret": "test_secret",
                "store_url_slug": "test-slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "origin_content": secret_origin,
            },
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        # 결과 전체를 직렬화해도 원산지 값이 나오면 안 된다.
        serialized = json.dumps(result, ensure_ascii=False)
        assert secret_origin not in serialized, "원산지 값이 check_config 응답에 노출됨"

    def test_origin_configured_false_with_placeholder(self, tmp_path, monkeypatch):
        """원산지가 플레이스홀더면 origin_configured=False."""
        cfg = {
            "naver": {
                "client_id": "test_id",
                "client_secret": "test_secret",
                "store_url_slug": "test-slug",
            },
            "smartstore_notice_defaults": {
                "origin_area_code": "REPLACE_WITH_CODE",
                "origin_content": "{ORIGIN_CONTENT}",
            },
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setenv("CLOSSIFY_CONFIG", str(cfg_file))

        result = mcp_server.check_config()
        assert result.get("origin_configured") is False


# --------------------------------------------------------------------------- #
# 도구 4개 등록 유지 (시그니처 변경 없음).
# --------------------------------------------------------------------------- #
class TestToolRegistrationPreserved:
    """T-109 변경 후에도 4개 도구가 등록되어 있는가."""

    def test_four_tools_registered(self):
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        assert len(tools) == 6, f"도구가 6개여야 함: {len(tools)}"

    def test_tool_names_unchanged(self):
        """6개 도구 이름이 변경되지 않았는가."""
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        names = {getattr(t, "name", None) for t in tools}
        expected = {
            "check_config",
            "upload_images",
            "register_product",
            "get_product",
            "prepare_listing",
            "submit_reviews",
        }
        assert names == expected, f"도구 이름 불일치: {names}"

    def test_register_product_signature_unchanged(self):
        """register_product 의 파라미터 이름/타입이 유지되는가."""
        import inspect

        sig = inspect.signature(mcp_server.register_product)
        param_names = list(sig.parameters.keys())
        expected = [
            "name",
            "price",
            "image_urls",
            "category_id",
            "detail_html",
            "options",
            "tags",
            "status",
            "stock",
            "delivery_fee",
            "courier",
            "notice",
        ]
        assert param_names == expected, f"시그니처 변경 감지: {param_names}"


# --------------------------------------------------------------------------- #
# Fail-closed 검증: 예외를 삼켜 등록을 진행시키지 않는다.
# --------------------------------------------------------------------------- #
class TestFailClosed:
    """컴플라이언스 검사 중 예외가 발생하면 등록을 차단한다."""

    def test_compliance_exception_blocks_registration(self):
        """_run_compliance_gate 가 예외를 throw 하면 등록이 차단되는가."""
        naver_calls = []
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(
                    mcp_server,
                    "_run_compliance_gate",
                    side_effect=RuntimeError("검사 내부 오류"),
                ):
                    with mock.patch.object(
                        naver_client,
                        "register_product",
                        side_effect=_mock_naver_register_called_recorder(naver_calls),
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                        )

        # 예외를 삼키지 않고 fail-closed 차단.
        assert result["ok"] is False
        assert len(naver_calls) == 0, "예외 발생 시 네이버 API 호출 금지"
        assert result.get("error") is not None
        assert "컴플라이언스" in result["error"] or "오류" in result["error"]
