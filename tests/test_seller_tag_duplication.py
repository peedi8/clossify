# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
"""판매자 태그 중복 가시화(WARN) 검증 — 제조사·수입자 필드 중복 + 목록 자기 중복.

네이버 태그 규칙: "브랜드·제조사·판매처 같은 상품정보 필드 키워드는 태그에
중복 입력 X". 이 규칙은 **가이드라인**이지 API 거절 사유가 아니므로,
``qa_agents._compliance_code_check`` 는 겹침을 ``WARN`` violation 하나로
가시화만 한다. 태그를 지우거나 페이로드를 고치지 않는다(조용한 절삭 금지).

본 테스트가 다루는 계약:

  (1) 반례 — 실제 ``build_payload`` 산출 페이로드에서 태그가 제조사와 같은
      말('루아공방')이고 목록 안에 자기 중복('루아'/'루 아')이면
      ``rule="태그 중복"`` · ``severity=WARN`` violation 이 정확히 1건.
  (2) 통제군 — 겹치지 않는 태그 목록 → ``태그 중복`` violation 0건.
  (3) 페이로드 불변 — 검사 실행 전후 ``sellerTags`` 가 같다(태그를 지우지 않음).
  (4) 제한어 경로 회귀 — ``_strip_seller_tags`` 는 여전히 제한어를 제거한다
      (API 가 실제로 거절하는 경우라 성격이 다름).
  (5) 단위 — 정규화 재사용(``_normalize_seller_tag_text``)·수입자 겹침·
      부분 일치 미보고·빈 태그 무시·같은 겹침 중복 보고 금지.

모든 페이로드는 실제 조립기(``naver_client.build_payload``)로 만든 실측
모양을 쓴다 — ``seoInfo.sellerTags=[{"text": ...}]`` · 고시 노드 안
``manufacturer``/``importer``. 실호출 없음(config mock 주입).
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

from clossify import common, naver_client, qa_agents

# --------------------------------------------------------------------------- #
# 공통 픽스처 — ETC 고시 본문을 완비시키는 config (실측 키 이름).
# 제조사를 '루아공방' 으로 고정해 워크오더 반례를 재현한다.
# --------------------------------------------------------------------------- #

_CFG = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "루아공방",
    "model_name": "LUA-MINI-001",
    # itemName(품명) 은 상품명에서 자동으로 뽑지 않는다(사용자 결정
    # 2026-08-26) — ETC 필수필드 완비를 위해 config 값으로 준다.
    "item_name": "미니 화병",
    "cert_detail": "해당사항 없음",
    "return_cost_reason": "[CFG] 반품 배송비 안내",
    "no_refund_reason": "[CFG] 환불 불가 안내",
    "quality_assurance_standard": "[CFG] 품질 보증 기준",
    "compensation_procedure": "[CFG] 보상 절차",
    "trouble_shooting_contents": "[CFG] 고장 대처",
}


def _build_real_payload(tags, *, cfg_extra=None, notice=None):
    """실제 build_payload 로 등록 페이로드를 만든다 (실측 모양, 실호출 없음).

    ``tags`` 는 판매자 태그 목록. 고시 타입은 ETC — config 의 제조사가
    고시 본문 ``etc.manufacturer`` 로 실린다. 사용자 notice 로
    ``certificateDetails`` 만 채운다(빌더가 cert_detail 을 certificationDetails
    철자로 싣는 기존 사정이 있어, 태그 검증 외 위반이 섞이지 않게 한다).
    """
    cfg = dict(_CFG)
    cfg.update(cfg_extra or {})
    p = {
        "name": "루아 미니 화병",
        "categoryId": "50000001",
        "salePrice": 12000,
        "tags": list(tags),
        "notice": notice
        or {
            "productInfoProvidedNoticeType": "ETC",
            "etc": {"certificateDetails": "해당사항 없음"},
        },
    }
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=cfg),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    ):
        return naver_client.build_payload(
            p, "<html><body>상세</body></html>", ["https://cdn.test/vase.jpg"]
        )


def _run_compliance(payload):
    """payload 로 컴플라이언스 코드검사를 돌린다 (config mock 주입)."""
    with mock.patch.object(common, "cfg", return_value={"smartstore_notice_defaults": _CFG}):
        return qa_agents._compliance_code_check("루아 미니 화병", {}, api_payload=payload)


def _tag_dup_violations(result):
    """결과에서 rule='태그 중복' violation 만 골라낸다."""
    return [
        v
        for v in (result.get("violations") or [])
        if isinstance(v, dict) and str(v.get("rule") or "") == "태그 중복"
    ]


def _seller_tags(payload):
    """페이로드에서 sellerTags 리스트 원본 참조를 꺼낸다."""
    return payload["originProduct"]["detailAttribute"]["seoInfo"]["sellerTags"]


# --------------------------------------------------------------------------- #
# (1) 반례 — 실측 조합에서 WARN violation 이 정확히 1건.
# --------------------------------------------------------------------------- #


# 워크오더 반례 조합 — 제조사 '루아공방' 과 겹치는 태그 + 자기 중복 태그.
_COUNTEREXAMPLE_TAGS = ["루아", "루아", "미니화병", "루 아", "루아공방"]
_WEAR_NOTICE_WITHOUT_IMPORTER = {
    "productInfoProvidedNoticeType": "WEAR",
    "wear": {
        "material": "면 100%",
        "color": "아이보리",
        "size": "FREE",
        "caution": "단독 세탁",
        "packDateText": "2026년 1월",
        "warrantyPolicy": "관련 법령 및 소비자분쟁해결기준에 따름",
    },
}


class TestCounterexampleDetected:
    """워크오더 반례: 태그 ['루아','루아','미니화병','루 아','루아공방'] + 제조사 '루아공방'."""

    def test_counterexample_reports_single_warn_violation(self):
        """반례 → '태그 중복' 1건 · severity WARN · detail 에 두 겹침 모두."""
        payload = _build_real_payload(_COUNTEREXAMPLE_TAGS)
        result = _run_compliance(payload)
        found = _tag_dup_violations(result)
        assert len(found) == 1, (
            f"'태그 중복' violation 이 정확히 1건이어야 함: "
            f"violations={result.get('violations')}"
        )
        v = found[0]
        assert (
            str(v.get("severity") or "").upper() == qa_agents.WARN
        ), f"severity 가 WARN 이어야 함(FAIL 승격 금지): {v}"
        detail = str(v.get("detail") or "")
        # 필드 중복 — 태그 '루아공방' 이 제조사와 같은 말.
        assert "태그 '루아공방'이 제조사와 같습니다" in detail, f"detail={detail!r}"
        # 자기 중복 — '루아' 와 '루 아' 가 정규화 뒤 같은 말(3회).
        assert "태그 '루아'가 목록에 3번 있습니다" in detail, f"detail={detail!r}"
        # 같은 겹침을 두 번 보고하지 않는다 — '루 아' 는 '루아' 로 묶여 별도 보고 없음.
        assert detail.count("제조사와 같습니다") == 1, f"detail={detail!r}"
        assert detail.count("목록에") == 1, f"detail={detail!r}"

    def test_counterexample_verdict_is_warn_not_fail(self):
        """다른 위반이 없으면 verdict 는 WARN — 가이드라인 위반은 차단이 아니다."""
        payload = _build_real_payload(_COUNTEREXAMPLE_TAGS)
        result = _run_compliance(payload)
        assert (
            result["verdict"] == qa_agents.WARN
        ), f"verdict 가 WARN 이어야 함(태그 중복은 게이트를 막지 않음): {result}"

    def test_counterexample_tag_list_untouched_after_check(self):
        """(3) 검사 실행 후에도 sellerTags 내용·원본 리스트 객체가 그대로다."""
        payload = _build_real_payload(_COUNTEREXAMPLE_TAGS)
        before = copy.deepcopy(_seller_tags(payload))
        tags_ref = _seller_tags(payload)
        _run_compliance(payload)
        assert _seller_tags(payload) == before, "검사가 sellerTags 를 변경함(절삭 금지 위반)"
        assert tags_ref == before, "원본 리스트 객체가 변경됨(절삭 금지 위반)"


# --------------------------------------------------------------------------- #
# (2) 통제군 — 겹치지 않으면 0건.
# --------------------------------------------------------------------------- #


class TestControlGroupClean:
    """겹치지 않는 태그 목록(예: 미니화병/탁상화병/홈데코) → 위반 0건."""

    def test_control_group_zero_tag_dup_violations(self):
        payload = _build_real_payload(["미니화병", "탁상화병", "홈데코"])
        result = _run_compliance(payload)
        found = _tag_dup_violations(result)
        assert found == [], f"겹침이 없는데 '태그 중복' 보고됨: {found}"

    def test_control_group_verdict_unaffected(self):
        """통제군은 태그 때문에 verdict 가 바뀌지 않는다(PASS 유지)."""
        payload = _build_real_payload(["미니화병", "탁상화병", "홈데코"])
        result = _run_compliance(payload)
        assert (
            result["verdict"] == qa_agents.PASS
        ), f"통제군 verdict 가 PASS 여야 함: violations={result.get('violations')}"


# --------------------------------------------------------------------------- #
# 원산지 정보 수입자 — 실제 조립기 경로의 반례와 통제군.
# --------------------------------------------------------------------------- #


class TestOriginAreaImporter:
    """``originAreaInfo.importer`` 는 고시 본문과 함께 태그 비교 대상이다."""

    def test_origin_area_importer_overlap_reports_warn(self):
        """WEAR 본문에 importer 가 없어도 실제 payload 수입자 겹침을 보고한다."""
        payload = _build_real_payload(
            ["수입상사", "미니화병"],
            cfg_extra={"importer": "수입상사"},
            notice=_WEAR_NOTICE_WITHOUT_IMPORTER,
        )
        detail_attr = payload["originProduct"]["detailAttribute"]
        assert detail_attr["originAreaInfo"]["importer"] == "수입상사"
        assert "importer" not in detail_attr["productInfoProvidedNotice"]["wear"]

        found = _tag_dup_violations(_run_compliance(payload))

        assert len(found) == 1, f"원산지 수입자 겹침이 보고되지 않음: {found}"
        assert found[0]["severity"] == qa_agents.WARN
        assert "태그 '수입상사'가 수입자와 같습니다" in found[0]["detail"]

    def test_origin_area_importer_control_group_has_no_warning(self):
        """다른 태그는 같은 원산지 수입자가 있어도 중복으로 보고하지 않는다."""
        payload = _build_real_payload(
            ["미니화병", "탁상화병"],
            cfg_extra={"importer": "수입상사"},
            notice=_WEAR_NOTICE_WITHOUT_IMPORTER,
        )

        assert _tag_dup_violations(_run_compliance(payload)) == []

    def test_blank_origin_area_importer_does_not_match_blank_tag(self):
        """빈 수입자와 빈 태그는 비교 대상에서 제외해 오탐을 막는다."""
        payload = _build_real_payload(
            ["", "미니화병"],
            cfg_extra={"importer": ""},
            notice=_WEAR_NOTICE_WITHOUT_IMPORTER,
        )

        assert payload["originProduct"]["detailAttribute"]["originAreaInfo"].get("importer") is None
        assert _tag_dup_violations(_run_compliance(payload)) == []


# --------------------------------------------------------------------------- #
# (5) 단위 — 수입자 겹침 · 정규화 재사용 · 부분 일치 · 빈 태그 · 중복 보고 금지.
# --------------------------------------------------------------------------- #


class TestTagDuplicationUnit:
    """_seller_tag_duplication_violation 단위 계약."""

    def _violation(self, tags, body):
        detail_attr = {"seoInfo": {"sellerTags": [{"text": t} for t in tags]}}
        return qa_agents._seller_tag_duplication_violation(detail_attr, body)

    def test_importer_overlap_reported(self):
        """태그가 고시 본문 importer 와 같은 말이면 수입자 겹침으로 보고."""
        v = self._violation(
            ["루아수입상사"], {"manufacturer": "루아공방", "importer": "루아수입상사"}
        )
        assert v is not None
        assert v["rule"] == "태그 중복"
        assert v["severity"] == qa_agents.WARN
        assert "태그 '루아수입상사'가 수입자와 같습니다" in v["detail"]

    def test_normalization_reused_spacing_and_hash(self):
        """'루아'/'루 아'/'#루아' 는 같은 말로 본다(정규화 재사용)."""
        v = self._violation(["루아", "루 아", "#루아"], {"manufacturer": "루아공방"})
        assert v is not None
        assert "태그 '루아'가 목록에 3번 있습니다" in v["detail"]

    def test_case_insensitive_latin_tag(self):
        """대소문자만 다른 라틴 태그도 같은 말로 본다(소문자화 재사용)."""
        v = self._violation(["WarmCo", "warmco"], {"manufacturer": "루아공방"})
        assert v is not None
        assert "목록에 2번 있습니다" in v["detail"]

    def test_partial_match_not_reported(self):
        """부분 문자열 겹침('루아공방류' vs '루아공방')은 같은 말이 아니다."""
        v = self._violation(["루아공방류"], {"manufacturer": "루아공방"})
        assert v is None, f"부분 일치를 겹침으로 보고함: {v}"

    def test_blank_tags_ignored(self):
        """공백·'#' 만으로 정규화되는 빈 태그는 세지 않는다."""
        v = self._violation(["", "   ", "#", "  #  "], {"manufacturer": "루아공방"})
        assert v is None, f"빈 태그를 겹침으로 보고함: {v}"

    def test_field_and_self_overlap_reported_once_each(self):
        """제조사 겹침 + 자기 중복이 함께 있어도 겹침당 1회씩만 보고."""
        v = self._violation(["루아공방", "루아공방"], {"manufacturer": "루아공방"})
        assert v is not None
        assert v["detail"].count("제조사와 같습니다") == 1, v["detail"]
        assert v["detail"].count("목록에 2번 있습니다") == 1, v["detail"]

    def test_missing_seo_info_returns_none(self):
        """seoInfo/sellerTags 가 없으면 위반 없음(관측 없음 = 단정 없음)."""
        assert qa_agents._seller_tag_duplication_violation({}, {"manufacturer": "루아공방"}) is None
        assert qa_agents._seller_tag_duplication_violation(None, {}) is None
        detail_attr = {"seoInfo": {"sellerTags": "not-a-list"}}
        assert qa_agents._seller_tag_duplication_violation(detail_attr, {}) is None

    def test_plain_string_tags_tolerated(self):
        """dict 가 아닌 문자열 태그 원소도 _seller_tag_text 규약대로 읽는다."""
        detail_attr = {"seoInfo": {"sellerTags": ["루아", "루아"]}}
        v = qa_agents._seller_tag_duplication_violation(detail_attr, {"manufacturer": "루아공방"})
        assert v is not None
        assert "목록에 2번 있습니다" in v["detail"]

    def test_no_notice_body_no_field_overlap(self):
        """고시 본문이 비어 있으면 필드 중복 판정 자체가 불가능하다."""
        v = self._violation(["루아공방"], {})
        assert v is None, f"본문 없는데 필드 겹침 보고됨: {v}"


# --------------------------------------------------------------------------- #
# (4) 제한어 경로 회귀 — _strip_seller_tags 는 여전히 제한어를 제거한다.
# --------------------------------------------------------------------------- #


class TestRestrictedStripRegression:
    """API 가 실제로 거절하는 제한어 자동 제거는 그대로 동작해야 한다."""

    def test_strip_removes_only_restricted_term(self):
        payload = _build_real_payload(["미니화병", "화병", "홈데코"])
        removed = naver_client._strip_seller_tags(payload, {"화병"})
        assert removed == ["화병"], f"제한어 '화병' 만 제거되어야 함: {removed}"
        texts = [t["text"] for t in _seller_tags(payload)]
        assert texts == ["미니화병", "홈데코"], f"남은 태그가 다름: {texts}"

    def test_strip_with_known_restricted_constant(self):
        """KNOWN_RESTRICTED_SELLER_TAGS 상수 경로도 여전히 제거한다."""
        payload = _build_real_payload(["도자기", "미니화병"])
        removed = naver_client._strip_seller_tags(
            payload, naver_client.KNOWN_RESTRICTED_SELLER_TAGS
        )
        assert removed == ["도자기"], f"제한어 '도자기' 가 제거되지 않음: {removed}"
        texts = [t["text"] for t in _seller_tags(payload)]
        assert texts == ["미니화병"], f"남은 태그가 다름: {texts}"

    def test_strip_noop_without_restricted_tags(self):
        """겹치는 제한어가 없으면 태그 목록을 건드리지 않는다."""
        payload = _build_real_payload(["미니화병", "탁상화병", "홈데코"])
        before = copy.deepcopy(_seller_tags(payload))
        removed = naver_client._strip_seller_tags(
            payload, naver_client.KNOWN_RESTRICTED_SELLER_TAGS
        )
        assert removed == []
        assert _seller_tags(payload) == before


# --------------------------------------------------------------------------- #
# 게이트 성격 — WARN 은 차단이 아니라 가시화다 (상설 조항).
# --------------------------------------------------------------------------- #


class TestWarnDoesNotBlock:
    """태그 중복 WARN 만으로는 QA 게이트가 막히지 않는다."""

    def test_gate_allows_warn_verdict(self):
        payload = _build_real_payload(_COUNTEREXAMPLE_TAGS)
        result = _run_compliance(payload)
        allowed, reason = qa_agents.qa_gate({"qa": result})
        assert allowed is True, f"WARN 이 게이트를 막음(가시화 계약 위반): {reason}"

    def test_gate_blocks_control_never(self):
        payload = _build_real_payload(["미니화병", "탁상화병", "홈데코"])
        result = _run_compliance(payload)
        allowed, _ = qa_agents.qa_gate({"qa": result})
        assert allowed is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
