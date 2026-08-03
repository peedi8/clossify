"""T-201d — 상세 렌더 + prepare 본체 + 위임 왕복 + 게이트 승격 검증 테스트.

작업지시(T-201d) 의 Acceptance 반례 전체를 단위 테스트로 구현한다.
실제 네이버 API 호출, 파일시스템 사용을 차단(monkeypatch + tmp_path).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import (
    common,
    detail_render,
    mcp_server,
    naver_client,
    qa_agents,
    register,
)


# --------------------------------------------------------------------------- #
# 공통: tmp_path 기반 prepared dir 격리.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """common.PREPARED_DIR 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


def _fake_attach_ok(sources):
    """images.attach_images 대체 — 항상 URL 리스트 반환."""
    urls = [f"http://cdn/test/img{i}.png" for i in range(len(sources))]
    return {"urls": urls, "rejected": [], "notes": []}


def _fake_attach_rejected(sources):
    """images.attach_images 대체 — 거부 항목 반환."""
    return {
        "urls": [],
        "rejected": [{"index": 0, "source": sources[0], "reason": "거부사유"}],
        "notes": [],
    }


# --------------------------------------------------------------------------- #
# URL 거부 반례.
# --------------------------------------------------------------------------- #
class TestUrlRejection:
    """URL 키 존재 시 ValueError."""

    @pytest.mark.parametrize("url_key", ["url", "source_url", "item_url", "detail_url"])
    def test_prepare_rejects_url_key(self, url_key):
        d = {"name": "테스트", "salePrice": 10000, "image_sources": ["x.png"], url_key: "http://x"}
        with pytest.raises(ValueError, match="URL 기반"):
            register.prepare_listing(d, attach_fn=_fake_attach_ok)

    def test_prepare_url_with_images_still_rejected(self):
        d = {"name": "테스트", "salePrice": 10000, "url": "http://x", "images": ["a.png"]}
        with pytest.raises(ValueError, match="URL 기반"):
            register.prepare_listing(d, attach_fn=_fake_attach_ok)


# --------------------------------------------------------------------------- #
# 이미지 0장 거부.
# --------------------------------------------------------------------------- #
class TestZeroImageRejection:
    """이미지 0장이면 등록/prepare 거부."""

    def test_prepare_rejects_no_image_sources(self, isolated_prepared_dir):
        d = {"name": "테스트", "salePrice": 10000}
        with pytest.raises(ValueError, match="image_sources"):
            register.prepare_listing(d, attach_fn=_fake_attach_ok)

    def test_prepare_rejects_rejected_images(self, isolated_prepared_dir):
        d = {"name": "테스트", "salePrice": 10000, "image_sources": ["bad.png"]}
        with pytest.raises(ValueError, match="거부"):
            register.prepare_listing(d, attach_fn=_fake_attach_rejected)

    def test_register_prepared_rejects_zero_listing_urls(self, isolated_prepared_dir):
        """listing_urls 가 0장이면 ValueError."""
        pkey = register.make_product_key("테스트", 10000)
        payload = {
            "product_key": pkey,
            "images": {"listing_urls": [], "detail_urls": []},
            "detail_html": "<html></html>",
            "qa": qa_agents.aggregate_qa_results([]),
            "version": common.PREPARED_PAYLOAD_VERSION,
        }
        register.write_prepared_payload(payload)
        with pytest.raises(ValueError, match="0장"):
            register.register_prepared_listing(
                {"product_key": pkey, "name": "테스트", "salePrice": 10000}
            )


# --------------------------------------------------------------------------- #
# payload 키 정합 반례.
# --------------------------------------------------------------------------- #
class TestPayloadKeyAlignment:
    """prepare_listing 이 쓴 키를 register_prepared_listing 이 읽는가."""

    def test_prepare_writes_and_register_reads_listing_urls(
        self, isolated_prepared_dir, monkeypatch
    ):
        """prepare_listing 이 images.listing_urls 를 쓰고,
        register_prepared_listing 이 그 키를 읽어 네이버에 반영하는가."""
        d = {
            "name": "테스트상품",
            "salePrice": 20000,
            "image_sources": ["a.png", "b.png"],
            "category_id": "50002366",
        }
        payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

        # payload 가 images.listing_urls / images.detail_urls / detail_html 키를 가짐.
        assert "images" in payload
        assert isinstance(payload["images"], dict)
        assert isinstance(payload["images"].get("listing_urls"), list)
        assert len(payload["images"]["listing_urls"]) == 2
        assert isinstance(payload.get("detail_html"), str)
        assert len(payload["detail_html"]) > 0

        # register_prepared_listing 이 그 키를 읽어 naver_client.build_payload 에 전달.
        naver_calls = []

        def fake_build(product, detail_html, image_urls, status="SALE"):
            naver_calls.append(
                {
                    "product": product,
                    "detail_html": detail_html,
                    "image_urls": list(image_urls),
                    "status": status,
                }
            )
            return {"originProduct": {"originProductNo": "test-no"}}

        def fake_register(api_payload):
            return (200, {"originProductNo": "test-no"})

        def fake_get(origin_no):
            return (200, {"originProduct": {"originProductNo": origin_no}})

        # submit_reviews 로 image/copy PENDING → PASS 해소.
        register.submit_reviews(
            payload["product_key"],
            [
                {"agent": "image", "verdict": "PASS"},
                {"agent": "copy", "verdict": "PASS"},
            ],
        )
        # compliance 는 submit_reviews 로 제출할 수 없다(결정론 검사).
        # 테스트의 목적은 URL 키 정합이지 compliance 게이트가 아니므로,
        # compliance 를 PASS 로 직접 덮어쓴다(테스트 격리).
        _p = register.load_prepared_payload(product_key=payload["product_key"])
        _qa = _p.get("qa") or {}
        for _row in _qa.get("agents") or []:
            if isinstance(_row, dict) and _row.get("agent") == "compliance":
                _row["verdict"] = qa_agents.PASS
                _row["violations"] = []
        # 집계 verdict 재계산.
        _agg = qa_agents.aggregate_qa_results(_qa.get("agents") or [])
        _p["qa"] = _agg
        register.write_prepared_payload(_p)

        monkeypatch.setattr(naver_client, "build_payload", fake_build)
        monkeypatch.setattr(naver_client, "register_product", fake_register)
        monkeypatch.setattr(naver_client, "get_product", fake_get)

        result = register.register_prepared_listing(
            {"product_key": payload["product_key"], "name": "테스트상품", "salePrice": 20000}
        )
        assert result["ok"] is True, f"register_prepared_listing 실패: {result}"
        # naver_client.build_payload 가 listing_urls 를 받았는가.
        assert len(naver_calls) == 1
        received_urls = naver_calls[0]["image_urls"]
        assert len(received_urls) == 2, f"이미지가 0장으로 넘어감(무음 실패): {received_urls}"


# --------------------------------------------------------------------------- #
# version 불일치 거부.
# --------------------------------------------------------------------------- #
class TestVersionMismatch:
    """다른 버전 payload 로드 시 명시 예외."""

    def test_version_mismatch_raises(self, isolated_prepared_dir):
        pkey = register.make_product_key("버전테스트", 10000)
        # version 99 로 저장.
        payload = {
            "product_key": pkey,
            "version": 99,
            "images": {"listing_urls": ["http://x.png"]},
            "detail_html": "<html></html>",
        }
        register.write_prepared_payload(payload)
        # write_prepared_payload 이 version 을 덮어쓰므로, 강제로 다시 쓰기.
        # (write_prepared_payload 은 PREPARED_PAYLOAD_VERSION 을 항상 붙인다)
        # 따라서 version 검사는 파일을 직접 조작해야 한다.
        path = register._prepared_payload_path(pkey)
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["version"] = 99
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(ValueError, match="version 불일치"):
            register.load_prepared_payload(product_key=pkey)


# --------------------------------------------------------------------------- #
# 위임 왕복 반례.
# --------------------------------------------------------------------------- #
class TestSubmitReviews:
    """submit_reviews 신뢰 모델 검증."""

    def _make_payload_with_qa(self, pkey, agents_dict):
        """주어진 agent 결과로 QA 집계를 만들어 payload 저장."""
        agent_rows = []
        for agent_name, verdict in agents_dict.items():
            agent_rows.append(
                qa_agents._qa_agent_result(
                    agent_name,
                    verdict,
                    [
                        {
                            "rule": "서버위반",
                            "severity": verdict,
                            "detail": f"서버가 {agent_name}={verdict} 산출",
                        }
                    ],
                    f"{agent_name} {verdict}",
                )
            )
        qa = qa_agents.aggregate_qa_results(agent_rows)
        payload = {
            "product_key": pkey,
            "version": common.PREPARED_PAYLOAD_VERSION,
            "images": {"listing_urls": ["http://x.png"], "detail_urls": []},
            "detail_html": "<html></html>",
            "qa": qa,
            "needs_llm": [],
            "needs_user": [],
        }
        register.write_prepared_payload(payload)
        return payload

    def test_pending_to_pass_allows_gate(self, isolated_prepared_dir):
        """① image=PENDING → submit_reviews(image=PASS) → 게이트 통과."""
        pkey = register.make_product_key("펜딩테스트", 10000)
        self._make_payload_with_qa(pkey, {"image": "PENDING", "copy": "PASS"})
        # PENDING 상태에서는 게이트 차단.
        payload_before = register.load_prepared_payload(product_key=pkey)
        allowed_before, _ = qa_agents.qa_gate(payload_before)
        assert allowed_before is False

        # submit_reviews 로 image PENDING → PASS.
        result = register.submit_reviews(
            pkey,
            [
                {"agent": "image", "verdict": "PASS"},
            ],
        )
        # 집계에 PENDING 이 없어야 함.
        agent_verdicts = [
            qa_agents._clamp_verdict(r.get("verdict")) for r in (result.get("agents") or [])
        ]
        assert "PENDING" not in agent_verdicts, f"PENDING 이 남아있음: {agent_verdicts}"

    def test_server_fail_stays_fail_on_client_pass(self, isolated_prepared_dir):
        """② 서버 copy=FAIL 인데 클라이언트 copy=PASS → 여전히 FAIL."""
        pkey = register.make_product_key("fail보존", 10000)
        self._make_payload_with_qa(pkey, {"image": "PASS", "copy": "FAIL"})
        result = register.submit_reviews(
            pkey,
            [
                {"agent": "copy", "verdict": "PASS"},
            ],
        )
        # FAIL 이 보존되어야 함.
        copy_verdict = None
        for row in result.get("agents") or []:
            if row.get("agent") == "copy":
                copy_verdict = qa_agents._clamp_verdict(row.get("verdict"))
        assert copy_verdict == "FAIL", f"서버 FAIL 이 클라이언트 PASS 로 덮어씌워짐: {copy_verdict}"
        # 서버 violations 보존.
        all_violations = result.get("violations") or []
        has_server_violation = any(
            "서버위반" in str(v.get("detail") or "") or "서버위반" in str(v.get("rule") or "")
            for v in all_violations
            if isinstance(v, dict)
        )
        assert has_server_violation, f"서버 violations 이 삭제됨: {all_violations}"

    def test_compliance_submission_rejected(self, isolated_prepared_dir):
        """③ agent=compliance 제출 → ValueError."""
        pkey = register.make_product_key("컴플라이언스거부", 10000)
        self._make_payload_with_qa(pkey, {"image": "PASS", "copy": "PASS"})
        with pytest.raises(ValueError, match="제출 불가 agent"):
            register.submit_reviews(
                pkey,
                [
                    {"agent": "compliance", "verdict": "PASS"},
                ],
            )

    def test_unknown_agent_rejected(self, isolated_prepared_dir):
        """④ 알 수 없는 agent → ValueError."""
        pkey = register.make_product_key("알수없는agent", 10000)
        self._make_payload_with_qa(pkey, {"image": "PASS", "copy": "PASS"})
        with pytest.raises(ValueError, match="제출 불가 agent"):
            register.submit_reviews(
                pkey,
                [
                    {"agent": "unknown_agent", "verdict": "PASS"},
                ],
            )

    def test_unknown_verdict_rejected(self, isolated_prepared_dir):
        """④ 알 수 없는 verdict → ValueError."""
        pkey = register.make_product_key("알수없는verdict", 10000)
        self._make_payload_with_qa(pkey, {"image": "PASS", "copy": "PASS"})
        with pytest.raises(ValueError, match="알 수 없는 verdict"):
            register.submit_reviews(
                pkey,
                [
                    {"agent": "image", "verdict": "GREAT"},
                ],
            )

    def test_missing_verdict_rejected(self, isolated_prepared_dir):
        """④ verdict 누락 → ValueError."""
        pkey = register.make_product_key("verdict누락", 10000)
        self._make_payload_with_qa(pkey, {"image": "PASS", "copy": "PASS"})
        with pytest.raises(ValueError, match="verdict 가 누락"):
            register.submit_reviews(
                pkey,
                [
                    {"agent": "image"},
                ],
            )

    def test_no_reply_stays_pending(self, isolated_prepared_dir):
        """⑤ 회신하지 않으면 PENDING 유지 → 등록 차단."""
        pkey = register.make_product_key("미회신", 10000)
        self._make_payload_with_qa(pkey, {"image": "PENDING", "copy": "PASS"})
        payload = register.load_prepared_payload(product_key=pkey)
        allowed, reason = qa_agents.qa_gate(payload)
        assert allowed is False
        assert "PENDING" in reason or "차단" in reason


# --------------------------------------------------------------------------- #
# 우회 차단: register_product 가 prepared QA 게이트를 적용.
# --------------------------------------------------------------------------- #
class TestBypassBlocking:
    """prepare 에서 막힌 상품을 register_product 로 직접 호출 → 차단."""

    def test_pending_prepared_blocks_register_product(self, isolated_prepared_dir, monkeypatch):
        """product_key 가 동일한 prepared payload 가 PENDING 이면 차단."""
        name = "우회차단상품"
        price = 15000
        pkey = register.make_product_key(name, price)
        # PENDING 상태 payload 저장.
        agent_rows = [
            qa_agents._qa_agent_result(
                "image",
                "PENDING",
                [{"rule": "대기", "severity": "PENDING", "detail": "육안 확인 필요"}],
                "PENDING",
            ),
            qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
        ]
        qa = qa_agents.aggregate_qa_results(agent_rows)
        payload = {
            "product_key": pkey,
            "version": common.PREPARED_PAYLOAD_VERSION,
            "images": {"listing_urls": ["http://x.png"], "detail_urls": []},
            "detail_html": "<html></html>",
            "qa": qa,
            "needs_llm": [],
            "needs_user": [],
        }
        register.write_prepared_payload(payload)

        # naver 호출 추적.
        naver_calls = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {}),
        )

        # COMMERCE_DRY_RUN 이 아닌 상태에서 register_product 직접 호출.
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        # 결정론 compliance 게이트는 이 테스트의 대상이 아니므로 통과시킨다.
        # (prepared_qa_gate 차단을 관찰하기 위함.)
        monkeypatch.setattr(
            mcp_server,
            "_run_compliance_gate",
            lambda *a, **kw: {
                "blocked": False,
                "violations": [],
                "needs_user": [],
                "pending_reviews": [],
            },
        )
        # _notice_config / _kc_config 를 mock 하여 config 파일 의존을 제거한다.
        # CI 환경(config.example.json)에서는 origin_area_code 가 플레이스홀더라
        # build_payload 단계에서 ValueError 가 발생하기 때문이다. 이 테스트의
        # 대상은 prepared_qa_gate 우회 차단이지 원산지 검사가 아니다.
        monkeypatch.setattr(
            naver_client,
            "_notice_config",
            lambda: {
                "origin_area_code": "04",
                "origin_content": "중국",
                "as_tel": "070-1234-5678",
                "manufacturer": "테스트제조사",
            },
        )
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
        result = mcp_server.register_product(
            name=name,
            price=price,
            image_urls=["http://x.png"],
            category_id="50002366",
            detail_html="<html></html>",
        )
        assert result["ok"] is False
        # 네이버 API 가 호출되지 않아야 한다.
        assert len(naver_calls) == 0, f"네이버 API 가 호출됨(우회 허용): {len(naver_calls)}회"
        # prepared_qa_gate 로 차단되었는가.
        assert (
            result.get("blocked_by") == "prepared_qa_gate"
        ), f"prepared_qa_gate 가 아닌 다른 원인으로 차단됨: {result.get('blocked_by')}"

    def test_no_prepared_marks_deterministic_only(self, isolated_prepared_dir, monkeypatch):
        """prepared 가 없는 신규 호출은 gate=deterministic_only."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        # DRY_RUN 이 아니지만 compliance gate 를 통과할 수 있도록 설정.
        notice_override = {
            "productInfoProvidedNoticeType": "WEAR",
            "etc": {
                "material": "면 100%",
                "color": "블랙",
                "size": "FREE",
                "caution": "물세탁",
                "packDateText": "2024-01-01",
                "warrantyPolicy": "7일교환",
            },
        }
        with (
            mock.patch.object(
                naver_client,
                "_notice_config",
                return_value={
                    "origin_area_code": "04",
                    "origin_content": "중국",
                    "as_tel": "070-1234-5678",
                    "manufacturer": "테스트제조사",
                },
            ),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        ):
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
                    naver_client, "register_product", return_value=(200, {"originProductNo": "x"})
                ):
                    result = mcp_server.register_product(
                        name="신규상품결정론만",
                        price=99999,
                        image_urls=["http://x.png"],
                        category_id="50021299",
                        detail_html="<html></html>",
                        notice=notice_override,
                    )
        assert result.get("gate") == "deterministic_only"


# --------------------------------------------------------------------------- #
# 도구 6개 등록.
# --------------------------------------------------------------------------- #
class TestSixTools:
    """MCP 서버가 정확히 6개 도구를 등록했는가."""

    def test_six_tools_registered(self):
        import asyncio

        tools = mcp_server.mcp.list_tools()
        if hasattr(tools, "__await__"):
            try:
                tools = asyncio.run(tools)
            except RuntimeError:
                tools = asyncio.get_event_loop().run_until_complete(tools)
        assert len(tools) == 6, f"도구가 6개여야 함: {len(tools)}"

    def test_tool_names(self):
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


# --------------------------------------------------------------------------- #
# detail_render 기본 검증.
# --------------------------------------------------------------------------- #
class TestDetailRender:
    """detail_render.render_detail_html 기본 동작."""

    def test_returns_html_document(self):
        html = detail_render.render_detail_html(
            {"name": "테스트", "summary": "요약"},
            ["http://cdn/a.png"],
            [],
        )
        assert "<!DOCTYPE html>" in html
        assert "detail-wrap" in html

    def test_empty_images_still_returns_doc(self):
        """이미지가 없어도 뼈대 문서를 반환한다."""
        html = detail_render.render_detail_html({"name": "테스트"}, [], [])
        assert "<!DOCTYPE html>" in html

    def test_needs_llm_for_copy_returns_descriptor(self):
        hint = detail_render.needs_llm_for_copy({"name": "테스트"})
        assert isinstance(hint, dict)
        assert hint.get("needs_llm") is True

    def test_needs_llm_for_copy_no_name(self):
        hint = detail_render.needs_llm_for_copy({})
        assert hint is None


# --------------------------------------------------------------------------- #
# inject_prepared_qa 봉인 검증.
# --------------------------------------------------------------------------- #
class TestInjectSealed:
    """inject_prepared_qa 가 submit_reviews 와 동일 검증 경로를 공유하는가."""

    def test_inject_rejects_compliance(self, isolated_prepared_dir):
        pkey = register.make_product_key("봉인테스트", 10000)
        agent_rows = [
            qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
            qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
        ]
        qa = qa_agents.aggregate_qa_results(agent_rows)
        register.write_prepared_payload(
            {
                "product_key": pkey,
                "version": common.PREPARED_PAYLOAD_VERSION,
                "images": {"listing_urls": ["http://x.png"], "detail_urls": []},
                "detail_html": "<html></html>",
                "qa": qa,
            }
        )
        with pytest.raises(ValueError, match="제출 불가 agent"):
            register.inject_prepared_qa(
                {
                    "product_key": pkey,
                    "reviews": [{"agent": "compliance", "verdict": "PASS"}],
                }
            )

    def test_inject_rejects_bad_verdict(self, isolated_prepared_dir):
        pkey = register.make_product_key("봉인verdict", 10000)
        agent_rows = [
            qa_agents._qa_agent_result("image", "PASS", [], "PASS"),
            qa_agents._qa_agent_result("copy", "PASS", [], "PASS"),
        ]
        qa = qa_agents.aggregate_qa_results(agent_rows)
        register.write_prepared_payload(
            {
                "product_key": pkey,
                "version": common.PREPARED_PAYLOAD_VERSION,
                "images": {"listing_urls": ["http://x.png"], "detail_urls": []},
                "detail_html": "<html></html>",
                "qa": qa,
            }
        )
        with pytest.raises(ValueError, match="알 수 없는 verdict"):
            register.inject_prepared_qa(
                {
                    "product_key": pkey,
                    "reviews": [{"agent": "image", "verdict": "BAD"}],
                }
            )
