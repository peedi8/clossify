"""ETC 고시 인증 필드(certificateDetails) 조립기·게이트 정합성 테스트.

결함 (wo-n89, 최초 이식 커밋부터): ``_base_etc_notice`` 가 인증 값을
``certDetail``/``certificationDetails`` 로 실어 정본 필드명
``certificateDetails`` (data/notice_types.json 의 ETC·ETC_SERVICE) 과 어긋났다.
판매자가 인증 정보를 줘도 컴플라이언스 게이트가 ``certificateDetails`` 누락으로
오판했다.

본 테스트가 기존 시험(test_registration_record 등) 과 다른 핵심: notice 본문을
**손으로 만들지 않고** ``_notice_defaults`` → ``_product_info_notice``
(``build_payload`` 가 내부적으로 거치는 같은 조립 체인, naver_client.py 의
``build_payload`` → ``_product_info_notice`` → ``_base_notice_body_for_type`` →
``_base_etc_notice``) 산출을 **그대로** 게이트에 넣는다. 기존 시험이 손수 만든
본문에 정본 이름을 직접 써서 조립기를 한 번도 안 거쳤기 때문에 오타가 살아남은
것의 재발 방지다.

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로 네트워크 차단.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar
from unittest import mock

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 테스트용 공통 픽스처.
# --------------------------------------------------------------------------- #

# naver_client._notice_config mock — 공통 5필드·AS·제조사 제공, cert_detail 은
# 상품 입력에서 주는 것으로 두고 여기선 비운다.
_NOTICE_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
    "cert_detail": "",
}

# common.cfg() mock — _compliance_code_check 가 원산지 일치 검사를 위해 직접
# 읽는 값. _notice_config mock 의 origin_content 와 일치시킨다.
_COMMON_CFG = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
        "as_tel": "070-1234-5678",
    },
}

_CERT_VALUE = "전파적합성 KCC-REI-XXX"


def _make_etc_product(cert_detail: str = "") -> dict:
    """ETC 상품 입력 dict. cert_detail 이 실질값일 때만 키를 둔다."""
    p = {
        "name": "테스트 ETC 상품",
        "categoryId": "50000000",
        "salePrice": 30000,
        "origin_code": "04",
        "made_in": "중국",
        "model_name": "TEST-MODEL-1",
        # 품명(itemName) 은 상품명에서 자동으로 뽑지 않는다(사용자 결정
        # 2026-08-26) — ETC 필수필드를 명시로 준다.
        "item_name": "테스트 품목",
        "notice": {"productInfoProvidedNoticeType": "ETC"},
    }
    if cert_detail:
        p["cert_detail"] = cert_detail
    return p


def _assemble_etc_notice(cert_detail: str = "") -> dict:
    """조립기 체인(build_payload 가 쓰는 것과 동일) 으로 ETC notice 를 만든다."""
    p = _make_etc_product(cert_detail)
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        defaults = naver_client._notice_defaults(p)
        return naver_client._product_info_notice(p, defaults)


def _run_gate(notice: dict) -> dict:
    """조립기 산출 notice 를 그대로 컴플라이언스 게이트에 넣는다."""
    ctx = {
        "notice": notice,
        "origin_content": "중국",
        "as_tel": "070-1234-5678",
    }
    with mock.patch.object(qa_agents.common, "cfg", return_value=_COMMON_CFG):
        return qa_agents._compliance_code_check("테스트 ETC 상품", ctx)


def _notice_rule_details(result: dict) -> list[str]:
    """게이트 결과에서 '고시 필수필드' 위반 detail 목록만 뽑는다."""
    return [
        str(v.get("detail") or "")
        for v in result.get("violations") or []
        if v.get("rule") == "고시 필수필드"
    ]


# --------------------------------------------------------------------------- #
# 1. _base_etc_notice 산출 키 — 정본 이름만 실린다.
# --------------------------------------------------------------------------- #
class TestBaseEtcNoticeCertField:
    """``_base_etc_notice`` 가 인증 값을 정본 필드명으로 싣는가."""

    _DEFAULTS: ClassVar[dict] = {
        "item_name": "테스트 ETC 상품",
        "model_name": "TEST-MODEL-1",
        "cert_detail": _CERT_VALUE,
        "made_in": "중국",
        "manufacturer": "테스트제조사",
        "manufacturer_importer": "",
        "manufacture_date": "",
        "quality_assurance_standard": "관련법에 따름",
        "return_cost_reason": "단순변심 반품비용 구매자부담",
        "no_refund_reason": "주문제작 청약철회 제한",
        "compensation_procedure": "소비자분쟁해결기준",
        "trouble_shooting_contents": "고객센터 문의",
        "as_tel": "070-1234-5678",
        "importer": "",
    }

    def test_cert_detail_loaded_under_canonical_name(self):
        """cert_detail 을 주면 정본 이름 certificateDetails 로 실린다."""
        body = naver_client._base_etc_notice(dict(self._DEFAULTS))
        assert (
            body.get("certificateDetails") == _CERT_VALUE
        ), f"정본 이름 certificateDetails 가 없음: {sorted(body.keys())}"
        assert (
            "certificationDetails" not in body
        ), "정본에 없는 오타 키 certificationDetails 가 실림"
        assert "certDetail" not in body, "정본에 없는 키 certDetail 이 실림"

    def test_no_cert_detail_omits_all_cert_keys(self):
        """cert_detail 이 없으면 세 키 모두 생략(조용한 채움 금지, fail-closed)."""
        defaults = dict(self._DEFAULTS)
        defaults["cert_detail"] = ""
        body = naver_client._base_etc_notice(defaults)
        for key in ("certificateDetails", "certificationDetails", "certDetail"):
            assert key not in body, f"값이 없는데 {key} 가 실림: {body.get(key)!r}"


# --------------------------------------------------------------------------- #
# 2. 조립기 산출 → 게이트: 값을 줬으면 누락 판정에서 빠진다.
# --------------------------------------------------------------------------- #
class TestAssemblerOutputThroughGate:
    """조립기 산출 notice 를 그대로 게이트에 넣었을 때의 판정."""

    def test_assembled_with_cert_not_flagged_missing(self):
        """cert_detail 를 줬으면 게이트가 certificateDetails 누락으로 지적하지 않음."""
        notice = _assemble_etc_notice(_CERT_VALUE)
        etc = notice.get("etc") or {}
        assert (
            etc.get("certificateDetails") == _CERT_VALUE
        ), f"조립기 산출에 정본 이름이 없음: {sorted(etc.keys())}"
        result = _run_gate(notice)
        details = _notice_rule_details(result)
        assert details == [], (
            f"값을 줬는데 고시 필수필드 누락 판정이 남음: {details} "
            f"(본문 키: {sorted(etc.keys())})"
        )

    def test_assembled_without_cert_flagged_missing(self):
        """통제군: cert_detail 을 안 주면 certificateDetails 누락으로 뜬다(fail-closed)."""
        notice = _assemble_etc_notice("")
        etc = notice.get("etc") or {}
        assert "certificateDetails" not in etc, "값을 안 줬는데 실림(조용한 채움)"
        result = _run_gate(notice)
        details = _notice_rule_details(result)
        assert len(details) == 1, f"고시 필수필드 위반이 정확히 1건이 아님: {details}"
        assert "certificateDetails" in details[0], f"누락 지적에 정본 이름이 없음: {details}"

    def test_control_fields_not_flagged_when_given(self):
        """통제군: 같은 조립 산출의 modelName·manufacturer 등은 누락에 안 뜬다."""
        notice = _assemble_etc_notice(_CERT_VALUE)
        result = _run_gate(notice)
        joined = " ".join(_notice_rule_details(result))
        for field in ("modelName", "manufacturer", "afterServiceDirector", "itemName"):
            assert field not in joined, f"값을 준 필드 {field} 가 누락으로 지적됨: {joined}"

    def test_gate_does_not_accept_typo_substitute(self):
        """결함 재발 방지: 게이트는 오타 키(certificationDetails/certDetail) 를
        certificateDetails 제공으로 인정하지 않는다. 조립기가 정본 이름을
        써야만 하는 이유."""
        notice = {
            "productInfoProvidedNoticeType": "ETC",
            "etc": {
                "itemName": "테스트 ETC 상품",
                "modelName": "TEST-MODEL-1",
                "manufacturer": "테스트제조사",
                "afterServiceDirector": "테스트제조사 070-1234-5678",
                "certificationDetails": _CERT_VALUE,  # 오타 키만 준 경우
                "returnCostReason": "단순변심 반품비용 구매자부담",
                "noRefundReason": "주문제작 청약철회 제한",
                "qualityAssuranceStandard": "관련법에 따름",
                "compensationProcedure": "소비자분쟁해결기준",
                "troubleShootingContents": "고객센터 문의",
            },
        }
        result = _run_gate(notice)
        details = _notice_rule_details(result)
        assert any(
            "certificateDetails" in d for d in details
        ), f"오타 키 certificationDetails 가 정본 제공으로 인정됨: {details}"

        notice["etc"] = {
            **{k: v for k, v in notice["etc"].items() if k != "certificationDetails"},
            "certDetail": _CERT_VALUE,  # 정본에 없는 키만 준 경우
        }
        result2 = _run_gate(notice)
        details2 = _notice_rule_details(result2)
        assert any(
            "certificateDetails" in d for d in details2
        ), f"정본에 없는 키 certDetail 이 정본 제공으로 인정됨: {details2}"
