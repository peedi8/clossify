"""KC 필요 여부 불명 카테고리의 fail-closed 동작 검증.

상세 조회가 실패해 ``exceptionalCategories`` 를 확정하지 못한 카테고리(91건,
``data/category_meta.json`` 의 ``incomplete.ids``) 가 컴플라이언스 게이트에서
조용히 "KC 불필요(False)" 로 판정되는 것을 막는 3-상태 판정을 검증한다.

검증 시나리오:
  1. ``data/category_meta.json`` 에 ``incomplete`` 키가 존재하고 건수가 일치.
  2. ``requires_kc()`` 기본 동작이 incomplete ID 에서 ``IncompleteCategoryError``.
  3. ``requires_kc(..., raise_if_incomplete=False)`` 가 incomplete ID 에서 ``None``.
  4. ``requires_kc()`` 가 incomplete 가 아닌 ID 에서는 ``True``/``False`` 만 반환.
  5. ``_compliance_code_check`` 가 incomplete 카테고리에서 FAIL 위반("KC 필요 여부 불명") 추가.
  6. 반례: incomplete ID 에 대한 FAIL 차단이 무동작이 아님(setup/teardown 으로 검증).

모든 테스트는 실제 네이버 API 를 호출하지 않는다.
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

from clossify import category_meta, common, naver_client, qa_agents


# --------------------------------------------------------------------------- #
# 헬퍼.
# --------------------------------------------------------------------------- #
def _first_incomplete_id() -> str:
    """incomplete.ids 의 첫 번째 ID 반환(테스트 픽스처)."""
    ids = category_meta.load_category_meta()["incomplete"]["ids"]
    assert ids, "incomplete.ids 가 비어 있음 — 데이터 갱신 필요"
    return ids[0]


def _first_known_kc_required_id() -> str:
    """incomplete 가 아닌 KC 필요 카테고리 ID 반환."""
    meta = category_meta.load_category_meta()
    incomplete = set(meta["incomplete"]["ids"])
    for cat in meta["categories"]:
        cid = str(cat.get("id"))
        if cid in incomplete:
            continue
        flags = cat.get("exceptionalCategories") or []
        if "KC_CERTIFICATION" in flags:
            return cid
    pytest.skip("KC_CERTIFICATION 카테고리가 메타에 없음 — 데이터 갱신 필요")


def _first_known_kc_not_required_id() -> str:
    """incomplete 가 아닌 KC 불필요 카테고리 ID 반환."""
    meta = category_meta.load_category_meta()
    incomplete = set(meta["incomplete"]["ids"])
    for cat in meta["categories"]:
        cid = str(cat.get("id"))
        if cid in incomplete:
            continue
        flags = cat.get("exceptionalCategories") or []
        if "KC_CERTIFICATION" not in flags:
            return cid
    pytest.skip("KC 불필요 카테고리가 메타에 없음")


# --------------------------------------------------------------------------- #
# 1. 데이터 자산 — incomplete 키 존재·건수.
# --------------------------------------------------------------------------- #
class TestIncompleteDataPresent:
    """data/category_meta.json 에 incomplete 키가 올바르게 존재하는가."""

    def test_incomplete_key_exists(self):
        meta = category_meta.load_category_meta(force=True)
        assert "incomplete" in meta, "incomplete 키가 category_meta.json 에 없음"
        assert isinstance(meta["incomplete"], dict)

    def test_incomplete_has_required_subkeys(self):
        inc = category_meta.load_category_meta(force=True)["incomplete"]
        for key in ("ids", "count", "reason", "impact"):
            assert key in inc, f"incomplete.{key} 이 없음"

    def test_incomplete_count_matches_ids_length(self):
        inc = category_meta.load_category_meta(force=True)["incomplete"]
        assert inc["count"] == len(
            inc["ids"]
        ), f"incomplete.count({inc['count']}) != len(ids)({len(inc['ids'])})"

    def test_incomplete_count_is_91(self):
        """상세 조회 실패 카테고리는 91건이어야 한다(수집 시점 기준)."""
        inc = category_meta.load_category_meta(force=True)["incomplete"]
        assert inc["count"] == 91, f"incomplete.count 가 91 이 아님: {inc['count']}"


# --------------------------------------------------------------------------- #
# 2. requires_kc 기본 동작 — IncompleteCategoryError.
# --------------------------------------------------------------------------- #
class TestRequiresKcIncompleteRaises:
    """requires_kc() 가 incomplete ID 에서 IncompleteCategoryError 를 발생시키는가."""

    def test_incomplete_id_raises_by_default(self):
        cid = _first_incomplete_id()
        with pytest.raises(category_meta.IncompleteCategoryError):
            category_meta.requires_kc(cid)

    def test_incomplete_id_raises_with_raise_if_unknown_true(self):
        """raise_if_unknown=True 여도 incomplete 면 IncompleteCategoryError."""
        cid = _first_incomplete_id()
        with pytest.raises(category_meta.IncompleteCategoryError):
            category_meta.requires_kc(cid, raise_if_unknown=True)


# --------------------------------------------------------------------------- #
# 3. requires_kc 3-상태 — None 반환 옵션.
# --------------------------------------------------------------------------- #
class TestRequiresKcThreeState:
    """raise_if_incomplete=False 일 때 None 반환(3-상태 판정)."""

    def test_incomplete_id_returns_none_when_suppressed(self):
        cid = _first_incomplete_id()
        result = category_meta.requires_kc(cid, raise_if_incomplete=False)
        assert (
            result is None
        ), f"incomplete ID 가 None 이 아님: {result!r} — False 를 반환하면 허위 신고 위험"

    def test_incomplete_id_returns_none_even_with_raise_if_unknown_false(self):
        cid = _first_incomplete_id()
        result = category_meta.requires_kc(cid, raise_if_unknown=False, raise_if_incomplete=False)
        assert result is None


# --------------------------------------------------------------------------- #
# 4. requires_kc — incomplete 가 아닌 ID 는 True/False 만 반환.
# --------------------------------------------------------------------------- #
class TestRequiresKcKnownIdsBoolean:
    """incomplete 가 아닌 ID 는 True 또는 False 만 반환(None 아님)."""

    def test_kc_required_id_returns_true(self):
        cid = _first_known_kc_required_id()
        result = category_meta.requires_kc(cid, raise_if_incomplete=False)
        assert result is True, f"KC 필요 카테고리가 True 가 아님: {cid} -> {result!r}"

    def test_kc_not_required_id_returns_false(self):
        cid = _first_known_kc_not_required_id()
        result = category_meta.requires_kc(cid, raise_if_incomplete=False)
        assert result is False, f"KC 불필요 카테고리가 False 가 아님: {cid} -> {result!r}"
        assert result is not None, "False 대신 None 반환 — incomplete 오판"


# --------------------------------------------------------------------------- #
# 5. 컴플라이언스 게이트 — incomplete 카테고리 FAIL 차단.
# --------------------------------------------------------------------------- #
class TestComplianceGateIncompleteFailClosed:
    """_compliance_code_check 가 incomplete 카테고리를 FAIL 로 차단하는가."""

    def _make_payload(self, category_id: str) -> dict:
        """테스트용 payload (KC 블록 없음)."""
        product = {
            "name": "테스트상품",
            "categoryId": category_id,
            "salePrice": 30000,
        }
        notice_cfg = {
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
        common_cfg = {
            "smartstore_notice_defaults": {
                "origin_area_code": "04",
                "origin_content": "중국",
            },
        }
        with mock.patch.object(naver_client, "_notice_config", return_value=notice_cfg):
            with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                payload = naver_client.build_payload(product, "<html></html>", ["http://x.png"])
        return payload, common_cfg

    def test_incomplete_category_produces_fail_verdict(self):
        cid = _first_incomplete_id()
        payload, common_cfg = self._make_payload(cid)
        with mock.patch.object(common, "cfg", return_value=common_cfg):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": cid},
                api_payload=payload,
            )
        assert (
            result["verdict"] == qa_agents.FAIL
        ), f"incomplete 카테고리인데 FAIL 이 아님: {result['verdict']}"

    def test_incomplete_category_has_unknown_kc_violation(self):
        cid = _first_incomplete_id()
        payload, common_cfg = self._make_payload(cid)
        with mock.patch.object(common, "cfg", return_value=common_cfg):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": cid},
                api_payload=payload,
            )
        rules = [str(v.get("rule") or "") for v in result["violations"]]
        assert any(
            "불명" in r or "미확정" in r for r in rules
        ), f"KC 불명 위반이 없음: {rules} — 조용히 통과하면 허위 신고 위험"

    def test_incomplete_violation_severity_is_fail(self):
        cid = _first_incomplete_id()
        payload, common_cfg = self._make_payload(cid)
        with mock.patch.object(common, "cfg", return_value=common_cfg):
            result = qa_agents._compliance_code_check(
                "테스트상품",
                {"category_id": cid},
                api_payload=payload,
            )
        for v in result["violations"]:
            if "불명" in str(v.get("rule") or "") or "미확정" in str(v.get("rule") or ""):
                assert (
                    str(v.get("severity") or "").upper() == qa_agents.FAIL
                ), f"KC 불명 위반의 severity 가 FAIL 이 아님: {v}"


# --------------------------------------------------------------------------- #
# 6. 무동작·identity 금지 — incomplete vs known KC 판정이 실제로 다르다.
# --------------------------------------------------------------------------- #
class TestNoNoOp:
    """incomplete 판정이 실제로 효과를 발휘하는가 (무동작 아님)."""

    def test_incomplete_none_differs_from_known_false(self):
        """incomplete(None) 은 known KC-불필요(False) 와 다르다."""
        inc_cid = _first_incomplete_id()
        known_cid = _first_known_kc_not_required_id()
        inc_result = category_meta.requires_kc(inc_cid, raise_if_incomplete=False)
        known_result = category_meta.requires_kc(known_cid, raise_if_incomplete=False)
        assert inc_result is None
        assert known_result is False
        assert inc_result != known_result, "incomplete 판정이 무동작이다 (False 와 동일)"

    def test_incomplete_none_differs_from_known_true(self):
        """incomplete(None) 은 known KC-필요(True) 와 다르다."""
        inc_cid = _first_incomplete_id()
        known_cid = _first_known_kc_required_id()
        inc_result = category_meta.requires_kc(inc_cid, raise_if_incomplete=False)
        known_result = category_meta.requires_kc(known_cid, raise_if_incomplete=False)
        assert inc_result is None
        assert known_result is True
        assert inc_result != known_result
