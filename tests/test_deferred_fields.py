"""지연 고시 필드(deferred_notice_fields) 기능 검증.

본 테스트가 다루는 계약:
  1. 판매자가 명시적으로 "상세페이지 참조" 로 미루기로 선택한 고시 필드는
     게이트의 "고시 필수필드 누락" 위반에서 제외되며, 빈 자리에는
     ``qa_agents.DEFERRED_NOTICE_PLACEHOLDER`` ("상세페이지 참조") 가 채워져
     전송된다.
  2. 미루기로 이름을 올리지 않은 빈 필드는 여전히 차단된다 (네이버 호출 0회).
  3. 원산지(origin) 필드는 법적 선언이므로 미루기 요청이 거부된다
     (네이버 호출 0회, 사유 명시).
  4. 반환의 ``deferred_notice_fields`` 는 미루기가 적용된 필드명 리스트며,
     미루기가 없으면 빈 리스트다.
  5. 미루기로 이름을 올렸더라도 실값이 채워진 필드는 실값이 우선하며,
     반환의 ``deferred_notice_fields`` 에서 제외된다.

모든 테스트는 COMMERCE_DRY_RUN off, HTTP 를 mock 으로 차단하며 호출 횟수를 센다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

# 의류 카테고리 (KC 불필요, WEAR 고시 타입).
_CLOTHING_CATEGORY = "50021299"

# notice_config mock: origin 이 설정된 정상 config.
# 5개 공통 고시 필드(returnCostReason 등)도 config 에서 제공하는 것이
# 정상 판매자 설정이다. 코드가 임의 문구를 만들어 채우지 않으므로 테스트
# 픽스처에서 명시적으로 제공한다.
_NOTICE_CFG_WITH_ORIGIN = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
}


def _mock_naver_register_called_recorder(call_log: list):
    """naver_client.register_product 호출을 기록하는 mock factory."""

    def _recorder(*args, **kwargs):
        call_log.append({"args": args, "kwargs": kwargs})
        return (200, {"originProductNo": "test-origin-no-deferred"})

    return _recorder


def _common_cfg_origin():
    """_compliance_code_check 의 common.cfg 직접 읽기를 위한 mock 값."""
    return {
        "smartstore_notice_defaults": {
            "origin_area_code": "04",
            "origin_content": "중국",
        },
    }


def _notice_body(notice: dict) -> dict:
    """payload 의 notice dict 에서 본문 노드를 찾는다.

    WEAR 타입은 ``wear`` 노드, ETC 는 ``etc``, FURNITURE 는 ``furniture``.
    어느 노드에도 본문이 없으면 빈 dict 를 반환한다.
    """
    if not isinstance(notice, dict):
        return {}
    ntype = str(notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    spec = qa_agents._notice_type_spec(ntype) if ntype else None
    node_key = (spec or {}).get("node") if spec else None
    if node_key and isinstance(notice.get(node_key), dict):
        return notice[node_key]
    for fb in ("wear", "etc", "furniture"):
        body = notice.get(fb)
        if isinstance(body, dict) and body:
            return body
    return {}


# WEAR 의류 notice override: material 만 빼고 모든 필수 필드를 채운다.
# material 을 테스트 대상(미루기 또는 누락) 으로 쓴다.
def _wear_notice_without_material(extra: dict | None = None) -> dict:
    body = {
        "color": "블랙",
        "size": "FREE",
        "caution": "물 세탁 가능",
        "packDateText": "2024-01-01",
        "warrantyPolicy": "구매 후 7일 이내 교환 가능",
    }
    if extra:
        body.update(extra)
    return {
        "productInfoProvidedNoticeType": "WEAR",
        "etc": body,
    }


# --------------------------------------------------------------------------- #
# 1. 미루기로 이름 올린 필드는 게이트를 통과하고 페이로드에 표준 문구가 실린다.
# --------------------------------------------------------------------------- #
class TestDeferredFieldPassesAndCarriesPhrasing:
    """``deferred_notice_fields=["material"]`` → 게이트 통과 + 페이로드에 문구."""

    def test_material_deferred_passes_gate(self):
        """material 을 미루기로 선택하면 게이트가 통과된다."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material()
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material"],
                        )

        # 게이트를 통과했는가.
        assert result["ok"] is True, f"등록 실패: {result}"
        # 네이버 API 가 1회 호출되었는가.
        assert len(naver_calls) == 1, f"네이버 API 호출 횟수가 1 이어야 함: {len(naver_calls)}"
        # 페이로드의 notice 본문에서 material 값이 표준 문구인가.
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        assert isinstance(notice, dict), "payload 에 notice dict 가 없음"
        body = _notice_body(notice)
        assert (
            body.get("material") == qa_agents.DEFERRED_NOTICE_PLACEHOLDER
        ), f"material 값이 표준 문구가 아님: {body.get('material')!r}"

    def test_deferred_field_reported_in_return(self):
        """반환이 ``deferred_notice_fields`` 로 ["material"] 을 보고하는가."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material()
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material"],
                        )

        reported = result.get("deferred_notice_fields")
        assert reported == [
            "material"
        ], f"deferred_notice_fields 가 [material] 이어야 함: {reported!r}"


# --------------------------------------------------------------------------- #
# 2. 미루기로 이름을 올리지 않은 빈 필드는 여전히 차단 (네이버 호출 0회).
# --------------------------------------------------------------------------- #
class TestBlankFieldWithoutDeferralStillBlocks:
    """``deferred_notice_fields`` 없이 빈 필드 → 게이트 차단, 네이버 호출 0회."""

    def test_blank_material_blocks_without_deferral(self):
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material()  # material 없음
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
                        notice=notice_override,
                        preview_confirmed=True,
                        # deferred_notice_fields 생략 → 빈 칸은 차단.
                    )

        assert result["ok"] is False, "material 빈 칸인데 통과하면 안 됨"
        assert result.get("blocked_by") == "compliance"
        assert len(naver_calls) == 0, f"네이버 호출이 0 이어야 함 (차단): {len(naver_calls)}"

    def test_blank_material_blocks_even_with_other_deferred(self):
        """다른 필드(color) 만 미루기로 올린 경우, 빈 material 은 여전히 차단."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"color": ""})
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
                        notice=notice_override,
                        preview_confirmed=True,
                        deferred_notice_fields=["color"],  # material 은 미루기 아님
                    )

        assert result["ok"] is False, "material 이 빈데 통과하면 안 됨"
        assert len(naver_calls) == 0, "차단 시 네이버 호출 0회"


# --------------------------------------------------------------------------- #
# 3. 원산지 필드를 미루기로 올리면 거부 (네이버 호출 0회, 사유 명시).
# --------------------------------------------------------------------------- #
class TestOriginFieldNotDeferrable:
    """원산지(origin) 필드는 법적 선언이라 미루기에서 거부된다."""

    def test_made_in_deferred_is_refused(self):
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["made_in"],
                        )

        assert result["ok"] is False
        assert (
            result.get("blocked_by") == "origin_field_not_deferrable"
        ), f"원산지 미루기 거부 사유가 아님: {result.get('blocked_by')}"
        assert len(naver_calls) == 0, "원산지 거부 시 네이버 호출 0회"
        # 사유에 "원산지" 또는 "법적 선언" 이 포함되어야 한다.
        msg = result.get("message") or ""
        assert "원산지" in msg or "법적 선언" in msg, f"사유에 원산지 언급 없음: {msg}"

    def test_origin_area_info_deferred_is_refused(self):
        """``originAreaInfo.content`` 도 원산지 필드로 거부된다."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["originAreaInfo.content"],
                        )

        assert result["ok"] is False
        assert result.get("blocked_by") == "origin_field_not_deferrable"
        assert len(naver_calls) == 0

    def test_origin_refusal_message_names_field(self):
        """거부 메시지가 어느 필드가 문제인지 명시하는가."""
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
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
                        notice=notice_override,
                        preview_confirmed=True,
                        deferred_notice_fields=["made_in"],
                    )

        msg = result.get("message") or ""
        assert "made_in" in msg, f"사유에 made_in 필드명이 없음: {msg}"


# --------------------------------------------------------------------------- #
# 4. 반환의 deferred_notice_fields 가 정확하다 — 빈 리스트 when none.
# --------------------------------------------------------------------------- #
class TestDeferredReportAccuracy:
    """``deferred_notice_fields`` 반환값의 정확성."""

    def test_empty_list_when_no_deferral(self):
        """미루기 선택이 없으면 빈 리스트."""
        naver_calls: list[dict] = []
        # 모든 필수 필드를 채운 notice — 미루기 없이 통과.
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                        )

        assert result["ok"] is True, f"통과해야 함: {result}"
        reported = result.get("deferred_notice_fields")
        assert reported == [], f"미루기 없으면 빈 리스트여야 함: {reported!r}"

    def test_empty_list_when_deferral_is_none(self):
        """``deferred_notice_fields=None`` 명시도 빈 리스트."""
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
                    with mock.patch.object(
                        naver_client, "register_product", return_value=(200, {})
                    ):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=notice_override,
                            preview_confirmed=True,
                            deferred_notice_fields=None,
                        )

        reported = result.get("deferred_notice_fields")
        assert reported == [], f"None 명시도 빈 리스트: {reported!r}"

    def test_report_lists_exactly_applied_fields(self):
        """여러 필드를 미루기로 올렸고 모두 빈 자리 → 반환에 정확히 그 필드들."""
        naver_calls: list[dict] = []
        # material, color 둘 다 빈 칸으로.
        notice_override = _wear_notice_without_material(extra={"color": ""})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material", "color"],
                        )

        assert result["ok"] is True, f"미루기로 통과해야 함: {result}"
        reported = result.get("deferred_notice_fields")
        # 순서가 보존되어야 한다.
        assert reported == ["material", "color"], f"미루기 적용 필드가 정확해야 함: {reported!r}"


# --------------------------------------------------------------------------- #
# 5. 미루기로 이름 올렸지만 실값이 있으면 실값이 우선, 반환에서 제외.
# --------------------------------------------------------------------------- #
class TestRealValueWinsOverDeferred:
    """실값이 있는 필드를 미루기로 표시해도 실값이 우선한다."""

    def test_real_value_kept_and_not_reported(self):
        """material 에 실값이 있는데 미루기로도 올린 경우."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material"],  # 실값 있는데 미루기
                        )

        assert result["ok"] is True, f"통과해야 함: {result}"
        # 반환에서 material 이 빠져야 한다 (실값이 있어 미루기 적용 안 됨).
        reported = result.get("deferred_notice_fields")
        assert reported == [], f"실값이 있으면 미루기에서 제외되어야 함: {reported!r}"
        # 페이로드의 material 값이 실값 그대로.
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        assert isinstance(notice, dict)
        body = _notice_body(notice)
        assert body.get("material") == "면 100%", f"실값이 우선해야 함: {body.get('material')!r}"

    def test_mixed_real_and_blank_fields(self):
        """material 은 실값, color 는 빈 칸 + 둘 다 미루기로 올린 경우."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%", "color": ""})
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material", "color"],
                        )

        assert result["ok"] is True, f"통과해야 함: {result}"
        # material 은 실값, color 만 미루기 적용 → 반환에 color 만.
        reported = result.get("deferred_notice_fields")
        assert reported == ["color"], f"color 만 미루기 적용되어야 함: {reported!r}"
        # 페이로드에서 material=실값, color=문구.
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        assert isinstance(notice, dict)
        body = _notice_body(notice)
        assert body.get("material") == "면 100%"
        assert body.get("color") == qa_agents.DEFERRED_NOTICE_PLACEHOLDER


# --------------------------------------------------------------------------- #
# 보너스: 입력 형태 검증.
# --------------------------------------------------------------------------- #
class TestDeferredInputValidation:
    """``deferred_notice_fields`` 입력 형태 검증."""

    def test_non_list_input_rejected(self):
        """리스트가 아닌 입력은 거부 (네이버 호출 0회)."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
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
                        notice=notice_override,
                        preview_confirmed=True,
                        deferred_notice_fields="material",  # str, not list
                    )

        assert result["ok"] is False
        assert len(naver_calls) == 0

    def test_non_string_item_rejected(self):
        """비문자열 항목이 섞인 리스트는 거부."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
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
                        notice=notice_override,
                        preview_confirmed=True,
                        deferred_notice_fields=["material", 123],
                    )

        assert result["ok"] is False
        assert len(naver_calls) == 0


# --------------------------------------------------------------------------- #
# 6. allowlist 검증 — 고시 정의에 없는 키는 거부 (대소문자 변형·별칭·오타 포함).
# 과거 이 게이트는 어떤 키든 받아 "미뤄졌다" 고 믿게 두고 네이버에 임의 키로
# "상세페이지 참조" 를 전송했다. 본 절은 allowlist 밖 키가 거부되는지 검증한다.
# --------------------------------------------------------------------------- #
class TestDeferredAllowlistRejection:
    """``deferred_notice_fields`` 의 allowlist 검증."""

    @pytest.mark.parametrize(
        "bad_field",
        [
            "madein",  # 오타(camelCase 아님)
            "country_of_origin",  # 별칭(snake_case)
            "OriginAreaCode",  # 대소문자 변형
            "originAreaInfo.content.value",  # 과잉 경로
            "totally_unknown_field",  # 완전 허구
        ],
    )
    def test_off_allowlist_key_rejected(self, bad_field):
        """allowlist 밖 키가 하나라도 섞이면 전체 요청 거부 (네이버 호출 0회)."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
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
                        notice=notice_override,
                        preview_confirmed=True,
                        deferred_notice_fields=["material", bad_field],
                    )

        assert result["ok"] is False, f"allowlist 밖 키({bad_field}) 가 통과하면 안 됨"
        assert (
            result.get("blocked_by") == "deferred_field_not_in_allowlist"
        ), f"blocked_by 사유가 다름: {result.get('blocked_by')}"
        assert len(naver_calls) == 0, "allowlist 거부 시 네이버 호출 0회"
        # 사유에 어느 키가 문제인지 명시.
        msg = result.get("message") or ""
        assert bad_field in msg, f"사유에 문제 필드명({bad_field}) 이 없음: {msg}"

    def test_mixed_off_and_on_list_still_rejected(self):
        """allowlist 내 키와 밖 키가 섞여도 전체 거부 (부분 적용 금지)."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material()
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
                        notice=notice_override,
                        preview_confirmed=True,
                        # material 은 allowlist 안, OriginAreaCode 는 밖.
                        deferred_notice_fields=["material", "OriginAreaCode"],
                    )

        assert result["ok"] is False, "부분 적용 금지 — 섞여 있으면 거부"
        assert result.get("blocked_by") == "deferred_field_not_in_allowlist"
        assert len(naver_calls) == 0

    def test_valid_deferred_still_works(self):
        """allowlist 안의 키만 주면 기존대로 동작 (회귀 방지)."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material()
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material", "color"],
                        )

        assert result["ok"] is True, f"allowlist 내 키는 통과해야 함: {result}"
        assert len(naver_calls) == 1


# --------------------------------------------------------------------------- #
# 7. 고시 5공통필드 미루기 값 분기 — `"1"` vs `"상세페이지 참조"`.
#
# 2026-08-06 실측(실스토어 상품 20건): 고시 35종에 공통인 5필드
# (returnCostReason · noRefundReason · qualityAssuranceStandard ·
# compensationProcedure · troubleShootingContents)는 미루기 시 값이 `"1"` 이었고,
# 그 외 고시 필드는 `"상세페이지 참조"` 였다 (섞임 0건).
#
# 본 절은 계약 (a)-(f) 를 검증한다:
#   (a) 5공통필드 미루기 → 페이로드 값이 ``"1"``.
#   (b) 비공통 필드 미루기 → ``"상세페이지 참조"`` (회귀).
#   (c) 공통·비공통 동시 미루기 → 각각 올바른 값.
#   (d) 원산지 필드는 여전히 미루기 거부 (회귀).
#   (e) 5필드 목록이 ``notice_types.json`` 교집합에서 유도됨 (비하드코딩).
#   (f) 미루기 선택 없으면 자동 채움 없음 (회귀).
# --------------------------------------------------------------------------- #

# 5공통필드를 **config 에서 채우지 않은** notice config — 공통필드가 빈 자리여야
# 미루기 대상이 될 수 있다. 기존 _NOTICE_CFG_WITH_ORIGIN 은 5공통필드를 config
# 에서 제공하므로, 미루기 테스트에는 이 축소본을 쓴다.
_NOTICE_CFG_NO_COMMON = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    # 5공통필드(return_cost_reason 등) 의도적 누락 — 빈 자리 = 미루기 대상.
}

# 5공통필드 전체(알파벳순) — 테스트 파라미터화에 쓴다.
_COMMON_5 = sorted(
    [
        "returnCostReason",
        "noRefundReason",
        "qualityAssuranceStandard",
        "compensationProcedure",
        "troubleShootingContents",
    ]
)


class TestCommonNoticeDeferredValue:
    """고시 5공통필드 미루기 시 전송값이 ``"1"`` 인지 검증 (계약 a-c)."""

    @pytest.mark.parametrize("field", _COMMON_5)
    def test_a_common_field_deferred_gets_one(self, field):
        """(a) 5공통필드 각각을 미루기로 선택 → 페이로드 값이 ``"1"``."""
        naver_calls: list[dict] = []
        # 해당 공통필드만 빼고 나머지는 채운 notice 본문을 만든다.
        all_common = {
            "returnCostReason": "반품비 테스트값",
            "noRefundReason": "환불불가 테스트값",
            "qualityAssuranceStandard": "품질보증 테스트값",
            "compensationProcedure": "보상절차 테스트값",
            "troubleShootingContents": "고장대처 테스트값",
        }
        # 테스트 대상 필드만 빈 문자열로 만든다 (미루기 대상 = 빈 자리).
        all_common[field] = ""
        notice_override = _wear_notice_without_material(extra={"material": "면 100%", **all_common})
        with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_NO_COMMON):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=[field],
                        )

        assert result["ok"] is True, f"등록 실패 ({field}): {result}"
        assert len(naver_calls) == 1
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        body = _notice_body(notice)
        assert body.get(field) == qa_agents.DEFERRED_COMMON_NOTICE_VALUE, (
            f"{field} 값이 DEFERRED_COMMON_NOTICE_VALUE({qa_agents.DEFERRED_COMMON_NOTICE_VALUE!r}) "
            f"이어야 함: {body.get(field)!r}"
        )

    def test_b_non_common_field_deferred_gets_placeholder(self):
        """(b) 비공통 필드(material) 미루기 → ``"상세페이지 참조"`` (회귀)."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material()
        # _NOTICE_CFG_WITH_ORIGIN 은 5공통필드를 config 에서 제공 — 게이트 통과.
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["material"],
                        )

        assert result["ok"] is True, f"등록 실패: {result}"
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        body = _notice_body(notice)
        assert (
            body.get("material") == qa_agents.DEFERRED_NOTICE_PLACEHOLDER
        ), f"material 값이 DEFERRED_NOTICE_PLACEHOLDER 이어야 함: {body.get('material')!r}"
        # 동시에 "1" 이 아니어야 한다.
        assert body.get("material") != qa_agents.DEFERRED_COMMON_NOTICE_VALUE

    def test_c_mixed_common_and_non_common_deferred(self):
        """(c) 공통(returnCostReason) + 비공통(material) 동시 미루기 → 각각 올바른 값."""
        naver_calls: list[dict] = []
        # 5공통필드 전부 빈 칸으로. material 도 빈 칸.
        # 미루기로 material(비공통) + 5공통필드 전부를 올림 — 게이트 통과.
        all_common_empty = {f: "" for f in _COMMON_5}
        notice_override = _wear_notice_without_material(extra={"material": "", **all_common_empty})
        deferred = ["material"] + _COMMON_5
        with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_NO_COMMON):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=deferred,
                        )

        assert result["ok"] is True, f"등록 실패: {result}"
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        body = _notice_body(notice)
        # material 은 비공통 → "상세페이지 참조".
        assert body.get("material") == qa_agents.DEFERRED_NOTICE_PLACEHOLDER
        # returnCostReason 은 공통 → "1".
        assert body.get("returnCostReason") == qa_agents.DEFERRED_COMMON_NOTICE_VALUE
        # 두 값이 서로 달라야 한다 (분기가 실제로 일어남).
        assert body.get("material") != body.get("returnCostReason")
        # 반환 보고에 material 과 5공통필드 모두 포함되어야 한다.
        reported = result.get("deferred_notice_fields")
        assert "material" in reported
        assert "returnCostReason" in reported
        assert sorted(reported) == sorted(deferred), f"미루기 적용 필드가 정확해야 함: {reported!r}"

    def test_common_field_real_value_wins_over_one(self):
        """공통필드에 실값이 있으면 `"1"` 이 아닌 실값이 전송된다 (실값 우선)."""
        naver_calls: list[dict] = []
        # 모든 필드를 실값으로 채운다 — 빈 자리가 없음.
        notice_override = _wear_notice_without_material(
            extra={
                "material": "면 100%",
                "returnCostReason": "실제 반품비 정책",
            }
        )
        # _NOTICE_CFG_WITH_ORIGIN 은 나머지 4공통필드를 config 에서 제공.
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            # returnCostReason 을 미루기로 올리지만 실값이 있음.
                            deferred_notice_fields=["returnCostReason"],
                        )

        assert result["ok"] is True, f"등록 실패: {result}"
        payload = naver_calls[0]["args"][0]
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice")
        )
        body = _notice_body(notice)
        # 실값이 우선 — "1" 이 아님.
        assert body.get("returnCostReason") == "실제 반품비 정책"
        assert body.get("returnCostReason") != qa_agents.DEFERRED_COMMON_NOTICE_VALUE
        # 보고에서도 제외되어야 함 (실값 있으면 미루기 적용 안 됨).
        reported = result.get("deferred_notice_fields")
        assert reported == [], f"실값이 있으면 미루기에서 제외: {reported!r}"


class TestCommonNoticeDerivationFromData:
    """(e) 5필드 목록이 ``notice_types.json`` 교집합에서 유도됨 (비하드코딩)."""

    def test_e_derived_set_is_exactly_5_fields(self):
        """``_common_notice_deferred_fields`` 가 정확히 5개 필드를 반환."""
        common = qa_agents._common_notice_deferred_fields()
        assert isinstance(common, frozenset)
        assert (
            len(common) == 5
        ), f"공통 필드가 정확히 5개여야 함 (현재 {len(common)}개): {sorted(common)}"

    def test_e_derived_set_matches_observed_5(self):
        """유도된 5필드가 실측 관측 결과와 일치함."""
        common = qa_agents._common_notice_deferred_fields()
        expected = frozenset(_COMMON_5)
        assert common == expected, (
            f"유도된 교집합이 실측 5필드와 다름:\n"
            f"  유도됨: {sorted(common)}\n"
            f"  실측:   {sorted(expected)}"
        )

    def test_e_derived_from_notice_types_json_not_hardcoded(self):
        """교집합이 데이터 파일의 35종 각 fields 배열에서 계산됨을 독립 검증."""
        import json

        path = _PROJECT_ROOT / "src" / "clossify" / "data" / "notice_types.json"
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        verified = doc.get("verified")
        assert isinstance(verified, list) and verified
        # 35종 각각의 fields 배열의 교집합을 독립 계산.
        field_sets = []
        for entry in verified:
            if not isinstance(entry, dict):
                continue
            fields = entry.get("fields")
            if not isinstance(fields, list):
                continue
            names = {str(n).strip() for n in fields if isinstance(n, str) and n.strip()}
            if names:
                field_sets.append(names)
        independent_common = set.intersection(*field_sets) if field_sets else set()
        # 함수 반환값과 독립 계산값이 일치해야 함.
        assert independent_common == set(
            qa_agents._common_notice_deferred_fields()
        ), "함수 반환값과 데이터 파일 독립 계산값이 불일치 — 캐시 오염 가능성"

    def test_e_branch_function_uses_derived_set(self):
        """``_deferred_value_for_field`` 가 유도된 교집합을 기준으로 분기함."""
        common = qa_agents._common_notice_deferred_fields()
        for field in common:
            assert qa_agents._deferred_value_for_field(field) == (
                qa_agents.DEFERRED_COMMON_NOTICE_VALUE
            ), f"공통필드 {field} 가 COMMON_VALUE 로 분기하지 않음"
        # allowlist 에서 공통이 아닌 필드 하나를 골라 비공통 분기 확인.
        non_common = "material"
        assert non_common not in common, "전제: material 은 공통필드가 아님"
        assert qa_agents._deferred_value_for_field(non_common) == (
            qa_agents.DEFERRED_NOTICE_PLACEHOLDER
        ), f"비공통필드 {non_common} 가 PLACEHOLDER 로 분기하지 않음"


class TestSentinelDetection:
    """``_is_deferred_sentinel_value`` 가 두 토큰 모두 인식하는지 검증."""

    def test_recognizes_placeholder(self):
        assert qa_agents._is_deferred_sentinel_value(qa_agents.DEFERRED_NOTICE_PLACEHOLDER) is True

    def test_recognizes_common_value(self):
        assert qa_agents._is_deferred_sentinel_value(qa_agents.DEFERRED_COMMON_NOTICE_VALUE) is True

    def test_rejects_real_value(self):
        assert qa_agents._is_deferred_sentinel_value("면 100%") is False

    def test_rejects_none(self):
        assert qa_agents._is_deferred_sentinel_value(None) is False

    def test_rejects_empty_string(self):
        assert qa_agents._is_deferred_sentinel_value("") is False


class TestDeferredCommonValueRegression:
    """(d, f) 기존 계약 회귀 — 분기 도입으로 기존 동작이 깨지지 않음."""

    def test_d_origin_field_still_refused(self):
        """(d) 원산지 필드는 공통필드 도입 후에도 여전히 미루기 거부."""
        naver_calls: list[dict] = []
        notice_override = _wear_notice_without_material(extra={"material": "면 100%"})
        with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_NO_COMMON):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_common_cfg_origin()):
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
                            preview_confirmed=True,
                            deferred_notice_fields=["madeIn"],
                        )

        assert result["ok"] is False
        assert result.get("blocked_by") == "origin_field_not_deferrable"
        assert len(naver_calls) == 0

    def test_f_no_deferral_no_autofill_common(self):
        """(f) 미루기 선택 없으면 공통필드 빈 칸에 자동채움 없음 (차단)."""
        naver_calls: list[dict] = []
        # 5공통필드를 빈 칸으로 두고 미루기 선택 없이 등록 시도.
        notice_override = _wear_notice_without_material(
            extra={
                "material": "면 100%",
                "returnCostReason": "",
                "noRefundReason": "",
                "qualityAssuranceStandard": "",
                "compensationProcedure": "",
                "troubleShootingContents": "",
            }
        )
        with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_NO_COMMON):
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
                        notice=notice_override,
                        preview_confirmed=True,
                        # deferred_notice_fields 생략 — 빈 칸은 차단.
                    )

        assert result["ok"] is False, "공통필드 빈 칸인데 미루기 없으면 차단되어야 함"
        assert result.get("blocked_by") == "compliance"
        assert len(naver_calls) == 0
