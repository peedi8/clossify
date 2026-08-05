"""고시 payload 조립기 35종 전체 확장 검증 테스트.

검증 시나리오:
  1. WEAR 타입 → payload 에 ``wear`` 노드로 실림(``etc`` 아님).
  2. FOOD, COSMETIC, KIDS 각각 자기 노드명으로 실림.
  3. ETC, FURNITURE 기존 동작 무회귀.
  4. 알 수 없는 타입(``__NOPE__``) → 에러(조용한 etc 폴백 아님).
  5. 공통 5필드가 config 기본값으로 채워지고, 상품별 입력이 우선.
  6. MCP 통합 반례: 의류 카테고리 + WEAR 필수 필드 완비 + config 원산지 일치 →
     ``register_product`` 가 차단하지 않고 등록 경로 진입.
  7. 기존 테스트 102개 무회귀, 도구 6개.

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로 네트워크 차단.
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

from clossify import common, mcp_server, naver_client

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

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
    "importer": "테스트수입사",
}

# 의류 카테고리 (MCP 통합 반례용).
_CLOTHING_CATEGORY = "50021299"


def _make_product(notice_type=None, node_key=None, body=None, extra_product=None):
    """테스트용 상품 dict 를 만든다."""
    p = {
        "name": "테스트상품",
        "categoryId": "50000000",
        "salePrice": 30000,
        "origin_code": "04",
        "made_in": "중국",
    }
    if extra_product:
        p.update(extra_product)
    if notice_type:
        notice = {"productInfoProvidedNoticeType": notice_type}
        if node_key and body is not None:
            notice[node_key] = body
        p["notice"] = notice
    return p


def _build_notice(p):
    """naver_client._notice_defaults + _product_info_notice 를 호출."""
    with mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            defaults = naver_client._notice_defaults(p)
            return naver_client._product_info_notice(p, defaults)


# --------------------------------------------------------------------------- #
# 1. WEAR → wear 노드 (핵심 반례).
# --------------------------------------------------------------------------- #
class TestWearNode:
    """WEAR 타입이 ``wear`` 노드로 실리는가 (`etc` 아님)."""

    def test_wear_type_produces_wear_node(self):
        """``productInfoProvidedNoticeType: WEAR`` → ``wear`` 키 존재."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={"material": "면 100%", "color": "블랙", "size": "FREE"},
        )
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == "WEAR"
        assert "wear" in notice, "wear 노드가 없음"
        assert "etc" not in notice, "etc 노드가 존재함 (노드 불일치 결함)"

    def test_wear_body_carries_user_fields(self):
        """사용자 입력 필드가 wear 본문에 실리는가."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면 100%",
                "color": "블랙",
                "size": "95",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 교환",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        assert wear["material"] == "면 100%"
        assert wear["color"] == "블랙"
        assert wear["size"] == "95"


# --------------------------------------------------------------------------- #
# 2. FOOD, COSMETIC, KIDS 각각 자기 노드명.
# --------------------------------------------------------------------------- #
class TestOtherTypeNodes:
    """FOOD/COSMETIC/KIDS 가 각각 자기 노드명으로 실리는가."""

    @pytest.mark.parametrize(
        "notice_type,expected_node",
        [
            ("FOOD", "food"),
            ("COSMETIC", "cosmetic"),
            ("KIDS", "kids"),
            ("SHOES", "shoes"),
            ("BAG", "bag"),
            ("BOOKS", "books"),
            ("FASHION_ITEMS", "fashionItems"),
            ("HOME_APPLIANCES", "homeAppliances"),
            ("BIOCHEMISTRY", "biochemistry"),
            ("BIOCIDAL", "biocidal"),
            ("SPORTS_EQUIPMENT", "sportsEquipment"),
            ("MUSICAL_INSTRUMENT", "musicalInstrument"),
            ("SLEEPING_GEAR", "sleepingGear"),
            ("JEWELLERY", "jewellery"),
            ("GENERAL_FOOD", "generalFood"),
            ("DIET_FOOD", "dietFood"),
            ("DIGITAL_CONTENTS", "digitalContents"),
            ("GIFT_CARD", "giftCard"),
            ("MOBILE_COUPON", "mobileCoupon"),
            ("RENTAL_ETC", "rentalEtc"),
            ("ETC_SERVICE", "etcService"),
            ("IMAGE_APPLIANCES", "imageAppliances"),
            ("SEASON_APPLIANCES", "seasonAppliances"),
            ("OFFICE_APPLIANCES", "officeAppliances"),
            ("CELLPHONE", "cellPhone"),
            ("OPTICS_APPLIANCES", "opticsAppliances"),
            ("MICROELECTRONICS", "microElectronics"),
            ("NAVIGATION", "navigation"),
            ("CAR_ARTICLES", "carArticles"),
            ("MEDICAL_APPLIANCES", "medicalAppliances"),
            ("KITCHEN_UTENSILS", "kitchenUtensils"),
            ("MOVIE_SHOW", "movieShow"),
        ],
    )
    def test_node_name_matches_data_spec(self, notice_type, expected_node):
        """타입별 node 이름이 data/notice_types.json 스펙과 일치하는가."""
        p = _make_product(
            notice_type=notice_type,
            node_key=expected_node,
            body={"returnCostReason": "테스트"},
        )
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == notice_type
        assert (
            expected_node in notice
        ), f"{notice_type} → {expected_node} 노드가 없음. keys={list(notice.keys())}"
        assert (
            "etc" not in notice or notice_type == "ETC"
        ), f"{notice_type} 인데 etc 노드가 존재함 (노드 불일치)"


# --------------------------------------------------------------------------- #
# 3. ETC, FURNITURE 기존 동작 무회귀.
# --------------------------------------------------------------------------- #
class TestEtcFurnitureRegression:
    """ETC/FURNITURE 기존 동작이 보존되는가."""

    def test_etc_default_when_no_type_given(self):
        """타입 명시 없이 호출해도 ETC 기본값으로 동작."""
        p = _make_product(extra_product={"made_in": "중국"})
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == "ETC"
        assert "etc" in notice

    def test_etc_explicit_type(self):
        """명시적 ETC 타입도 etc 노드로 실림."""
        p = _make_product(notice_type="ETC", node_key="etc", body={})
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == "ETC"
        assert "etc" in notice
        assert "furniture" not in notice

    def test_furniture_explicit_type(self):
        """FURNITURE 타입은 furniture 노드로 실림."""
        p = _make_product(
            notice_type="FURNITURE",
            node_key="furniture",
            body={"material": "원목", "color": "월넛"},
        )
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == "FURNITURE"
        assert "furniture" in notice
        assert "etc" not in notice

    def test_furniture_category_inference(self):
        """카테고리 경로에 '가구' 가 있으면 FURNITURE 로 추론."""
        p = _make_product(extra_product={"category_path": "가구>의자>사무용의자"})
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == "FURNITURE"
        assert "furniture" in notice

    def test_etc_body_has_legacy_fields(self):
        """ETC 본문에 기존 필드(itemName, manufacturer 등)가 있는가."""
        p = _make_product(notice_type="ETC")
        notice = _build_notice(p)
        etc = notice["etc"]
        assert "itemName" in etc
        assert "manufacturer" in etc
        assert "returnCostReason" in etc


# --------------------------------------------------------------------------- #
# 4. 알 수 없는 타입 → 에러 (조용한 etc 폴백 금지).
# --------------------------------------------------------------------------- #
class TestUnknownTypeError:
    """알 수 없는 타입이 주어지면 ValueError."""

    def test_unknown_type_raises(self):
        """``__NOPE__`` 타입 → ValueError."""
        p = _make_product(
            notice_type="__NOPE__",
            node_key="__NOPE__",
            body={},
        )
        with pytest.raises(ValueError, match="알 수 없는 고시 타입"):
            _build_notice(p)

    def test_unknown_type_no_etc_fallback(self):
        """알 수 없는 타입이 etc 로 폴백하지 않음을 보장."""
        p = _make_product(
            notice_type="__NOPE__",
            node_key="__NOPE__",
            body={},
        )
        try:
            _build_notice(p)
            assert False, "ValueError 가 발생해야 함"
        except ValueError:
            pass

    def test_empty_notice_type_does_not_raise(self):
        """빈 notice_type (명시 없음) → ETC 기본값 (회귀 보존)."""
        p = _make_product()
        # 에러 없이 ETC 로 떨어져야 함.
        notice = _build_notice(p)
        assert notice["productInfoProvidedNoticeType"] == "ETC"


# --------------------------------------------------------------------------- #
# 5. 공통 5필드 자동 채움 + 사용자 입력 우선.
# --------------------------------------------------------------------------- #
class TestCommonFields:
    """공통 5필드(returnCostReason 등) 자동 채움 및 우선순위."""

    _COMMON_5 = (
        "returnCostReason",
        "noRefundReason",
        "qualityAssuranceStandard",
        "compensationProcedure",
        "troubleShootingContents",
    )

    def test_common_fields_filled_from_config(self):
        """공통 5필드가 config 기본값으로 채워지는가."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
                "color": "블랙",
                "size": "FREE",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        for field in self._COMMON_5:
            assert field in wear, f"{field} 가 없음"
            assert (
                wear[field]
                == _NOTICE_CFG_WITH_ORIGIN[
                    {
                        "returnCostReason": "return_cost_reason",
                        "noRefundReason": "no_refund_reason",
                        "qualityAssuranceStandard": "quality_assurance_standard",
                        "compensationProcedure": "compensation_procedure",
                        "troubleShootingContents": "trouble_shooting_contents",
                    }[field]
                ]
            )

    def test_user_value_overrides_config_default(self):
        """사용자 입력값이 config 기본값보다 우선하는가."""
        user_return_cost = "사용자정의반품비용"
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
                "color": "블랙",
                "size": "FREE",
                "returnCostReason": user_return_cost,
            },
        )
        notice = _build_notice(p)
        assert notice["wear"]["returnCostReason"] == user_return_cost

    def test_common_fields_in_all_types(self):
        """모든 데이터 기반 타입에 공통 5필드가 채워지는가."""
        # 몇 가지 대표 타입만 검증 (전체는 TestOtherTypeNodes 에서 커버).
        for notice_type, node in [
            ("FOOD", "food"),
            ("COSMETIC", "cosmetic"),
            ("KIDS", "kids"),
            ("SHOES", "shoes"),
        ]:
            p = _make_product(
                notice_type=notice_type,
                node_key=node,
                body={},
            )
            notice = _build_notice(p)
            body = notice[node]
            for field in self._COMMON_5:
                assert field in body, f"{notice_type}/{node} 에 {field} 없음"


# --------------------------------------------------------------------------- #
# 6. MCP 통합 반례: 의류 + WEAR 완비 → 등록 경로 진입.
# --------------------------------------------------------------------------- #
class TestMcpIntegration:
    """MCP register_product 가 WEAR 완비 시 등록 경로로 진입하는가."""

    def test_wear_complete_passes_compliance_and_calls_naver(self):
        """의류 + WEAR 필수필드 완비 + config 원산지 일치 → 네이버 호출."""
        naver_calls = []
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 교환 가능",
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
                        side_effect=lambda *a, **k: (
                            naver_calls.append({"args": a, "kwargs": k})
                            or (200, {"originProductNo": "test-123"})
                        ),
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
        assert result["ok"] is True, f"등록 실패: {result}"
        assert len(naver_calls) == 1, f"네이버 API 호출 횟수가 예상과 다름: {len(naver_calls)}"

    def test_wear_uses_wear_node_in_payload(self):
        """register_product 호출 시 payload 에 wear 노드로 실리는가."""
        captured_payload = {}

        def capture(payload, tk=None):
            captured_payload.update(payload)
            return (200, {"originProductNo": "test-123"})

        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁 가능",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "구매 후 7일 교환 가능",
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
                    with mock.patch.object(naver_client, "register_product", side_effect=capture):
                        result = mcp_server.register_product(
                            name="테스트니트",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                            notice=notice_override,
                            preview_confirmed=True,
                        )
        assert result["ok"] is True
        notice = (
            captured_payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        assert notice.get("productInfoProvidedNoticeType") == "WEAR"
        assert "wear" in notice, "payload 에 wear 노드가 없음"
        assert "etc" not in notice, "payload 에 etc 노드가 있음 (결함)"

    def test_unknown_type_blocks_at_register_product(self):
        """알 수 없는 타입 → register_product 가 payload 빌드 실패로 거부."""
        notice_override = {
            "productInfoProvidedNoticeType": "__NOPE__",
            "__NOPE__": {},
        }
        with mock.patch.object(
            naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
        ):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(naver_client, "register_product"):
                    result = mcp_server.register_product(
                        name="테스트",
                        price=30000,
                        image_urls=["http://cdn/x.png"],
                        category_id=_CLOTHING_CATEGORY,
                        detail_html="<html><body>상세</body></html>",
                        notice=notice_override,
                        preview_confirmed=True,
                    )
        assert result["ok"] is False
        # 에러 메시지에 관련 안내 포함.
        assert result.get("error") is not None


# --------------------------------------------------------------------------- #
# 7. 데이터 기반 검증: 35종 전체 지원 + 노드명 일관성.
# --------------------------------------------------------------------------- #
class TestThirtyFiveTypesSupport:
    """data/notice_types.json 의 verified 35종 전체를 지원하는가."""

    def test_all_verified_types_produce_correct_node(self):
        """verified 목록의 모든 타입이 자기 node 키로 실리는가."""
        specs = naver_client._load_notice_type_specs()
        assert len(specs) == 35, f"verified 타입이 35종이 아님: {len(specs)}"
        for spec in specs:
            notice_type = spec["type"]
            node = spec["node"]
            if notice_type in ("ETC", "FURNITURE"):
                continue  # 별도 테스트에서 커버
            p = _make_product(
                notice_type=notice_type,
                node_key=node,
                body={"returnCostReason": "테스트"},
            )
            notice = _build_notice(p)
            assert notice["productInfoProvidedNoticeType"] == notice_type
            assert node in notice, f"{notice_type} → {node} 노드 없음: keys={list(notice.keys())}"

    def test_type_count_is_35(self):
        """data/notice_types.json 의 verified 타입 수가 35인가."""
        specs = naver_client._load_notice_type_specs()
        assert len(specs) == 35

    def test_no_hardcoded_node_outside_data(self):
        """_product_info_notice 가 데이터 외의 노드명을 생성하지 않는가."""
        specs = naver_client._load_notice_type_specs()
        valid_nodes = {spec["node"] for spec in specs}
        for spec in specs:
            if spec["type"] in ("ETC", "FURNITURE"):
                continue
            p = _make_product(
                notice_type=spec["type"],
                node_key=spec["node"],
                body={"returnCostReason": "x"},
            )
            notice = _build_notice(p)
            keys = set(notice.keys()) - {"productInfoProvidedNoticeType"}
            assert keys <= valid_nodes, f"{spec['type']}: 데이터 외 노드 {keys - valid_nodes}"


# --------------------------------------------------------------------------- #
# 8. 무동작/identity 금지 검증.
# --------------------------------------------------------------------------- #
class TestNoNoOp:
    """빈 본문/무동작이 아닌 실제 조립이 일어나는가."""

    def test_wear_body_is_not_empty(self):
        """WEAR 본문이 비어있지 않은가 (공통필드 최소)."""
        p = _make_product(notice_type="WEAR", node_key="wear", body={})
        notice = _build_notice(p)
        wear = notice["wear"]
        assert len(wear) >= 5, f"wear 본문이 너무 작음 (공통필드 누락?): {wear}"

    def test_user_fields_not_fabricated(self):
        """사용자가 주지 않은 타입별 필드를 지어내지 않는가."""
        p = _make_product(
            notice_type="WEAR",
            node_key="wear",
            body={
                "material": "면",
            },
        )
        notice = _build_notice(p)
        wear = notice["wear"]
        # material 은 사용자가 줌.
        assert wear.get("material") == "면"
        # color, size 등은 사용자가 주지 않았으므로 채워지지 않아야 함
        # (공통 5필드 + afterServiceDirector + manufacturer 제외).
        _autofilled = set(naver_client._NOTICE_COMMON_FIELDS) | {
            "afterServiceDirector",
            "manufacturer",
        }
        for field in ("color", "size", "caution", "packDateText"):
            assert field not in wear, f"사용자가 주지 않은 {field} 를 지어냄: {wear.get(field)}"


# --------------------------------------------------------------------------- #
# 9. 도구 6개 등록 유지 (무회귀).
# --------------------------------------------------------------------------- #
class TestToolRegistrationPreserved:
    """변경 후에도 6개 도구가 등록되어 있는가."""

    def test_tool_count_registered(self):
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        assert len(tools) == 6, f"도구가 6개여야 함: {len(tools)}"

    def test_register_product_signature_unchanged(self):
        """register_product 파라미터가 유지되는가.

        ``preview_confirmed`` 는 미리보기 승인 게이트로
        추가된 키워드 인자다. 기본값 ``False`` 이며, 설정의
        ``require_preview_confirmation`` 이 켜져 있을 때 게이트로 동작한다.
        ``option_groups`` 는 다축 옵션 조합의 그룹 이름(예:
        ``["색상","사이즈"]``) 을 ``naver_client._build_option_info`` 로
        전달하기 위해 끝에 추가된 키워드 전용 인자다. 이 키가 없으면
        그룹 이름이 "옵션1"/"옵션2" 등의 번호 폴백으로 떨어진다.
        """
        import inspect

        sig = inspect.signature(mcp_server.register_product)
        param_names = list(sig.parameters.keys())
        expected = [
            "name",
            "price",
            "image_urls",
            "category_id",
            "detail_html",
            "product_key",
            "options",
            "tags",
            "status",
            "stock",
            "delivery_fee",
            "courier",
            "notice",
            "preview_confirmed",
            "option_groups",
        ]
        assert param_names == expected, f"시그니처 변경 감지: {param_names}"
