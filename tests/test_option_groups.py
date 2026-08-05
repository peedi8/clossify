# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUsage-1.0
"""다축 옵션 그룹 이름 전단(phi) 검증 테스트.

이 테스트 모듈은 ``mcp_server.register_product`` 의 ``option_groups``
키워드 전용 인자가 실제 네이버 커머스 API 송신 페이로드의
``optionCombinationGroupNames`` 까지 도달하는지를 HTTP 전송 계층에서
캡처해 확인한다. 결함의 본질은 그룹 이름이 도달하지 못하고
``"옵션1"``/``"옵션2"`` 번호 폴백으로 떨어지는 것이었으므로, 송신 본문에서
실제로 그 이름을 봐야 한다 (중간 dict 가 아니라).

검증 시나리오:
  1. ``option_groups=["색상","사이즈"]`` → 송신 payload 의
     ``optionCombinationGroupNames.optionGroupName1/2`` 가 그 이름을 실음.
  2. ``option_groups`` 생략 → 기존 번호 폴백("옵션1"/"옵션2")이 유지됨.
  3. 무효값(빈 문자열 항목, 비-리스트, 4개 초과) → 네이버 호출 0회로 거부.
  4. (시그니처 화이트리스트 갱신은 test_compliance_gate_bypass.py 와
     test_notice_type_nodes.py 에서 다룬다 — 이 파일은 아님.)

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로
``naver_client.register_product`` 를 캡처 mock 으로 대체한다.
``COMMERCE_DRY_RUN`` 도 꺼진 상태로 둔다 (캡처 mock 이 우선).
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

from clossify import common, mcp_server, naver_client, register

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

# 의류 카테고리 (KC 불필요, WEAR 고시 타입).
_CLOTHING_CATEGORY = "50021299"

# notice_config mock: 원산지·AS 전화·공통 5필드가 정상 설정된 config.
# 컴플라이언스 게이트 통과를 위해 WEAR 필수 필드를 notice override 로 채운다.
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


def _wear_notice_override():
    """WEAR 필수 필드를 완비한 notice override 를 반환.

    컴플라이언스 게이트를 통과하기 위해 필요하다 — 옵션 그룹 이름 검증은
    게이트 *이전* 단계에서 일어나지만, "정상 경로 진입" 시나리오(1, 2)는
    게이트도 통과해야 송신 페이로드를 캡처할 수 있다.
    """
    return {
        "productInfoProvidedNoticeType": "WEAR",
        "wear": {
            "material": "면 100%",
            "color": "블랙",
            "size": "FREE",
            "caution": "물 세탁 가능",
            "packDateText": "2024-01-01",
            "warrantyPolicy": "구매 후 7일 이내 교환 가능",
        },
    }


def _two_axis_options():
    """2축 옵션 조합(색상 x 사이즈) 리스트.

    ``_option_width`` 가 ``optionName1``/``optionName2`` 쌍에서 2 를
    산출하도록 한다. ``option_groups`` 가 없으면 ``"옵션1"``/``"옵션2"``
    폴백이, 있으면 그 값이 ``optionCombinationGroupNames`` 에 실린다.
    """
    return [
        {"optionName1": "아이보리", "optionName2": "S", "stock": 3, "price": 0},
        {"optionName1": "아이보리", "optionName2": "M", "stock": 5, "price": 0},
        {"optionName1": "블랙", "optionName2": "M", "stock": 2, "price": 1000},
    ]


def _ctx_mocks():
    """컴플라이언스 게이트가 WEAR 통과 판정을 내리도록 하는 context manager 스택.

    ``_compliance_code_check`` 가 ``common.cfg()`` 를 직접 읽기 때문에,
    CI(config.example.json)의 플레이스홀더 원산지와 충돌한다.
    ``_notice_config`` mock 값과 일치하도록 ``common.cfg`` 도 함께 덮어쓴다.
    """
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common,
            "cfg",
            return_value={
                "smartstore_notice_defaults": {
                    "origin_area_code": "04",
                    "origin_content": "중국",
                },
            },
        ),
    )


# --------------------------------------------------------------------------- #
# 1. option_groups 전달 → optionCombinationGroupNames 에 실림 (HTTP 계층 캡처).
# --------------------------------------------------------------------------- #
class TestOptionGroupsReachPayload:
    """``option_groups=["색상","사이즈"]`` 가 송신 페이로드에 도달하는가."""

    def test_group_names_carry_user_values_in_outgoing_payload(self):
        """송신 payload 의 optionCombinationGroupNames 가 사용자가 준 이름을 실는가.

        핵심 반례: 결함의 본질은 그룹 이름이 도달하지 못하고 "옵션1"/"옵션2"
        폴백으로 떨어지는 것이었다. 캡처 mock 으로 *실제 송신 본문* 을 잡아
        optionGroupName1/2 가 "색상"/"사이즈" 인지 확인한다.
        """
        captured_payload: dict = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-origin-option-1"})

        notice_override = _wear_notice_override()
        options = _two_axis_options()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=capture):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    options=options,
                    preview_confirmed=True,
                    option_groups=["색상", "사이즈"],
                )

        # 등록 자체는 성공해야 한다 (옵션 그룹 이름은 등록 실패 사유가 아님).
        assert result["ok"] is True, f"등록 실패: {result}"
        # 캡처된 송신 payload 에서 optionInfo 를 찾는다.
        option_info = (
            captured_payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("optionInfo", {})
        )
        group_names = option_info.get("optionCombinationGroupNames") or {}
        assert (
            group_names.get("optionGroupName1") == "색상"
        ), f"optionGroupName1 이 '색상' 이 아님: {group_names}"
        assert (
            group_names.get("optionGroupName2") == "사이즈"
        ), f"optionGroupName2 가 '사이즈' 가 아님: {group_names}"
        # 폴백 이름이 섞이지 않았는지 확인 (결함 재발 방지).
        assert (
            "옵션1" not in group_names.values()
        ), f"폴백 '옵션1' 이 그룹 이름에 섞여 있음: {group_names}"
        assert (
            "옵션2" not in group_names.values()
        ), f"폴백 '옵션2' 가 그룹 이름에 섞여 있음: {group_names}"

    def test_three_axis_group_names_all_carry_user_values(self):
        """3축 옵션에서도 세 그룹 이름 모두 사용자 값으로 실리는가."""
        captured_payload: dict = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-origin-option-3axis"})

        notice_override = _wear_notice_override()
        options = [
            {"optionName1": "블랙", "optionName2": "M", "optionName3": "단품", "stock": 1},
            {"optionName1": "블랙", "optionName2": "L", "optionName3": "세트", "stock": 1},
        ]
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=capture):
                result = mcp_server.register_product(
                    name="테스트셔츠",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    options=options,
                    preview_confirmed=True,
                    option_groups=["색상", "사이즈", "구성"],
                )

        assert result["ok"] is True, f"등록 실패: {result}"
        option_info = (
            captured_payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("optionInfo", {})
        )
        group_names = option_info.get("optionCombinationGroupNames") or {}
        assert group_names.get("optionGroupName1") == "색상"
        assert group_names.get("optionGroupName2") == "사이즈"
        assert group_names.get("optionGroupName3") == "구성"


# --------------------------------------------------------------------------- #
# 2. option_groups 생략 → 기존 번호 폴백 유지.
# --------------------------------------------------------------------------- #
class TestOptionGroupsOmittedKeepsFallback:
    """``option_groups`` 를 주지 않으면 기존 폴백 동작이 유지되는가."""

    def test_omitted_option_groups_uses_numbered_fallback(self):
        """``option_groups`` 생략 시 ``"옵션1"``/``"옵션2"`` 폴백이 실리는가.

        이것은 회귀 방어용이다 — 새 인자가 기존 동작을 바꾸지 않음을 보인다.
        """
        captured_payload: dict = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-origin-option-fb"})

        notice_override = _wear_notice_override()
        options = _two_axis_options()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=capture):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    options=options,
                    preview_confirmed=True,
                    # option_groups 생략 — 기본값 None.
                )

        assert result["ok"] is True, f"등록 실패: {result}"
        option_info = (
            captured_payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("optionInfo", {})
        )
        group_names = option_info.get("optionCombinationGroupNames") or {}
        # 번호 폴백이 여전히 동작함 — 인자 생략이 동작을 바꾸지 않음.
        assert (
            group_names.get("optionGroupName1") == "옵션1"
        ), f"생략 시 폴백 '옵션1' 이 와야 함: {group_names}"
        assert (
            group_names.get("optionGroupName2") == "옵션2"
        ), f"생략 시 폴백 '옵션2' 가 와야 함: {group_names}"


# --------------------------------------------------------------------------- #
# 3. 무효값 → 네이버 호출 0회로 거부.
# --------------------------------------------------------------------------- #
class TestOptionGroupsValidationRefuses:
    """무효 ``option_groups`` 값은 네이버 호출 없이 거부하는가.

    검증은 ``status`` 검증 직후에 일어나므로, 컴플라이언스 게이트보다 *먼저*
    실행된다. 하지만 네이버 API 호출 자체가 일어나지 않는 것이 핵심 계약이다.
    """

    def test_non_list_refused_without_api_call(self):
        """리스트가 아닌 값(예: 문자열) → 거부, 네이버 호출 0회."""
        naver_calls: list = []

        def recorder(*args, **kwargs):
            naver_calls.append({"args": args, "kwargs": kwargs})
            return (200, {"originProductNo": "should-not-happen"})

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=recorder):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    preview_confirmed=True,
                    option_groups="색상",  # 문자열 — 리스트가 아님.
                )

        assert result["ok"] is False, "문자열 option_groups 는 거부되어야 함"
        assert len(naver_calls) == 0, f"네이버 API 가 호출됨: {naver_calls}"
        assert result.get("error") is not None

    def test_empty_string_entry_refused_without_api_call(self):
        """빈 문자열 항목이 섞인 리스트 → 거부, 네이버 호출 0회."""
        naver_calls: list = []

        def recorder(*args, **kwargs):
            naver_calls.append({"args": args, "kwargs": kwargs})
            return (200, {"originProductNo": "should-not-happen"})

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=recorder):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    preview_confirmed=True,
                    option_groups=["색상", ""],  # 빈 문자열 항목.
                )

        assert result["ok"] is False, "빈 문자열 항목은 거부되어야 함"
        assert len(naver_calls) == 0, f"네이버 API 가 호출됨: {naver_calls}"

    def test_whitespace_only_entry_refused_without_api_call(self):
        """공백만 있는 항목 → 거부 (strip 후 빈 문자열이 되므로)."""
        naver_calls: list = []

        def recorder(*args, **kwargs):
            naver_calls.append({"args": args, "kwargs": kwargs})
            return (200, {"originProductNo": "should-not-happen"})

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=recorder):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    preview_confirmed=True,
                    option_groups=["색상", "   "],  # 공백만 있음.
                )

        assert result["ok"] is False
        assert len(naver_calls) == 0

    def test_non_string_entry_refused_without_api_call(self):
        """비-문자열 항목(예: 정수) → 거부."""
        naver_calls: list = []

        def recorder(*args, **kwargs):
            naver_calls.append({"args": args, "kwargs": kwargs})
            return (200, {"originProductNo": "should-not-happen"})

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=recorder):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    preview_confirmed=True,
                    option_groups=["색상", 42],  # 정수 항목.
                )

        assert result["ok"] is False
        assert len(naver_calls) == 0

    def test_more_than_three_refused_without_api_call(self):
        """리스트 길이 4 이상 → 거부 (네이버는 최대 3축)."""
        naver_calls: list = []

        def recorder(*args, **kwargs):
            naver_calls.append({"args": args, "kwargs": kwargs})
            return (200, {"originProductNo": "should-not-happen"})

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=recorder):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    preview_confirmed=True,
                    option_groups=["색상", "사이즈", "구성", "소재"],  # 4개 — 초과.
                )

        assert result["ok"] is False, "4개 항목은 거부되어야 함"
        assert len(naver_calls) == 0

    def test_empty_list_refused_without_api_call(self):
        """빈 리스트 → 거부 (길이 1~3 범위 밖)."""
        naver_calls: list = []

        def recorder(*args, **kwargs):
            naver_calls.append({"args": args, "kwargs": kwargs})
            return (200, {"originProductNo": "should-not-happen"})

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=recorder):
                result = mcp_server.register_product(
                    name="테스트니트",
                    price=30000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    preview_confirmed=True,
                    option_groups=[],  # 빈 리스트.
                )

        assert result["ok"] is False
        assert len(naver_calls) == 0

    def test_single_group_accepted_and_reaches_payload(self):
        """길이 1 리스트는 유효범위(1~3) 안이므로 거부되지 않는다."""
        captured_payload: dict = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-origin-option-single"})

        notice_override = _wear_notice_override()
        # 단일 축 옵션 — optionName1 만.
        options = [
            {"optionName1": "블랙", "stock": 2, "price": 0},
            {"optionName1": "아이보리", "stock": 3, "price": 0},
        ]
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            with mock.patch.object(naver_client, "register_product", side_effect=capture):
                result = mcp_server.register_product(
                    name="테스트티",
                    price=20000,
                    image_urls=["http://cdn/x.png"],
                    category_id=_CLOTHING_CATEGORY,
                    detail_html="<html><body>상세</body></html>",
                    notice=notice_override,
                    options=options,
                    preview_confirmed=True,
                    option_groups=["색상"],
                )

        assert result["ok"] is True, f"길이 1 리스트는 허용되어야 함: {result}"
        option_info = (
            captured_payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("optionInfo", {})
        )
        group_names = option_info.get("optionCombinationGroupNames") or {}
        assert group_names.get("optionGroupName1") == "색상"


# --------------------------------------------------------------------------- #
# 4. 도구 7개 / 시그니처 — 회귀 방어 (option_groups 추가에도 도구 수 불변).
# --------------------------------------------------------------------------- #
class TestToolCountPreserved:
    """``option_groups`` 추가에도 MCP 도구가 7개로 유지되는가.

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


# --------------------------------------------------------------------------- #
# 5. prepare_listing 이 option_groups 를 prepared product block 에 보존하는가.
#
# 결함의 본질: mcp_server.register_product 의 직접 경로는 option_groups 를
# product dict 에 싣어 optionCombinationGroupNames 까지 도달시킨다(위 클래스들).
# 그러나 register.prepare_listing 이 저장하는 product block 이 키를 열거하는
# 구조이고 option_groups 가 거기에 없으면, prepared 에서 출발하는 등록 경로
# (register_prepared_listing) 는 product block 에서 option_groups 를 읽지
# 못해 폴백 "옵션1"/"옵션2" 로 떨어진다 — 두 경로가 서로 다른 것을 보는
# "두 곳이 불일치" 결함이 다시 생긴다. 본 클래스는 저장→재로드 왕복으로
# option_groups 가 prepared product block 에 보존되는지 검증한다.
# --------------------------------------------------------------------------- #
class TestPrepareListingPreservesOptionGroups:
    """``prepare_listing`` 이 ``option_groups`` 를 product block 에 저장하는가."""

    def test_option_groups_stored_in_prepared_product_block(self, monkeypatch):
        """prepare_listing 에 option_groups 를 주면 저장된 product block 에 실리는가.

        저장소(load_prepared_payload)에서 읽어 검증한다 — prepare_listing 이
        반환한 메모리 객체가 아니라 디스크에 쓰인 내용이 핵심이다. 이것이
        빠지면 register_prepared_listing 경로가 그룹 이름을 잃는다.
        """
        import tempfile

        # PREPARED_DIR 을 임시 디렉터리로 격리 — 다른 테스트/실제 prepared
        # payload 와 충돌하지 않게 한다.
        prepared_dir = Path(tempfile.mkdtemp(prefix="clossify_optgroups_")) / "prepared"
        prepared_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(common, "PREPARED_DIR", prepared_dir)

        def _fake_attach_ok(sources):
            urls = [f"http://cdn/test/img{i}.png" for i in range(len(sources))]
            return {"urls": urls, "rejected": [], "notes": []}

        notice_override = _wear_notice_override()
        ctx1, ctx2, ctx3 = _ctx_mocks()
        with ctx1, ctx2, ctx3:
            payload = register.prepare_listing(
                {
                    "name": "옵션그룹보존니트",
                    "salePrice": 30000,
                    "image_sources": ["a.png"],
                    "category_id": _CLOTHING_CATEGORY,
                    "notice": notice_override,
                    "options": _two_axis_options(),
                    "option_groups": ["색상", "사이즈"],
                },
                attach_fn=_fake_attach_ok,
            )

        # 반환된 payload 의 product block 에도 있어야 하고,
        product_block = payload.get("product") or {}
        assert product_block.get("option_groups") == [
            "색상",
            "사이즈",
        ], f"반환 payload 의 product.option_groups 가 잘못됨: {product_block.get('option_groups')}"

        # 핵심: 디스크에서 재로드해도 product block 에 option_groups 가 살아있어야 한다.
        # 이 경로가 register_prepared_listing 이 읽는 자리다.
        reloaded = register.load_prepared_payload(product_key=payload["product_key"])
        reloaded_product = reloaded.get("product") or {}
        assert reloaded_product.get("option_groups") == [
            "색상",
            "사이즈",
        ], (
            "디스크에서 재로드한 prepared product block 의 option_groups 가 잘못됨: "
            f"{reloaded_product.get('option_groups')} "
            "(register_prepared_listing 경로가 그룹 이름을 잃게 됨)"
        )
