# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""COMMERCE_DRY_RUN 모드에서 컴플라이언스 게이트가 동일하게 실행되는지 검증.

회귀 대상: 과거에는 ``COMMERCE_DRY_RUN=1`` 일 때 ``register_product`` 가
컴플라이언스 게이트를 ``blocked=False`` 인 stand-in 객체로 교체했다. 이로 인해
리허설 모드에서는 비컴플라이언스 상품이 "성공" 으로 보고되고, 실제 경로에서는
거부되는 모순이 생겼다.

본 테스트 파일은 다음 네 가지를 검증한다:

(a) Dry-run 에서 게이트가 위반을 발견하면 실제 경로와 동일하게 차단한다.
(b) ``dry_run: true`` 가 DRY_RUN 모드의 모든 반환 경로(성공/차단/실패) 에 있다.
(c) ``dry_run: false`` 가 DRY_RUN 을 끈 상태의 반환 경로에 있다(패리티).
(d) Dry-run 에서도 prepared QA 게이트가 실행되어 PENDING prepared 를 차단한다.

이 테스트들은 게이트 우회 회귀를 잡기 위한 용도이므로, 게이트 stub 은
사용하지 않고 진짜 게이트를 통과/차단 시키는 페이로드를 제공한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import common, mcp_server, naver_client, qa_agents, register


# --------------------------------------------------------------------------- #
# 공통 픽스처 / 헬퍼.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """``common.PREPARED_DIR`` 을 tmp_path 로 격리.

    ``register_product`` MCP 도구는 내부적으로 prepared payload 를 조회한다.
    실제 ``.local/prepared`` 디렉토리에 잔존하는 payload 가 있으면 테스트가
    서로 간섭한다. 테스트마다 임시 디렉토리로 돌린다.
    """
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


# _notice_config mock — 원산지/AS/제조사 + WEAR 공통 5필드.
_NOTICE_MOCK = {
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

# common.cfg() mock — _compliance_code_check 가 origin 매칭을 위해 직접 읽는다.
_COMMON_CFG_MOCK = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}


def _make_compliant_payload() -> dict:
    """컴플라이언스 게이트를 진심으로 통과하는 WEAR 페이로드를 반환.

    게이트 stub 을 쓰지 않고 진짜 ``_run_compliance_gate`` 를 통과하려면,
    페이로드가 아래 경로에 비어있지 않은 값을 갖춰야 한다:
      - ``originProduct.images.representativeImage.url``
      - ``originProduct.detailAttribute.productInfoProvidedNotice.WEAR.*`` (13 필드)
      - ``originProduct.detailAttribute.originAreaInfo.content``
      - ``originProduct.detailAttribute.afterServiceInfo.afterServiceTelephoneNumber``
    """
    wear_body = {
        "material": "면 100%",
        "color": "블랙",
        "size": "FREE",
        "manufacturer": "테스트제조사",
        "caution": "물 세탁 가능",
        "packDateText": "2026-01",
        "warrantyPolicy": "구매 후 7일 교환 가능",
        "afterServiceDirector": "테스트제조사 070-1234-5678",
        "returnCostReason": "단순변심 반품비용 구매자부담",
        "noRefundReason": "주문제작 청약철회 제한",
        "qualityAssuranceStandard": "관련법에 따름",
        "compensationProcedure": "소비자분쟁해결기준",
        "troubleShootingContents": "고객센터 문의",
    }
    return {
        "originProduct": {
            "originProductNo": "test-no",
            "images": {
                "representativeImage": {"url": "http://cdn/test/representative.png"},
            },
            "deliveryInfo": {"deliveryCompany": "HKSTRANS"},
            "detailAttribute": {
                "productInfoProvidedNotice": {
                    "productInfoProvidedNoticeType": "WEAR",
                    "wear": wear_body,
                },
                "originAreaInfo": {
                    "originAreaCode": "04",
                    "content": "중국",
                },
                "afterServiceInfo": {
                    "afterServiceTelephoneNumber": "070-1234-5678",
                },
            },
        },
    }


def _make_noncompliant_payload_empty_image() -> dict:
    """대표 이미지 URL 이 비어있는 페이로드 — 게이트가 반드시 차단해야 한다."""
    payload = _make_compliant_payload()
    payload["originProduct"]["images"]["representativeImage"]["url"] = ""
    return payload


def _dry_run_naver_register(payload):
    """DRY_RUN 모드의 ``naver_client.register_product`` 대체."""
    return {"ok": True, "originProductNo": "test-no"}


def _apply_common_mocks(monkeypatch):
    """게이트가 config 의 placeholder 값에 부딪히지 않도록 공통 mock 을 적용."""
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)


# =========================================================================== #
# (a) Dry-run 에서 게이트 위반 발견 시 실제 경로와 동일하게 차단.
# =========================================================================== #
class TestDryRunGateBlocksWhenRealPathBlocks:
    """리허설 모드에서도 컴플라이언스 위반을 차단하는가."""

    def test_dry_run_blocks_when_compliance_gate_fails(self, isolated_prepared_dir, monkeypatch):
        """게이트가 차단하는 페이로드는 DRY_RUN 에서도 ok=False 여야 한다.

        회귀: 과거에는 DRY_RUN 이 게이트를 stand-in 으로 교체해 이 케이스가
        ok=True 로 통과했다. 본 테스트는 빈 대표 이미지 URL 페이로드로
        게이트를 진심으로 통과시켜 보고, 차단되는지 확인한다.
        """
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _apply_common_mocks(monkeypatch)
        # 빈 대표 이미지 URL 페이로드를 반환하는 build_payload.
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: _make_noncompliant_payload_empty_image(),
        )

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or _dry_run_naver_register(payload),
        )

        result = mcp_server.register_product(
            name="게이트차단상품",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        # 게이트가 dry-run 에서도 차단해야 한다.
        assert (
            result["ok"] is False
        ), f"DRY_RUN 이 게이트 위반을 통과시킴(bug 회귀): {result.get('blocked_by')}"
        assert (
            result.get("blocked_by") == "compliance"
        ), f"compliance 차단이 아님: {result.get('blocked_by')}"
        # 네이버 API 는 호출되지 않아야 한다(리허설이라도 송신 0회).
        assert (
            len(naver_calls) == 0
        ), f"게이트 차단인데 naver_client.register_product 호출됨: {len(naver_calls)}회"
        # dry_run 마커도 차단 경로에 있어야 한다.
        assert result.get("dry_run") is True


# =========================================================================== #
# (b) dry_run: true 가 DRY_RUN 모드의 모든 반환 경로에 있다.
# =========================================================================== #
class TestDryRunMarkerOnAllReturnPaths:
    """``dry_run: true`` 가 DRY_RUN 모드의 모든 반환 경로에 존재하는가."""

    def test_dry_run_marker_true_on_success(self, isolated_prepared_dir, monkeypatch):
        """성공 경로에 ``dry_run: true`` 가 있다."""
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload()
        )
        monkeypatch.setattr(naver_client, "register_product", _dry_run_naver_register)

        result = mcp_server.register_product(
            name="성공경로",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True
        assert result.get("dry_run") is True, "성공 경로에 dry_run: true 없음"

    def test_dry_run_marker_true_on_compliance_block(self, isolated_prepared_dir, monkeypatch):
        """컴플라이언스 차단 경로에 ``dry_run: true`` 가 있다."""
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: _make_noncompliant_payload_empty_image(),
        )

        result = mcp_server.register_product(
            name="차단경로",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is False
        assert result.get("blocked_by") == "compliance"
        assert result.get("dry_run") is True, "차단 경로에 dry_run: true 없음"

    def test_dry_run_marker_true_on_early_validation_fail(self, monkeypatch):
        """가장 이른 검증 실패(name 빈 문자열)에 ``dry_run: true`` 가 있다."""
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        result = mcp_server.register_product(
            name="",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is False
        assert result.get("dry_run") is True, "초기 검증 실패에 dry_run: true 없음"

    def test_dry_run_marker_true_on_build_failure(self, isolated_prepared_dir, monkeypatch):
        """build_payload 예외 경로에 ``dry_run: true`` 가 있다."""
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _apply_common_mocks(monkeypatch)

        def _boom(*a, **kw):
            raise RuntimeError("build boom")

        monkeypatch.setattr(naver_client, "build_payload", _boom)

        result = mcp_server.register_product(
            name="빌드실패",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is False
        assert result.get("dry_run") is True, "빌드 실패 경로에 dry_run: true 없음"


# =========================================================================== #
# (c) dry_run: false 가 DRY_RUN 끈 상태의 반환 경로에 있다(패리티).
# =========================================================================== #
class TestDryRunMarkerFalseWhenUnset:
    """``COMMERCE_DRY_RUN`` 을 끄면 ``dry_run: false`` 가 반환에 있다."""

    def test_dry_run_marker_false_on_success(self, isolated_prepared_dir, monkeypatch):
        """DRY_RUN 을 끈 성공 경로에 ``dry_run: false`` 가 있다."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload()
        )
        captured: list = []

        def _fake_post(payload, tk, **kwargs):
            captured.append(payload)
            return 200, {"originProductNo": "TEST-1"}

        monkeypatch.setattr(naver_client, "get_token", lambda: "t")

        # _post_product_payload 가 (status_code, body) 튜플을 반환하도록 래핑.
        def _post_wrapper(payload, tk, **kwargs):
            return _fake_post(payload, tk)

        monkeypatch.setattr(naver_client, "_post_product_payload", _post_wrapper)

        result = mcp_server.register_product(
            name="실제등록성공",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True
        assert result.get("dry_run") is False, "DRY_RUN 꺼짐인데 dry_run: false 없음"
        assert len(captured) == 1, "실제 등록은 정확히 1회 송신"

    def test_dry_run_marker_false_on_early_validation_fail(self):
        """초기 검증 실패(name 빈 문자열)에 ``dry_run: false`` 가 있다."""
        # COMMERCE_DRY_RUN 을 명시적으로 설정하지 않는다(기본값 False).
        # 환경변수가 테스트 프로세스에 남아있을 수 있으므로 delenv 로 확실히 지운다.
        import os

        old = os.environ.pop("COMMERCE_DRY_RUN", None)
        try:
            result = mcp_server.register_product(
                name="",
                price=10000,
                category_id="50021299",
                image_urls=["http://cdn/x.png"],
                detail_html="<html></html>",
                preview_confirmed=True,
            )
        finally:
            if old is not None:
                os.environ["COMMERCE_DRY_RUN"] = old
        assert result["ok"] is False
        assert result.get("dry_run") is False, "DRY_RUN 꺼짐인데 dry_run: false 없음"


# =========================================================================== #
# (d) Dry-run 에서도 prepared QA 게이트가 실행되어 PENDING prepared 를 차단.
# =========================================================================== #
class TestDryRunPreparedQaGate:
    """DRY_RUN 에서도 prepared QA 게이트가 동작하는가."""

    def test_dry_run_blocks_on_prepared_qa_pending(self, isolated_prepared_dir, monkeypatch):
        """PENDING 상태의 prepared payload 는 DRY_RUN 에서도 차단된다.

        회귀: 과거에는 ``if not _dry_run:`` 가드가 prepared QA 게이트를
        감싸고 있어서, DRY_RUN 에서는 PENDING prepared 가 있어도 통과했다.
        본 테스트는 PENDING 상태의 prepared 를 디스크에 두고, DRY_RUN 모드에서
        등록이 차단되는지 확인한다.
        """
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _apply_common_mocks(monkeypatch)

        name = "펜딩프리퍼드드라이런"
        price = 42000
        pkey = register.make_product_key(name, price)

        # PENDING 상태의 QA 결과를 가진 prepared payload 작성.
        # image/copy QA 가 verdict 없으면 _normalize_agent_result 가 PENDING 처리한다.
        pending_agents = [
            {"agent": "image"},  # verdict 누락 → PENDING
            {"agent": "copy"},  # verdict 누락 → PENDING
            qa_agents._qa_agent_result("compliance", "PASS", [], "PASS"),
        ]
        qa = qa_agents.aggregate_qa_results(pending_agents)
        prepared_payload = {
            "product_key": pkey,
            "version": common.PREPARED_PAYLOAD_VERSION,
            "product": {"name": name, "salePrice": price},
            "images": {
                "listing_urls": ["http://cdn/pending.png"],
                "detail_urls": [],
            },
            "detail_html": "<html>pending prepared</html>",
            "qa": qa,
            "needs_llm": [],
            "needs_user": [],
        }
        register.write_prepared_payload(prepared_payload)

        monkeypatch.setattr(
            naver_client, "build_payload", lambda *a, **kw: _make_compliant_payload()
        )
        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or _dry_run_naver_register(payload),
        )

        # 명시적으로 image_urls 와 detail_html 을 주어 prepared 의 값 채움
        # 로직을 건너뛰더라도, product_key 가 유도되어 prepared_qa_gate 가 실행된다.
        result = mcp_server.register_product(
            name=name,
            price=price,
            category_id="50021299",
            image_urls=["http://cdn/explicit.png"],
            detail_html="<html>explicit</html>",
            preview_confirmed=True,
        )
        # PENDING prepared 는 DRY_RUN 에서도 차단되어야 한다.
        assert result["ok"] is False, (
            f"DRY_RUN 이 PENDING prepared 를 통과시킴(bug 회귀): " f"{result.get('blocked_by')}"
        )
        assert (
            result.get("blocked_by") == "prepared_qa_gate"
        ), f"prepared_qa_gate 차단이 아님: {result.get('blocked_by')}"
        assert (
            len(naver_calls) == 0
        ), f"prepared_qa_gate 차단인데 naver 호출됨: {len(naver_calls)}회"
        assert result.get("dry_run") is True, "prepared_qa_gate 차단에 dry_run: true 없음"
