# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""고시 필드 관계(XOR 상호배제)와 철자 정정 — 실등록에서 확정된 계약 검증.

본 테스트 파일은 두 가지 실호출 확정 사실을 다룬다:

  1. **KITCHEN_UTENSILS releaseDate XOR releaseDateText**: 둘 중 하나만 보내야 한다.
     둘 다 비면 미제공(차단), 하나만 있으면 충족, 둘 다 있으면 위반(네이버 거절).
     releaseDateText 하나만 보냈을 때 HTTP 200 등록 성공이 확인되었다.
  2. **KITCHEN_UTENSILS component (단수)**: 네이버 스펙은 component (단수) 이다.
     과거 데이터가 components (복수) 로 기재되어 값이 통째로 무시되고 NotNull 거절.
  3. **ETC afterServiceDirector / customerServicePhoneNumber 상호배제**:
     기존 코드 special-case 로 다루던 것을 데이터 기반으로 옮겼다. 동작 보존이 핵심.

**핵심 계약 — 확인된 것만 기록한다.** 본 테스트는 확인된 2개 관계만 검증한다.
다른 타입의 필드를 보고 "이것도 XOR 같다" 로 채우지 않는다(과잉 차단 방지).

검증은 ``COMMERCE_DRY_RUN`` 을 끈 상태에서 수행한다(티켓 요구사항).
HTTP mock 으로 네이버 호출 횟수를 센다.
"""

from __future__ import annotations

import copy
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
# 공통 픽스처.
# --------------------------------------------------------------------------- #

# KITCHEN_UTENSILS 카테고리에서 컴플라이언스 게이트가 통과하려면 채워야 하는
# 최소 config notice 섹션 (원산지/AS/공통5필드).
_KITCHEN_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "returnCostReason": "[CFG] 반품 배송비 안내",
    "noRefundReason": "[CFG] 환불 불가 안내",
    "qualityAssuranceStandard": "[CFG] 품질 보증 기준",
    "compensationProcedure": "[CFG] 보상 절차",
    "troubleShootingContents": "[CFG] 고장 대처",
    "delivery_company": "HKSTRANS",
}

# ETC 카테고리 컴플라이언스 통과용 최소 config notice 섹션.
_ETC_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "returnCostReason": "[CFG] 반품 배송비 안내",
    "noRefundReason": "[CFG] 환불 불가 안내",
    "qualityAssuranceStandard": "[CFG] 품질 보증 기준",
    "compensationProcedure": "[CFG] 보상 절차",
    "troubleShootingContents": "[CFG] 고장 대처",
    "delivery_company": "HKSTRANS",
}


def _kitchen_body_with(*, release_date=None, release_date_text=None, **extra) -> dict:
    """KITCHEN_UTENSILS 고시 본문을 만든다.

    releaseDate / releaseDateText 는 키워드 인자로 받는다 — 둘 중 하나만 주는 것이
    XOR 계약이다. 둘 다 주면(또는 둘 다 생략하면) 호출자가 의도한 것이다.
    나머지 필수 필드는 미리 채워둔다.
    """
    body: dict = {
        "itemName": "테스트 주방용품",
        "modelName": "TEST-001",
        "material": "스테인리스 스틸",
        # 네이버 스펙은 component (단수). 복수형(components)은 무시된다.
        "component": "본품 1개",
        "size": "가로 10cm x 세로 20cm",
        "producer": "테스트제조사",
        "warrantyPolicy": "구매일로부터 1년",
        # importDeclaration 은 boolean. False = 수입신고 대상 아님.
        "importDeclaration": False,
    }
    if release_date is not None:
        body["releaseDate"] = release_date
    if release_date_text is not None:
        body["releaseDateText"] = release_date_text
    body.update(extra)
    return body


def _kitchen_notice_from_body(body: dict) -> dict:
    """고시 본문을 KITCHEN_UTENSILS notice 형태로 감싼다."""
    return {
        "productInfoProvidedNoticeType": "KITCHEN_UTENSILS",
        "kitchenUtensils": body,
    }


@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리 (prepared QA 게이트 차단 방지)."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _extract_notice_body(payload, node_key="kitchenUtensils") -> dict:
    """빌드된 페이로드에서 고시 본문을 추출."""
    if not isinstance(payload, dict):
        return {}
    op = payload.get("originProduct")
    if not isinstance(op, dict):
        return {}
    da = op.get("detailAttribute")
    if not isinstance(da, dict):
        return {}
    notice = da.get("productInfoProvidedNotice")
    if not isinstance(notice, dict):
        return {}
    body = notice.get(node_key)
    if not isinstance(body, dict):
        for fallback in ("etc", "furniture", "kitchenUtensils"):
            fb = notice.get(fallback)
            if isinstance(fb, dict):
                body = fb
                break
    return body if isinstance(body, dict) else {}


def _register_kitchen(*, product_notice, monkeypatch, isolated_prepared_dir):
    """KITCHEN_UTENSILS 타입으로 register_product 호출.

    반환: (result_dict, captured)
    captured = {"payload": 전송된 페이로드 deepcopy, "calls": 네이버 호출 수}
    COMMERCE_DRY_RUN 을 끄고, _post_product_payload 를 mock 으로 가로챈다.
    """
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    captured: dict = {"payload": None, "calls": 0}

    def _fake_post(payload, tk, **kwargs):
        captured["payload"] = copy.deepcopy(payload)
        captured["calls"] += 1
        return 200, {"originProductNo": "TEST-KITCHEN-1"}

    patches = [
        mock.patch.object(naver_client, "_notice_config", return_value=_KITCHEN_CFG),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(naver_client, "_post_product_payload", side_effect=_fake_post),
        mock.patch.object(naver_client, "get_token", return_value="fake-token"),
        mock.patch.object(
            mcp_server,
            "_category_path_for",
            return_value="주방용품/조리도구",
        ),
        mock.patch.object(
            naver_client,
            "_category_path_for",
            return_value="주방용품/조리도구",
        ),
    ]
    for p in patches:
        p.start()
    try:
        result = mcp_server.register_product(
            name="테스트 주방용품",
            price=10000,
            image_urls=["http://cdn.example/img.png"],
            category_id="50004528",
            detail_html="<html><body>detail</body></html>",
            notice=product_notice,
            preview_confirmed=True,
        )
    finally:
        for p in patches:
            p.stop()
    return result, captured


def _register_etc(*, product_notice, monkeypatch, isolated_prepared_dir):
    """ETC 타입으로 register_product 호출. ETC 상호배제 회귀 테스트용.

    반환 구조는 _register_kitchen 과 동일.
    """
    monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
    captured: dict = {"payload": None, "calls": 0}

    def _fake_post(payload, tk, **kwargs):
        captured["payload"] = copy.deepcopy(payload)
        captured["calls"] += 1
        return 200, {"originProductNo": "TEST-ETC-1"}

    patches = [
        mock.patch.object(naver_client, "_notice_config", return_value=_ETC_CFG),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(naver_client, "_post_product_payload", side_effect=_fake_post),
        mock.patch.object(naver_client, "get_token", return_value="fake-token"),
        # ETC 추론 — 카테고리 경로가 힌트에 없으면 ETC 기본값.
        mock.patch.object(mcp_server, "_category_path_for", return_value="기타"),
        mock.patch.object(naver_client, "_category_path_for", return_value="기타"),
    ]
    for p in patches:
        p.start()
    try:
        result = mcp_server.register_product(
            name="테스트 기타 상품",
            price=10000,
            image_urls=["http://cdn.example/img.png"],
            category_id="50000001",
            detail_html="<html><body>detail</body></html>",
            notice=product_notice,
            preview_confirmed=True,
        )
    finally:
        for p in patches:
            p.stop()
    return result, captured


# =========================================================================== #
# 데이터 파일 무결성 — 확인된 관계만 기록.
# =========================================================================== #


class TestNoticeFieldRelationsDataIntegrity:
    """notice_field_relations.json 데이터 파일 무결성 검증.

    핵심 계약: 확인된 것만 기록한다. 다른 타입의 필드를 보고 "이것도 XOR 같다"
    로 채우지 않는다 (규제 필드 오신고 방지).
    """

    def test_only_confirmed_relations_in_data(self):
        """데이터에는 확인된 타입의 관계만 있다 (추측 금지).

        확인된 것 (2 sources):
          - 기존: KITCHEN_UTENSILS (releaseDate XOR releaseDateText),
            ETC (afterServiceDirector XOR customerServicePhoneNumber)
          - 2026-08-04 야간 수확 (live API 400 XOR response):
            COSMETIC, HOME_APPLIANCES, FOOD(2쌍), SEASON_APPLIANCES,
            OFFICE_APPLIANCES, CELLPHONE, OPTICS_APPLIANCES, BOOKS, KIDS,
            BIOCHEMISTRY(2쌍), MICROELECTRONICS, NAVIGATION, CAR_ARTICLES,
            MEDICAL_APPLIANCES, GENERAL_FOOD(2쌍), DIET_FOOD,
            MUSICAL_INSTRUMENT, SPORTS_EQUIPMENT, IMAGE_APPLIANCES.
        다른 타입이 우연히 들어가면 안 된다 (추측 금지).
        """
        relations = qa_agents._load_notice_field_relations()
        assert isinstance(relations, dict)
        # 허용된 타입 집합 — 확인된 것만. 기존 2개 + 2026-08-04 수확 19개.
        allowed_types = {
            # 기존 기록분.
            "KITCHEN_UTENSILS",
            "ETC",
            # 2026-08-04 야간 수확 (live API 400 XOR response).
            "COSMETIC",
            "HOME_APPLIANCES",
            "FOOD",
            "SEASON_APPLIANCES",
            "OFFICE_APPLIANCES",
            "CELLPHONE",
            "OPTICS_APPLIANCES",
            "BOOKS",
            "KIDS",
            "BIOCHEMISTRY",
            "MICROELECTRONICS",
            "NAVIGATION",
            "CAR_ARTICLES",
            "MEDICAL_APPLIANCES",
            "GENERAL_FOOD",
            "DIET_FOOD",
            "MUSICAL_INSTRUMENT",
            "SPORTS_EQUIPMENT",
            "IMAGE_APPLIANCES",
        }
        extra = set(relations.keys()) - allowed_types
        assert not extra, f"확인되지 않은 타입의 관계가 데이터에 있습니다 (추측 금지 위반): {extra}"

    def test_kitchen_xor_group_correct(self):
        """KITCHEN_UTENSILS 의 XOR 그룹은 [releaseDate, releaseDateText]."""
        groups = qa_agents._notice_xor_groups("KITCHEN_UTENSILS")
        assert len(groups) >= 1, "KITCHEN_UTENSILS XOR 그룹이 없음"
        # [releaseDate, releaseDateText] 그룹이 존재하는지 확인.
        found = any(set(group) == {"releaseDate", "releaseDateText"} for group in groups)
        assert found, f"releaseDate/releaseDateText XOR 그룹이 없음: {groups}"

    def test_etc_xor_group_correct(self):
        """ETC 의 XOR 그룹은 [afterServiceDirector, customerServicePhoneNumber]."""
        groups = qa_agents._notice_xor_groups("ETC")
        assert len(groups) >= 1, "ETC XOR 그룹이 없음"
        found = any(
            set(group) == {"afterServiceDirector", "customerServicePhoneNumber"} for group in groups
        )
        assert found, f"afterServiceDirector/customerServicePhoneNumber XOR 그룹이 없음: {groups}"

    def test_date_text_pair_is_derived_and_other_types_have_no_groups(self):
        """정본 field_meta 의 WEAR 날짜/직접입력 쌍은 XOR 이고, 나머지는 빈 리스트다."""
        assert qa_agents._notice_xor_groups("WEAR") == [["packDate", "packDateText"]]
        for type_without_pair in ("FURNITURE", "SHOES", "BAG"):
            groups = qa_agents._notice_xor_groups(type_without_pair)
            assert (
                groups == []
            ), f"날짜/직접입력 쌍이 없는 타입 {type_without_pair} 에 XOR 그룹이 있음: {groups}"


# =========================================================================== #
# (a)(b) releaseDateText 만 / releaseDate 만 → 게이트 통과.
# =========================================================================== #


class TestKitchenXorSingleFieldPasses:
    """(a)(b) XOR 그룹에서 하나만 채워지면 게이트 통과."""

    def test_a_release_date_text_only_passes_gate(self, monkeypatch, isolated_prepared_dir):
        """(a) releaseDateText 만 주면 게이트 통과.

        실호출로 releaseDateText 하나만 보냈을 때 HTTP 200 등록 성공이 확인되었다.
        """
        body = _kitchen_body_with(release_date_text="2026년 1월")
        notice = _kitchen_notice_from_body(body)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True, (
            f"releaseDateText 만으로 게이트 통과 실패: "
            f"blocked_by={result.get('blocked_by')}, error={result.get('error')}"
        )
        assert captured["calls"] == 1, "정확히 1회 HTTP 호출이어야 함"

    def test_b_release_date_only_passes_gate(self, monkeypatch, isolated_prepared_dir):
        """(b) releaseDate 만 주어도 통과한다."""
        body = _kitchen_body_with(release_date="2026-01-01")
        notice = _kitchen_notice_from_body(body)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True, (
            f"releaseDate 만으로 게이트 통과 실패: "
            f"blocked_by={result.get('blocked_by')}, error={result.get('error')}"
        )

    def test_unit_missing_with_relations_single_filled(self):
        """단위 테스트 — 하나만 채워지면 누락 0건."""
        fields = ["releaseDate", "releaseDateText"]
        # releaseDateText 만 채운 경우.
        body = {"releaseDateText": "2026-01"}
        missing = qa_agents._notice_field_missing_with_relations(
            body, fields, notice_type="KITCHEN_UTENSILS"
        )
        assert missing == [], f"releaseDateText 만 있는데 누락 보고됨: {missing}"
        # releaseDate 만 채운 경우.
        body = {"releaseDate": "20260101"}
        missing = qa_agents._notice_field_missing_with_relations(
            body, fields, notice_type="KITCHEN_UTENSILS"
        )
        assert missing == [], f"releaseDate 만 있는데 누락 보고됨: {missing}"


# =========================================================================== #
# (c) 둘 다 주면 차단, 사유에 "둘 중 하나만", 네이버 호출 0회.
# =========================================================================== #


class TestKitchenXorBothFieldsBlocked:
    """(c) XOR 그룹에서 둘 다 채워지면 차단 — 네이버가 거절하므로 미리 막는다."""

    def test_c_both_fields_blocked_with_xor_reason(self, monkeypatch, isolated_prepared_dir):
        """(c) releaseDate 와 releaseDateText 둘 다 주면 차단된다.

        사유에 "둘 중 하나만" 이 드러나야 한다. 네이버 호출 0회.
        """
        body = _kitchen_body_with(
            release_date="2026-01-01",
            release_date_text="2026년 1월",
        )
        notice = _kitchen_notice_from_body(body)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        # 차단되어야 한다.
        assert result["ok"] is False, "둘 다 줬는데 통과함 — XOR 위반 미감지"
        # 네이버 호출 0회 — 게이트에서 차단.
        assert (
            captured["calls"] == 0
        ), f"네이버 호출이 0회여야 함 (게이트 차단 전): {captured['calls']}"
        # 차단 사유에 "둘 중 하나만" 이 드러나야 한다.
        violations = result.get("violations") or []
        xor_violations = [v for v in violations if "상호배제" in str(v.get("rule") or "")]
        assert xor_violations, f"고시 필드 상호배제 위반이 없음: violations={violations}"
        # 상세 메시지에 "둘 중 하나만" 표현이 있어야 한다.
        detail_text = " ".join(str(v.get("detail") or "") for v in xor_violations)
        assert "둘 중 하나만" in detail_text, f"차단 사유에 '둘 중 하나만' 이 없음: {detail_text!r}"

    def test_c_unit_xor_violations_both_filled(self):
        """단위 테스트 — _notice_field_xor_violations 가 둘 다 채워진 경우를 잡는다."""
        body = {"releaseDate": "20260101", "releaseDateText": "2026-01"}
        violations = qa_agents._notice_field_xor_violations(body, "KITCHEN_UTENSILS")
        assert len(violations) == 1, f"위반이 1건이어야 함: {len(violations)}"
        detail = str(violations[0].get("detail") or "")
        assert "둘 중 하나만" in detail, f"사유에 '둘 중 하나만' 없음: {detail!r}"

    def test_c_needs_user_surfaces_xor(self, monkeypatch, isolated_prepared_dir):
        """(c) needs_user 에 XOR 위반이 올라간다 (사용자 안내).

        needs_user 의 항목에 "둘 중 하나만" / 상호배제 안내가 있어야 한다.
        """
        body = _kitchen_body_with(
            release_date="2026-01-01",
            release_date_text="2026년 1월",
        )
        notice = _kitchen_notice_from_body(body)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is False
        needs_user = result.get("needs_user") or []
        # needs_user 중 하나라도 why 또는 answer_shape 에 상호배제 안내가 있어야.
        hints = []
        for n in needs_user:
            hints.append(str(n.get("why") or ""))
            hints.append(str(n.get("answer_shape") or ""))
        combined = " ".join(hints)
        assert (
            "둘 중 하나만" in combined or "상호배제" in combined
        ), f"needs_user 에 XOR 안내가 없음: needs_user={needs_user}"


# =========================================================================== #
# (d) 둘 다 없으면 기존대로 차단 (미제공).
# =========================================================================== #


class TestKitchenXorNeitherFieldBlocked:
    """(d) XOR 그룹이 둘 다 비면 미제공으로 차단 (기존 동작)."""

    def test_d_neither_field_blocked_as_missing(self, monkeypatch, isolated_prepared_dir):
        """(d) releaseDate 와 releaseDateText 둘 다 없으면 누락으로 차단."""
        body = _kitchen_body_with()  # 둘 다 안 넣음.
        notice = _kitchen_notice_from_body(body)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is False, "둘 다 없는데 통과함 — 미제공 미감지"
        # 네이버 호출 0회.
        assert captured["calls"] == 0

    def test_d_unit_missing_with_relations_neither(self):
        """단위 테스트 — 둘 다 비면 첫 멤버만 누락으로 보고 (중복 방지)."""
        fields = ["releaseDate", "releaseDateText"]
        body = {}
        missing = qa_agents._notice_field_missing_with_relations(
            body, fields, notice_type="KITCHEN_UTENSILS"
        )
        # 둘 다 비었을 때 — XOR 그룹 전체가 충족되지 않았으므로 누락 보고.
        # 단, 그룹의 첫 멤버만 보고한다 (중복 방지).
        assert (
            len(missing) == 1
        ), f"XOR 그룹 둘 다 비었을 때 누락이 1건이어야 함 (중복 방지): {missing}"


# =========================================================================== #
# (e) 전송 페이로드에 component (단수) 로 실린다 (components 아님).
# =========================================================================== #


class TestKitchenComponentSingularSpelling:
    """(e) 필드명 정정 — component (단수) 가 페이로드에 실린다."""

    def test_e_component_singular_in_payload(self, monkeypatch, isolated_prepared_dir):
        """(e) 전송 페이로드의 키는 component (단수) 이다.

        네이버 스펙이 component (단수) 이므로, 우리가 component 로 보내야
        NotNull 검증을 통과한다. 복수형(components)은 네이버가 무시한다.
        """
        body = _kitchen_body_with(release_date_text="2026년 1월")
        notice = _kitchen_notice_from_body(body)
        result, captured = _register_kitchen(
            product_notice=notice,
            monkeypatch=monkeypatch,
            isolated_prepared_dir=isolated_prepared_dir,
        )
        assert result["ok"] is True, f"등록 실패: {result.get('error')}"
        body_sent = _extract_notice_body(captured["payload"])
        # component (단수) 키가 있어야 한다.
        assert (
            "component" in body_sent
        ), f"페이로드에 component (단수) 키가 없음: keys={list(body_sent.keys())}"
        assert (
            body_sent.get("component") == "본품 1개"
        ), f"component 값이 다름: {body_sent.get('component')!r}"

    def test_e_components_plural_not_required_by_data(self):
        """단위 테스트 — data/notice_types.json 의 KITCHEN_UTENSILS 필수 필드에
        component (단수) 가 있고 components (복수) 는 없다."""
        spec = naver_client._notice_type_spec("KITCHEN_UTENSILS")
        assert spec is not None, "KITCHEN_UTENSILS 스펙이 없음"
        fields = spec.get("fields") or []
        assert "component" in fields, f"component (단수) 가 필수 필드에 없음: {fields}"
        assert (
            "components" not in fields
        ), f"components (복수) 가 필수 필드에 남아있음 (철자 정정 누락): {fields}"


# =========================================================================== #
# (f) ETC 상호배제가 기존과 동일하게 동작한다 (회귀 방지).
# =========================================================================== #


class TestEtcMutualExclusionRegression:
    """(f) ETC 의 afterServiceDirector / customerServicePhoneNumber 상호배제.

    기존에는 코드 special-case(_enforce_notice_as_contact_exclusive) 로 다뤘다.
    이제 데이터 기반(notice_field_relations.json)으로 옮겼다. 동작은 바뀌지 않는다.

    **회귀 방지 핵심**: config 가 afterServiceDirector 를 채우고 사용자가
    customerServicePhoneNumber 만 명시하면, afterServiceDirector 가 제거되어야
    한다 (단일 노출 정책). 이 동작이 데이터 기반 경로에서도 보존되는지 확인.
    """

    def test_f_user_customer_only_drops_after_service_director(self):
        """단위 테스트 — 사용자가 customerServicePhoneNumber 만 명시한 경우.

        afterServiceDirector 가 본문에 있더라도(예: config 가 채운 경우),
        사용자가 customerServicePhoneNumber 만 명시적으로 제공했으면
        afterServiceDirector 를 제거한다 (회귀 방지).
        """
        body = {
            "afterServiceDirector": "테스트제조사 070-1234-5678",  # config 가 채운 값.
            "customerServicePhoneNumber": "070-9999-9999",  # 사용자 명시.
        }
        user_fields = {"customerServicePhoneNumber"}
        # 본문을 직접 변경하므로 카피로 검증.
        body_copy = copy.deepcopy(body)
        naver_client._enforce_notice_as_contact_exclusive(body_copy, user_fields)
        assert (
            "afterServiceDirector" not in body_copy
        ), f"사용자가 customer 만 명시했는데 afterServiceDirector 가 남아있음: {body_copy}"
        assert "customerServicePhoneNumber" in body_copy

    def test_f_user_after_only_drops_customer_service(self):
        """단위 테스트 — 사용자가 afterServiceDirector 만 명시한 경우.

        customerServicePhoneNumber 가 본문에 있더라도 제거된다.
        """
        body = {
            "afterServiceDirector": "테스트제조사 070-1234-5678",  # 사용자 명시.
            "customerServicePhoneNumber": "070-9999-9999",  # config 가 채운 값.
        }
        user_fields = {"afterServiceDirector"}
        body_copy = copy.deepcopy(body)
        naver_client._enforce_notice_as_contact_exclusive(body_copy, user_fields)
        assert (
            "customerServicePhoneNumber" not in body_copy
        ), f"사용자가 after 만 명시했는데 customerServicePhoneNumber 가 남아있음: {body_copy}"
        assert "afterServiceDirector" in body_copy

    def test_f_both_user_provided_not_silently_dropped(self):
        """단위 테스트 — 둘 다 사용자가 명시한 경우.

        **조용한 선택 금지**: 어느 하나를 조용히 버리지 않는다. 게이트의
        "고시 필드 상호배제" 위반이 이 케이스를 잡는다.
        """
        body = {
            "afterServiceDirector": "테스트제조사 070-1234-5678",
            "customerServicePhoneNumber": "070-9999-9999",
        }
        user_fields = {"afterServiceDirector", "customerServicePhoneNumber"}
        body_copy = copy.deepcopy(body)
        naver_client._enforce_notice_as_contact_exclusive(body_copy, user_fields)
        # 둘 다 남아 있어야 한다 — 조용히 하나를 버리지 않는다.
        assert (
            "afterServiceDirector" in body_copy
        ), "사용자가 둘 다 명시했는데 afterServiceDirector 가 조용히 제거됨"
        assert (
            "customerServicePhoneNumber" in body_copy
        ), "사용자가 둘 다 명시했는데 customerServicePhoneNumber 가 조용히 제거됨"

    def test_f_config_filled_both_preserves_old_behavior(self):
        """단위 테스트 — 둘 다 config 가 채운 경우(사용자 명시 아님).

        **회귀 방지**: 기존 동작은 afterServiceDirector 를 우선하여
        customerServicePhoneNumber 를 제거하는 것이었다. 이 동작이 보존되어야 한다.
        """
        body = {
            "afterServiceDirector": "테스트제조사 070-1234-5678",
            "customerServicePhoneNumber": "070-9999-9999",
        }
        user_fields = set()  # 둘 다 사용자 명시 아님.
        body_copy = copy.deepcopy(body)
        naver_client._enforce_notice_as_contact_exclusive(body_copy, user_fields)
        # 기존 동작 보존: afterServiceDirector 를 우선하여 customerServicePhoneNumber 제거.
        assert (
            "customerServicePhoneNumber" not in body_copy
        ), f"config 가 둘 다 채운 경우 customerServicePhoneNumber 가 제거되어야 함 (회귀): {body_copy}"
        assert "afterServiceDirector" in body_copy

    def test_f_etc_xor_violations_detected(self):
        """단위 테스트 — ETC 에서 둘 다 채워지면 XOR 위반으로 잡힌다."""
        body = {
            "afterServiceDirector": "테스트제조사 070-1234-5678",
            "customerServicePhoneNumber": "070-9999-9999",
        }
        violations = qa_agents._notice_field_xor_violations(body, "ETC")
        assert len(violations) == 1, f"ETC XOR 위반이 1건이어야 함: {len(violations)}"
        detail = str(violations[0].get("detail") or "")
        assert "둘 중 하나만" in detail

    def test_f_etc_single_filled_no_violation(self):
        """단위 테스트 — ETC 에서 하나만 채워지면 XOR 위반 없음."""
        # afterServiceDirector 만.
        body = {"afterServiceDirector": "테스트제조사 070-1234-5678"}
        violations = qa_agents._notice_field_xor_violations(body, "ETC")
        assert violations == [], f"하나만 있는데 XOR 위반 보고됨: {violations}"
        # customerServicePhoneNumber 만.
        body = {"customerServicePhoneNumber": "070-1234-5678"}
        violations = qa_agents._notice_field_xor_violations(body, "ETC")
        assert violations == []


# =========================================================================== #
# (g) 관계가 기록되지 않은 타입은 기존 동작 그대로.
# =========================================================================== #


class TestUnconfirmedTypeBehaviorUnchanged:
    """(g) 관계가 기록되지 않은 타입은 기존 동작을 유지한다.

    확인되지 않은 타입(WEAR 등)은 XOR 그룹이 없으므로, _notice_field_missing
    과 _notice_field_missing_with_relations 가 동일하게 동작해야 한다.
    """

    def test_g_date_text_pair_derived_and_other_types_empty(self):
        """WEAR의 정본 날짜/직접입력 XOR 은 유지하고, 관계 없는 타입은 빈 리스트다."""
        assert qa_agents._notice_xor_groups("WEAR") == [["packDate", "packDateText"]]
        for t in ("FURNITURE", "SHOES", "BAG", "SLEEPING_GEAR"):
            assert qa_agents._notice_xor_groups(t) == []

    def test_g_missing_same_with_or_without_relations(self):
        """관계 미기재 타입은 _notice_field_missing 과 _with_relations 가 동일."""
        # WEAR 의 필수 필드 일부.
        fields = ["material", "color", "size", "manufacturer"]
        body = {"material": "면 100%", "color": "블랙"}  # size, manufacturer 누락.
        without = qa_agents._notice_field_missing(body, fields)
        with_rel = qa_agents._notice_field_missing_with_relations(body, fields, notice_type="WEAR")
        assert without == with_rel, (
            f"WEAR(미기재 타입) 가 relations 유무와 다르게 동작함: "
            f"without={without}, with_rel={with_rel}"
        )

    def test_g_no_xor_violations_for_unconfirmed(self):
        """확인되지 않은 타입은 XOR 위반이 0건."""
        # 의도적으로 같은 이름의 필드를 채워도 — 관계가 없으면 위반 아님.
        body = {"releaseDate": "x", "releaseDateText": "y"}
        for t in ("WEAR", "FURNITURE"):
            violations = qa_agents._notice_field_xor_violations(body, t)
            assert (
                violations == []
            ), f"미기재 타입 {t} 에 XOR 위반이 보고됨 (추측 금지 위반): {violations}"
