# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""이름↔팩트 모순 게이트 검증 테스트 (이름·사실 모순 등록 차단 워크오더).

검증 시나리오 (a)-(h):

  (a) 이름 "손잡이 있는 도자기 컵" + 팩트 {손잡이 디자인: 손잡이 없는 디자인}
      → conflict(polarity, topic 손잡이).
  (b) 이름 "뚜껑 포함 머그" + 팩트 {뚜껑: 미포함} → conflict.
  (c) 이름 "세라믹 컵" + 팩트 {소재: 유리} → conflict(material).
  (d) 이름 "도자기 컵" + 팩트 {소재: 세라믹} → ok(동의어군).
  (e) 팩트 없음 → skipped + 사유.
  (f) register_product: conflict prepared → 거부(blocked_by=name_fact_conflict),
      name_conflict_acknowledged=True → 통과(사람 확인 경로).
  (g) 주제어 불일치(이름 손잡이/팩트 뚜껑) → 판정 안 함.
  (h) 실물: 머그 번들 766133149201 의 title_ko + source_info_ko.facts 로
      → conflict 1건(손잡이).

모든 테스트는 실제 네이버 API 를 호출하지 않는다 — monkeypatch 로 네트워크 차단.
게이트는 결정론 규칙만 쓴다(LLM 0·외부 호출 0·새 의존성 0).
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

from clossify import common, mcp_server, name_fact, naver_client, register

# --------------------------------------------------------------------------- #
# 공통 픽스처.
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
}


def _fake_attach_ok(sources):
    urls = [f"http://cdn/test/nfc{i}.png" for i in range(len(sources))]
    return {"urls": urls, "rejected": [], "notes": []}


def _compliant_payload():
    """컴플라이언스 통과용 임시 build_payload 반환값."""
    return {
        "originProduct": {
            "originProductNo": "test-no",
            "images": {"representativeImage": {"url": "http://cdn/test/rep.png"}},
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "ETC",
                    "etc": {"itemName": "테스트"},
                },
                "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                "afterServiceInfo": {"afterServiceTelephoneNumber": "070-1234-5678"},
            },
        }
    }


def _run_prepare(d: dict) -> dict:
    """준비 단계(prepare_listing) 를 네트워크 차단 상태로 실행."""
    with (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common,
            "cfg",
            return_value={
                "smartstore_notice_defaults": {"origin_area_code": "04", "origin_content": "중국"}
            },
        ),
        mock.patch.object(
            naver_client, "build_payload", side_effect=lambda *a, **kw: _compliant_payload()
        ),
    ):
        return register.prepare_listing(d, attach_fn=_fake_attach_ok)


# 실물 머그 번들 검증 경로: 저장소 스캔 위반(로컬 경로 단어) 없이 실행 시점에
# 주입한다 — 환경변수로 source_info_ko.json 절대경로를 넘긴다. 없으면 스티는
# 것은 스킵한다(워크오더는 실물 검증을 요구하므로 실측 환경에서는 반드시
# 설정하고 실행한다).
_BUNDLE_ENV = "CLOSSIFY_BUNDLE_SOURCE_INFO_KO"


def _load_bundle() -> dict | None:
    import os

    env_path = os.environ.get(_BUNDLE_ENV)
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


# --------------------------------------------------------------------------- #
# (a)-(e), (g) 게이트 규칙 — 결정론 판정.
# --------------------------------------------------------------------------- #
class TestGateRules:
    """name_fact.check_name_facts 의 결정론 규칙 검증."""

    def test_a_handle_polarity_conflict(self):
        """(a) 손잡이 있는 ↔ 손잡이 없는 디자인 → conflict(polarity, 손잡이)."""
        r = name_fact.check_name_facts(
            "손잡이 있는 도자기 컵",
            [{"name": "손잡이 디자인", "value": "손잡이 없는 디자인"}],
        )
        assert r["status"] == "conflict"
        assert len(r["conflicts"]) == 1
        c = r["conflicts"][0]
        assert c["topic"] == "손잡이"
        assert c["name_says"] == "있는"
        assert "없는" in c["fact_says"]
        assert c["rule"] == "polarity"

    def test_b_lid_included_vs_excluded(self):
        """(b) 뚜껑 포함 ↔ 미포함 → conflict."""
        r = name_fact.check_name_facts("뚜껑 포함 머그", [{"name": "뚜껑", "value": "미포함"}])
        assert r["status"] == "conflict"
        assert r["conflicts"][0]["topic"] == "뚜껑"
        assert r["conflicts"][0]["rule"] == "polarity"

    def test_c_material_group_conflict(self):
        """(c) 세라믹 ↔ 유리 → conflict(material)."""
        r = name_fact.check_name_facts("세라믹 컵", [{"name": "소재", "value": "유리"}])
        assert r["status"] == "conflict"
        c = r["conflicts"][0]
        assert c["rule"] == "material"
        assert c["topic"] == "소재"

    def test_d_material_synonym_passes(self):
        """(d) 도자기 ↔ 세라믹 → ok(같은 동의어군)."""
        r = name_fact.check_name_facts("도자기 컵", [{"name": "소재", "value": "세라믹"}])
        assert r["status"] == "ok"
        assert r["conflicts"] == []

    def test_e_no_facts_skipped_with_reason(self):
        """(e) 팩트 없음 → skipped + 사유(조용한 통과 금지)."""
        for empty in (None, [], "x", [{"junk": 1}]):
            r = name_fact.check_name_facts("도자기 컵", empty)
            assert r["status"] == "skipped", f"{empty!r} → {r}"
            assert r["reason"] == "facts 없음"

    def test_g_topic_mismatch_not_judged(self):
        """(g) 이름 손잡이 / 팩트 뚜껑(주제어 불일치) → 판정 안 함(ok)."""
        r = name_fact.check_name_facts(
            "손잡이 있는 도자기 컵", [{"name": "뚜껑", "value": "미포함"}]
        )
        assert r["status"] == "ok"
        assert r["conflicts"] == []

    def test_generation_track_name_ko_form_accepted(self):
        """생성 트랙 name_ko/value_ko 형태도 같이 판정한다."""
        r = name_fact.check_name_facts(
            "손잡이 있는 도자기 컵",
            [{"name_ko": "손잡이 디자인", "value_ko": "손잡이 없는 디자인"}],
        )
        assert r["status"] == "conflict"


# --------------------------------------------------------------------------- #
# prepare_listing 통합 — name_fact_check 키 + needs_user.
# --------------------------------------------------------------------------- #
class TestPrepareListingIntegration:
    """prepare_listing 이 게이트 결과를 payload 와 needs_user 에 싣는가."""

    def test_prepare_conflict_in_payload_and_needs_user(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "prepared"
        fake_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
        payload = _run_prepare(
            {
                "name": "손잡이 있는 도자기 컵",
                "salePrice": 12000,
                "image_sources": ["http://cdn/a.png"],
                "facts": [{"name": "손잡이 디자인", "value": "손잡이 없는 디자인"}],
            }
        )
        nfc = payload.get("name_fact_check")
        assert isinstance(nfc, dict) and nfc["status"] == "conflict"
        name_hints = [n for n in payload.get("needs_user") or [] if n.get("field") == "name"]
        assert name_hints, "conflict 인데 needs_user 에 상품명 확인 항목이 없음"
        hint = name_hints[0]
        assert hint["label"] == "상품명 사실 확인"
        assert hint.get("answer_shape") == "text"
        assert "손잡이" in hint["why"]

    def test_prepare_no_facts_reports_skipped(self, tmp_path, monkeypatch):
        fake_dir = tmp_path / "prepared"
        fake_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
        payload = _run_prepare(
            {"name": "도자기 컵", "salePrice": 12000, "image_sources": ["http://cdn/a.png"]}
        )
        nfc = payload.get("name_fact_check")
        assert nfc["status"] == "skipped"
        assert nfc.get("reason") == "facts 없음"

    def test_mcp_prepare_success_always_carries_name_fact_check(self):
        """mcp_server.prepare_listing 성공 반환 최상위에 name_fact_check 항상 존재.

        준비 본체(register.prepare_listing) 가 payload 에 name_fact_check 를
        싣어도 도구 응답에서 빠지면 계약 위반이다 — ok/conflict/skipped 어느
        상태든 3항 형식(status, conflicts, skipped 시 reason) 그대로 노출.
        """
        for nfc in (
            {"status": "ok", "conflicts": []},
            {
                "status": "conflict",
                "conflicts": [
                    {
                        "topic": "손잡이",
                        "name_says": "있는",
                        "fact_says": "손잡이 없는 디자인",
                        "rule": "polarity",
                    }
                ],
            },
            {"status": "skipped", "conflicts": [], "reason": "facts 없음"},
        ):
            fake_payload = {
                "product_key": "nfctest000003",
                "needs_llm": [],
                "needs_user": [],
                "qa": {"agents": []},
                "images": {"listing_urls": [], "detail_urls": []},
                "name_fact_check": nfc,
            }
            with (
                mock.patch.object(
                    mcp_server._register_mod, "prepare_listing", return_value=fake_payload
                ),
                # 실제 prepared 디렉터리에 쓰지 않도록 저장만 스텁한다.
                mock.patch.object(
                    mcp_server._register_mod, "write_prepared_payload", return_value=None
                ),
            ):
                result = mcp_server.prepare_listing(
                    {"name": "도자기 컵", "salePrice": 12000, "image_sources": ["http://cdn/a.png"]}
                )
            assert result["ok"] is True
            top = result.get("name_fact_check")
            assert top == nfc, f"성공 반환 최상위 name_fact_check 누락/불일치: {result.keys()}"
            assert top["status"] in {"ok", "conflict", "skipped"}
            if top["status"] == "skipped":
                assert top.get("reason")


# --------------------------------------------------------------------------- #
# (f) register_product 게이트 — fail-closed + 사람 확인 경로.
# --------------------------------------------------------------------------- #
class TestRegisterProductGate:
    """register_product 가 미해결 모순을 거부하는가."""

    def _conflict_prepared(self):
        return {
            "product_key": "nfctest000001",
            "product": {"name": "손잡이 있는 도자기 컵", "salePrice": 12000},
            "name_fact_check": name_fact.check_name_facts(
                "손잡이 있는 도자기 컵",
                [{"name": "손잡이 디자인", "value": "손잡이 없는 디자인"}],
            ),
        }

    def test_f_blocks_conflict_prepared(self):
        """(f) conflict prepared → 거부(blocked_by=name_fact_conflict), 네이버 0회."""
        naver_calls = []
        with mock.patch.object(
            mcp_server._register_mod,
            "resolve_prepared_for_register",
            return_value=(
                self._conflict_prepared(),
                {"key": "nfctest000001", "source": "explicit", "name": "", "salePrice": None},
            ),
        ):
            with mock.patch.object(
                naver_client,
                "register_product",
                side_effect=lambda *a, **k: naver_calls.append(1) or (200, {}),
            ):
                result = mcp_server.register_product(
                    name="손잡이 있는 도자기 컵",
                    price=12000,
                    category_id="50000001",
                    image_urls=["http://cdn/x.png"],
                    detail_html="<html><body>x</body></html>",
                    preview_confirmed=True,
                )
        assert result["ok"] is False
        assert result.get("blocked_by") == "name_fact_conflict"
        assert len(naver_calls) == 0, "모순 미해결 상태에서 네이버 API 호출 발생"
        assert "손잡이" in str(result.get("message") or result.get("error"))

    def test_f_acknowledged_passes_gate(self):
        """(f) name_conflict_acknowledged=True → 모순 게이트 통과(사람 확인 경로)."""
        with mock.patch.object(
            mcp_server._register_mod,
            "resolve_prepared_for_register",
            return_value=(
                self._conflict_prepared(),
                {"key": "nfctest000001", "source": "explicit", "name": "", "salePrice": None},
            ),
        ):
            with mock.patch.object(
                naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
            ):
                with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                    with mock.patch.object(
                        common,
                        "cfg",
                        return_value={
                            "smartstore_notice_defaults": {
                                "origin_area_code": "04",
                                "origin_content": "중국",
                            }
                        },
                    ):
                        result = mcp_server.register_product(
                            name="손잡이 있는 도자기 컵",
                            price=12000,
                            category_id="50000001",
                            image_urls=["http://cdn/x.png"],
                            detail_html="<html><body>x</body></html>",
                            preview_confirmed=True,
                            name_conflict_acknowledged=True,
                        )
        # 모순 게이트는 통과해야 한다(이후 게이트 결과와 무관하게).
        assert (
            result.get("blocked_by") != "name_fact_conflict"
        ), f"사람 확인 경로가 여전히 모순 게이트에 막힘: {result}"

    def test_ok_prepared_not_blocked(self):
        """name_fact_check ok/skipped prepared 는 모순 게이트에 막히지 않는다."""
        prepared = {
            "product_key": "nfctest000002",
            "product": {"name": "도자기 컵", "salePrice": 12000},
            "name_fact_check": name_fact.check_name_facts(
                "도자기 컵", [{"name": "소재", "value": "세라믹"}]
            ),
        }
        with mock.patch.object(
            mcp_server._register_mod,
            "resolve_prepared_for_register",
            return_value=(
                prepared,
                {"key": "nfctest000002", "source": "explicit", "name": "", "salePrice": None},
            ),
        ):
            with mock.patch.object(
                naver_client, "_notice_config", return_value=_NOTICE_CFG_WITH_ORIGIN
            ):
                with mock.patch.object(naver_client, "_kc_config", return_value=({}, "")):
                    with mock.patch.object(
                        common,
                        "cfg",
                        return_value={
                            "smartstore_notice_defaults": {
                                "origin_area_code": "04",
                                "origin_content": "중국",
                            }
                        },
                    ):
                        result = mcp_server.register_product(
                            name="도자기 컵",
                            price=12000,
                            category_id="50000001",
                            image_urls=["http://cdn/x.png"],
                            detail_html="<html><body>x</body></html>",
                            preview_confirmed=True,
                        )
        assert result.get("blocked_by") != "name_fact_conflict"


# --------------------------------------------------------------------------- #
# (h) 실물 머그 번들 766133149201 검증.
# --------------------------------------------------------------------------- #
class TestRealBundleMug:
    """실물 번들 title_ko + source_info_ko.facts → conflict 1건(손잡이)."""

    def test_h_real_bundle_yields_handle_conflict(self):
        bundle = _load_bundle()
        if bundle is None:
            import pytest

            pytest.skip("실물 번들 source_info_ko.json 을 찾을 수 없어 스킵")
        r = name_fact.check_name_facts(bundle.get("title_ko"), bundle.get("facts"))
        assert r["status"] == "conflict", f"실물 번들이 conflict 여야 함: {r}"
        assert len(r["conflicts"]) == 1, f"conflict 는 정확히 1건이어야 함: {r['conflicts']}"
        c = r["conflicts"][0]
        assert c["topic"] == "손잡이"
        assert c["rule"] == "polarity"
        # 소재(도자기↔세라믹) 는 동의어군이라 추가 conflict 이 없어야 한다.
