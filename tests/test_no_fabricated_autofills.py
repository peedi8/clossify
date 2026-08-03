"""규제 신고값 임의 삽입 잔존 제거 (직접 반례 실행).

본 파일이 검증하는 항목을 순서대로 나열한다:

  1. **임의 삽입 0**: 최소 입력(상품명·가격·이미지·카테고리)만으로
     ``build_payload`` 호출 시, 고시 관련 필드에 **코드가 만든 문자열이
     들어가지 않음**(비어 있거나 생략).
  2. **누락 지적**: 같은 최소 입력에서 컴플라이언스가 **FAIL** 로 부족
     항목을 열거.
  3. **placeholder 구분**: 사용자가 ``상세참조`` 를 입력 → payload 에는
     **그 값 그대로 전송**되지만, 컴플라이언스 판정은 **미제공으로 간주해
     지적**.
  4. **KC 전부/전무**: 두 키 다 설정 → 블록 존재 / 하나만 설정 →
     **블록 전체 생략** + 경고 메타.
  5. **AS 미설정** → 컴플라이언스 FAIL(등록 차단), 기본 문자열 생성 0.

이 테스트 파일은 요구되는 "반례" 를 코드로 고정한다 —
회귀 방지 목적이다. 한글 리터럴만 사용, 외부 API 호출 없음.
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

from clossify import common, mcp_server, naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 공통 픽스처.
# --------------------------------------------------------------------------- #

# _notice_config 가 반환할 "판매자가 실제 신고하는 값" 없는 빈 config.
# 본 검증의 핵심 전제: 코드가 값을 지어내지 않는다 → config 가 비면 payload 도 비고
# 컴플라이언스가 FAIL 지적한다.
_EMPTY_NOTICE_CFG: dict = {
    # origin 만 있고 나머지는 전부 비어 있다. origin_area_code/origin_content 는
    # _resolve_origin_area_code 가 ValueError 를 던지는 것을 막기 위한 최소값이다
    # (원산지 누락 자체는 별도 FAIL 항목이지만, build_payload 자체가 예외로 죽으면
    #  반례 1/2 를 검증할 수 없다).
    "origin_area_code": "04",
    "origin_content": "중국",
}

# common.cfg() mock 용 — 컴플라이언스 원산지 일치 검사가 읽는 최소 config.
_COMMON_CFG_EMPTY: dict = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}

# 의류 카테고리 — E2E 검증용.
_CLOTHING_CATEGORY = "50021299"

# 코드가 만들어 넣던 임의 문구 토큰들. payload 어디에서도 발견되면 안 된다.
_FORBIDDEN_AUTOFILLS = (
    "상세페이지 참조",
    "상세참조",
    "판매자연락처",
    "해당없음 / 상세참조",
    "해당없음 / KC면제",
    "해외구매대행",
)


def _build_payload_minimal():
    """최소 입력(상품명·가격·카테고리·이미지)으로 build_payload 를 호출.

    config(_notice_config)는 origin 만 있고 나머지는 비어 있다.
    반환: payload dict.
    """
    product = {
        "name": "테스트상품",
        "categoryId": _CLOTHING_CATEGORY,
        "salePrice": 30000,
    }
    with mock.patch.object(naver_client, "_notice_config", return_value=_EMPTY_NOTICE_CFG):
        with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
            return naver_client.build_payload(
                product, "<html><body>상세</body></html>", ["http://cdn/x.png"]
            )


def _collect_payload_strings(payload) -> list[str]:
    """payload 의 모든 문자열 값을 평탄하게 모은다 (전수 검사용)."""
    found: list[str] = []

    def visit(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for sub in value.values():
                visit(sub)
        elif isinstance(value, list):
            for sub in value:
                visit(sub)

    visit(payload)
    return found


# --------------------------------------------------------------------------- #
# 반례 1 — 임의 삽입 0.
# --------------------------------------------------------------------------- #
class TestNoFabricatedAutofills:
    """최소 입력으로 build_payload 호출 시 코드가 만든 문자열이 없어야 한다."""

    def test_no_forbidden_autofill_tokens_in_payload(self):
        """금지된 임의 문구 토큰이 payload 어디에서도 발견되지 않는다."""
        payload = _build_payload_minimal()
        all_strings = _collect_payload_strings(payload)
        for token in _FORBIDDEN_AUTOFILLS:
            hits = [s for s in all_strings if token in s]
            assert not hits, f"코드가 만든 임의 문구({token!r}) 가 payload 에 발견됨: {hits[:3]}"

    def test_notice_manufacturer_empty_or_absent(self):
        """고시 본문에 manufacturer 가 코드 임의값으로 채워지지 않는다."""
        payload = _build_payload_minimal()
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        # node 키는 notice 타입에 따라 etc/wear/... 중 하나.
        for node_key, body in notice.items():
            if not isinstance(body, dict):
                continue
            if "manufacturer" in body:
                # 있으면 빈 문자열이 아니어야 하고, 코드 임의문구가 아니어야 한다.
                val = str(body["manufacturer"]).strip()
                assert val, f"{node_key}.manufacturer 가 빈 문자열로 들어감(생략해야 함)"
                for token in _FORBIDDEN_AUTOFILLS:
                    assert (
                        token not in val
                    ), f"{node_key}.manufacturer 가 임의문구({token!r}) 를 포함"

    def test_as_telephone_empty_when_config_empty(self):
        """config 에 as_tel 이 없으면 afterServiceTelephoneNumber 는 빈 문자열이다."""
        payload = _build_payload_minimal()
        as_info = (
            payload.get("originProduct", {}).get("detailAttribute", {}).get("afterServiceInfo", {})
        )
        tel = as_info.get("afterServiceTelephoneNumber")
        assert tel == "", f"afterServiceTelephoneNumber 가 코드 임의값으로 채워짐: {tel!r}"

    def test_common_five_fields_absent_when_config_empty(self):
        """공통 5 고시 필드가 config 비어 있을 때 payload 에 임의문구로 들어가지 않는다."""
        payload = _build_payload_minimal()
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        common_fields = (
            "returnCostReason",
            "noRefundReason",
            "qualityAssuranceStandard",
            "compensationProcedure",
            "troubleShootingContents",
        )
        for node_key, body in notice.items():
            if not isinstance(body, dict):
                continue
            for field in common_fields:
                if field in body:
                    val = str(body[field]).strip()
                    # 있으면 임의문구가 아니어야 한다(사용자가 준 실제값).
                    for token in _FORBIDDEN_AUTOFILLS:
                        assert (
                            token not in val
                        ), f"{node_key}.{field} 가 임의문구({token!r}) 를 포함"

    def test_kc_block_absent_when_config_incomplete(self):
        """KC 설정이 불완전하면 certificationTargetExcludeContent 가 payload 에 없다."""
        payload = _build_payload_minimal()
        detail_attr = payload.get("originProduct", {}).get("detailAttribute", {})
        assert (
            "certificationTargetExcludeContent" not in detail_attr
        ), "KC 블록이 config 불완전인데도 payload 에 존재함 (부분 블록 금지 위반)"


# --------------------------------------------------------------------------- #
# 반례 2 — 누락 지적 (컴플라이언스 FAIL).
# --------------------------------------------------------------------------- #
class TestComplianceFailOnMinimumInput:
    """최소 입력에서 컴플라이언스가 FAIL 로 부족 항목을 열거하는가."""

    def test_compliance_returns_fail_verdict(self):
        """최소 입력 → 컴플라이언스 verdict 가 FAIL."""
        payload = _build_payload_minimal()
        with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_EMPTY):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=payload,
            )
        assert (
            result["verdict"] == qa_agents.FAIL
        ), f"최소 입력인데 컴플라이언스가 FAIL 이 아님: {result['verdict']}"

    def test_compliance_enumerates_missing_items(self):
        """최소 입력 → 위반 항목에 고시 필수필드 누락과 AS 연락처 누락이 포함된다."""
        payload = _build_payload_minimal()
        with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_EMPTY):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=payload,
            )
        rules = [str(v.get("rule") or "") for v in result["violations"]]
        # 고시 필수필드 누락 지적이 있어야 한다.
        assert any(
            "고시" in r and "필드" in r for r in rules
        ), f"고시 필수필드 누락 지적이 없음: {rules}"
        # AS 연락처 누락 지적이 있어야 한다 (요구 항목 4).
        assert any("A/S" in r or "AS" in r for r in rules), f"AS 연락처 누락 지적이 없음: {rules}"
        # 모든 위반의 severity 가 FAIL 인 항목이 하나 이상 있어야 한다.
        fail_severities = [
            v
            for v in result["violations"]
            if str(v.get("severity") or "").upper() == qa_agents.FAIL
        ]
        assert fail_severities, "FAIL 심각도 위반이 없음"

    def test_e2e_register_blocked_by_compliance(self):
        """register_product E2E: 최소 입력 → blocked_by == 'compliance'."""
        with mock.patch.object(naver_client, "_notice_config", return_value=_EMPTY_NOTICE_CFG):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_EMPTY):
                    with mock.patch.object(naver_client, "register_product") as naver_mock:
                        result = mcp_server.register_product(
                            name="테스트상품",
                            price=30000,
                            image_urls=["http://cdn/x.png"],
                            category_id=_CLOTHING_CATEGORY,
                            detail_html="<html><body>상세</body></html>",
                        )
        assert result["ok"] is False, "최소 입력인데 등록이 허용됨"
        assert (
            result.get("blocked_by") == "compliance"
        ), f"blocked_by 가 compliance 가 아님: {result.get('blocked_by')}"
        # 네이버 API 가 호출되지 않아야 한다.
        assert naver_mock.call_count == 0, "컴플라이언스 차단인데 네이버 API 가 호출됨"


# --------------------------------------------------------------------------- #
# 반례 3 — placeholder 구분 (전송 O, 판정 X).
# --------------------------------------------------------------------------- #
class TestPlaceholderDualPolicy:
    """사용자가 placeholder 값을 주면: payload 전송 O, 컴플라이언스 판정 X.

    정책 요구: "전송은 하되 '필수 항목이 채워졌다' 고 판정하지는 않는다."
    """

    def test_placeholder_value_transmitted_verbatim(self):
        """사용자가 준 placeholder 값이 payload 에 그대로 실린다 (전송 O)."""
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "상세참조",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁",
                "packDateText": "상세페이지 참조",
                "warrantyPolicy": "구매 후 7일 교환",
            },
        }
        product = {
            "name": "테스트니트",
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": 30000,
            "notice": notice_override,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_EMPTY_NOTICE_CFG):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        wear = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
            .get("wear", {})
        )
        assert (
            wear.get("material") == "상세참조"
        ), f"material placeholder 가 전송되지 않음: {wear.get('material')!r}"
        assert (
            wear.get("packDateText") == "상세페이지 참조"
        ), f"packDateText placeholder 가 전송되지 않음: {wear.get('packDateText')!r}"

    def test_placeholder_value_judged_missing(self):
        """placeholder 값은 컴플라이언스에서 '미제공' 으로 간주해 FAIL 지적한다."""
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "material": "상세참조",
                "color": "블랙",
                "size": "FREE",
                "caution": "물 세탁",
                "packDateText": "상세페이지 참조",
                "warrantyPolicy": "구매 후 7일 교환",
            },
        }
        product = {
            "name": "테스트니트",
            "categoryId": _CLOTHING_CATEGORY,
            "salePrice": 30000,
            "notice": notice_override,
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=_EMPTY_NOTICE_CFG):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_EMPTY):
            result = qa_agents._compliance_code_check(
                "테스트니트",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=payload,
            )
        assert (
            result["verdict"] == qa_agents.FAIL
        ), f"placeholder 입력인데 컴플라이언스가 FAIL 이 아님: {result['verdict']}"
        # 위반 상세에 "상세참조" 등 placeholder 필드 누락이 열거되어야 한다.
        all_detail = " ".join(str(v.get("detail") or "") for v in result["violations"])
        assert (
            "material" in all_detail or "누락" in all_detail
        ), f"placeholder 필드 누락이 지적되지 않음: {all_detail}"

    def test_placeholder_recognized_as_missing_unit(self):
        """_notice_field_missing 단위 테스트: placeholder 토큰들이 missing 으로 판정된다."""
        tokens = (
            "상세참조",
            "상세 참조",
            "상세페이지참조",
            "상세페이지 참조",
            "해당없음",
            "-",
            "",
            "   ",
            "null",
            "n/a",
            "none",
            "별도표시",
            "본품참조",
        )
        for token in tokens:
            body = {"field": token}
            missing = qa_agents._notice_field_missing(body, ["field"])
            assert "field" in missing, f"토큰 {token!r} 이 missing 으로 판정되지 않음 (정책 위반)"

    def test_real_value_not_treated_as_missing(self):
        """대조군: 실제 정보값은 missing 으로 판정되지 않는다."""
        body = {"field": "면 100%", "other": "2026-01"}
        missing = qa_agents._notice_field_missing(body, ["field", "other"])
        assert missing == [], f"실제값이 missing 으로 잘못 판정됨: {missing}"


# --------------------------------------------------------------------------- #
# 반례 4 — KC 전부/전무.
# --------------------------------------------------------------------------- #
class TestKcFullOrOmit:
    """KC 선언 블록: 두 키 모두 → 존재 / 하나만 → 전체 생략 + 경고."""

    def test_both_keys_present_block_emitted(self):
        """kcCertifiedProductExclusionYn + kcExemptionType 모두 있으면 블록이 싣는다."""
        kc_cfg = {
            "kc_declaration": {
                "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
                "kcExemptionType": "OVERSEAS",
            }
        }
        with mock.patch.object(naver_client, "load_config", return_value=kc_cfg):
            block, warning = naver_client._kc_config()
        assert block.get("kcCertifiedProductExclusionYn") == "KC_EXEMPTION_OBJECT"
        assert block.get("kcExemptionType") == "OVERSEAS"
        assert warning == "", f"두 키가 있는데 경고가 남: {warning!r}"

    def test_one_key_missing_block_omitted_with_warning(self):
        """kcCertifiedProductExclusionYn 만 있고 kcExemptionType 없으면 블록 전체 생략."""
        kc_cfg = {
            "kc_declaration": {
                "kcCertifiedProductExclusionYn": "KC_EXEMPTION_OBJECT",
                # kcExemptionType 누락.
            }
        }
        with mock.patch.object(naver_client, "load_config", return_value=kc_cfg):
            block, warning = naver_client._kc_config()
        assert block == {}, f"한 키만 있는데 KC 블록이 생성됨 (부분 블록 금지 위반): {block}"
        assert warning, "블록 생략 시 경고 메타가 비어 있음 (조용한 생략 금지 위반)"
        # 경고 메타에 부분 블록 금지 관련 안내가 포함되어야 한다.
        assert (
            "완전하지 않습니다" in warning or "생략" in warning
        ), f"경고 메타가 부분 블록 금지를 설명하지 않음: {warning!r}"

    def test_other_key_missing_block_omitted_with_warning(self):
        """kcExemptionType 만 있고 kcCertifiedProductExclusionYn 없어도 블록 전체 생략."""
        kc_cfg = {
            "kc_declaration": {
                # kcCertifiedProductExclusionYn 누락.
                "kcExemptionType": "OVERSEAS",
            }
        }
        with mock.patch.object(naver_client, "load_config", return_value=kc_cfg):
            block, warning = naver_client._kc_config()
        assert block == {}, f"한 키만 있는데 KC 블록이 생성됨 (역순 케이스): {block}"
        assert warning, "블록 생략 시 경고 메타가 비어 있음 (역순 케이스)"

    def test_no_kc_section_block_omitted_with_warning(self):
        """kc_declaration 섹션 자체가 없어도 블록 생략 + 경고."""
        with mock.patch.object(naver_client, "load_config", return_value={}):
            block, warning = naver_client._kc_config()
        assert block == {}
        assert warning, "KC 섹션 부재 시 경고 메타가 비어 있음"

    def test_payload_kc_warning_meta_present_when_omitted(self):
        """KC 생략 시 payload 의 _kcWarning 메타에 경고가 남는다 (조용한 생략 금지)."""
        # kc_config 가 ({}, "경고문구") 를 반환하도록 mock.
        warning_text = "KC 설정이 완전하지 않아 블록을 생략합니다."
        with mock.patch.object(naver_client, "_notice_config", return_value=_EMPTY_NOTICE_CFG):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, warning_text)):
                payload = naver_client.build_payload(
                    {"name": "x", "categoryId": _CLOTHING_CATEGORY, "salePrice": 1000},
                    "<html></html>",
                    ["http://x.png"],
                )
        assert (
            payload.get("_kcWarning") == warning_text
        ), f"_kcWarning 메타가 누락됨: {payload.get('_kcWarning')!r}"
        detail_attr = payload.get("originProduct", {}).get("detailAttribute", {})
        assert "certificationTargetExcludeContent" not in detail_attr


# --------------------------------------------------------------------------- #
# 반례 5 — AS 미설정 → 컴플라이언스 FAIL, 기본 문자열 생성 0.
# --------------------------------------------------------------------------- #
class TestAsContactMissingFails:
    """AS 연락처 미설정 시: 코드가 임의 문자열을 만들지 않고, 컴플라이언스 FAIL 차단."""

    def test_no_fabricated_as_string_in_payload(self):
        """config 에 as_tel 이 없으면 afterServiceTelephoneNumber 는 빈 문자열이다."""
        payload = _build_payload_minimal()
        tel = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("afterServiceInfo", {})
            .get("afterServiceTelephoneNumber")
        )
        assert tel == "", f"AS 연락처가 임의값으로 채워짐: {tel!r}"
        # 특히 금지된 임의문구가 아니어야 한다.
        for token in _FORBIDDEN_AUTOFILLS:
            assert tel != token, f"AS 연락처가 {token!r} 임의문구로 채워짐"

    def test_as_missing_compliance_fail(self):
        """AS 연락처 누락 → 컴플라이언스 FAIL (WARN 이 아님)."""
        payload = _build_payload_minimal()
        with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_EMPTY):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=payload,
            )
        as_violations = [
            v
            for v in result["violations"]
            if "A/S" in str(v.get("rule") or "") or "AS" in str(v.get("rule") or "")
        ]
        assert as_violations, "AS 연락처 누락 위반이 없음"
        for v in as_violations:
            assert (
                str(v.get("severity") or "").upper() == qa_agents.FAIL
            ), f"AS 연락처 누락이 FAIL 이 아님 (WARN 잔존): {v}"

    def test_as_missing_severity_is_fail_not_warn(self):
        """AS 연락처 누락 위반의 severity 가 정확히 'FAIL' 이다 (요구 항목 4)."""
        payload = _build_payload_minimal()
        with mock.patch.object(common, "cfg", return_value=_COMMON_CFG_EMPTY):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": _CLOTHING_CATEGORY},
                api_payload=payload,
            )
        # AS 관련 위반을 찾아 severity 확인.
        found_as_fail = False
        for v in result["violations"]:
            detail = str(v.get("detail") or "") + str(v.get("rule") or "")
            if "afterServiceTelephoneNumber" in detail or "A/S" in detail or "AS" in detail:
                if str(v.get("severity") or "").upper() == qa_agents.FAIL:
                    found_as_fail = True
                    break
        assert found_as_fail, "AS 연락처 누락이 FAIL severity 로 보고되지 않음 (WARN 잔존 가능성)"
