# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""조용한 실패/거짓 게이트 결함 3종 검증.

본 테스트는 "검증하지 않은 것을 검증됐다고 말하지 않는다" 는 원칙의
세 가지 회귀를 검증한다:

결함 1 — 승인 편집 필드가 QA 를 무력화한다:
  (a) 승인 편집으로 상품명이 금지 표현으로 바뀌면 → 등록 차단 (네이버 호출 0회).
  (b) 승인 편집으로 고시 필수필드가 누락되면 → 등록 차단.
  (c) 승인 편집으로 태그가 바뀌면 → gate="approval_edited" + unreviewed 명시.
  (d) 승인 편집이 없으면 → gate="full" 유지 (거짓 라벨 금지).

결함 2 — 카테고리 메타 조회 실패가 조용히 ETC 로 강등된다:
  (e) ``_category_path_for`` 가 ``CategoryMetaUnavailableError`` 를 전파한다
      (mcp_server / naver_client / register 3개 모듈).
  (f) ``register_product`` 가 메타 데이터 파일 부재 시 fail-closed 차단.
  (g) ``prepare_listing`` 이 메타 조회 실패를 컴플라이언스 FAIL 로 번역.

결함 3 — 스캐너가 존재하지 않는 경로를 조용히 스킵한다:
  (h) ``_iter_files`` 가 존재하지 않는 경로에서 ``FileNotFoundError``.
  (i) ``SCAN_PATHS`` 에 stale 항목("data")이 없다.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# scripts/ 디렉터리를 import 가능하게 path 에 추가.
_SCRIPTS = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from clossify import (
    approval_server,
    category_meta,
    common,
    mcp_server,
    naver_client,
    qa_agents,
    register,
)
from clossify.approval_server import Outcome
from clossify.category_meta import CategoryMetaUnavailableError

# =========================================================================== #
# 공통 픽스처 / 헬퍼.
# =========================================================================== #

# config 의 smartstore_notice_defaults 섹션 — 원산지·AS·공통 5필드 모두 채움.
_NOTICE_CFG_FULL = {
    "origin_area_code": "04",
    "origin_content": "중국",
    "as_tel": "070-1234-5678",
    "manufacturer": "테스트제조사",
    "return_cost_reason": "단순변심 반품비용 구매자부담",
    "no_refund_reason": "주문제작 청약철회 제한",
    "quality_assurance_standard": "관련법에 따름",
    "compensation_procedure": "소비자분쟁해결기준",
    "trouble_shooting_contents": "고객센터 문의",
    "delivery_company": "HKSTRANS",
}


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


def _config_patches():
    """config mock 일괄 생성 (context manager 진입용)."""
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
        mock.patch.object(
            common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
        ),
    )


def _mock_approval_server(edits: dict | None = None, approved: bool = True):
    """ApprovalServer 를 mock 하여 승인 + edits 를 반환하도록 한다.

    ``edits`` 가 None 이면 편집 없이 승인만 한다. ``approved=False`` 면 거부.
    """
    outcome = Outcome(
        approved=approved,
        decisions={"edits": edits} if edits else {},
    )

    class _FakeSrv:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            return 0  # 사용하지 않는 포트.

        def wait(self, timeout=None):
            return outcome

        def close(self):
            pass

    return _FakeSrv


# =========================================================================== #
# 결함 1 — 승인 편집 필드에 대한 결정론 재검사.
# =========================================================================== #


def _make_passing_prepared(name: str, price: int = 10000, category_id: str = "50002366"):
    """QA 게이트를 통과하는(PASS/WARN) prepared payload 를 만든다.

    approval bridge 경로는 prepared payload 가 있어야 진입한다. prepared QA
    게이트(qa_gate) 가 PENDING/FAIL 로 차단하지 않도록, 세 에이전트 결과를
    모두 PASS 로 채운다. 코드 검사(금지어·필수필드)는 테스트 시나리오에 따라
    별도로 mock 한다.
    """
    passing_qa = {
        "verdict": "PASS",
        "agents": [
            {"agent": "image", "verdict": "PASS", "violations": [], "summary": "ok"},
            {"agent": "copy", "verdict": "PASS", "violations": [], "summary": "ok"},
            {"agent": "compliance", "verdict": "PASS", "violations": [], "summary": "ok"},
        ],
        "violations": [],
    }
    return {
        "product_key": "testkey123",
        "product": {
            "name": name,
            "categoryId": category_id,
            "salePrice": price,
            "options": [],
            "tags": [],
            "notice": {},
            "origin_code": "",
            "manufacturer": "",
            "importer": "",
            "as_tel": "070-1234-5678",
            "as_guide": "",
            "courier": "HKSTRANS",
            "delivery_fee": 3000,
            "option_groups": [],
        },
        "images": {
            "listing_urls": ["http://cdn.example/img.png"],
            "detail_urls": ["http://cdn.example/img.png"],
        },
        "detail_html": "<html><body>detail</body></html>",
        "qa": passing_qa,
        "needs_llm": [],
        "needs_user": [],
        "status": "SALE",
        "preview_path": "/tmp/fake_preview.html",
        "version": common.PREPARED_PAYLOAD_VERSION,
    }


class TestApprovalEditRecheck:
    """승인 편집으로 바뀐 필드가 결정론 재검사를 통과/차단하는가."""

    def test_a_edited_name_with_banned_claim_blocks(self, monkeypatch, isolated_prepared_dir):
        """(a) 승인 편집으로 상품명이 금지 표현으로 바뀌면 등록 차단.

        회귀 시나리오: QA 가 "정상이름" 으로 PASS 했는데, 승인 편집에서
        "최고급" 같은 금지 표현으로 바꾸면 — 과거에는 그대로 전송되었다.
        개정 후에는 카피 코드검사(금지어)가 편집된 이름으로 재실행되어
        차단한다(fail-closed).
        """
        # 승인 편집으로 상품명을 금지 표현으로 바꾼다.
        # _copy_code_check 가 잡는 금지 표현을 사용한다.
        bad_name = "최고급 특허받은 의약품"  # "최고급" 등 금지 표현.
        fake_srv = _mock_approval_server(edits={"상품명": bad_name})
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _count_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {"originProductNo": "X"}

        passing_payload = _make_passing_prepared("정상이름")
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(mcp_server, "_config_enable_local_approval", return_value=True),
            mock.patch.object(
                mcp_server, "_config_require_preview_confirmation", return_value=True
            ),
            mock.patch.object(approval_server, "ApprovalServer", fake_srv),
            mock.patch.object(
                mcp_server,
                "_run_compliance_gate",
                return_value={
                    "blocked": False,
                    "violations": [],
                    "needs_user": [],
                    "pending_reviews": [],
                },
            ),
            mock.patch.object(register, "load_prepared_payload", return_value=passing_payload),
            mock.patch.object(
                register, "resolve_prepared_for_register", return_value=(passing_payload, {})
            ),
            mock.patch.object(naver_client, "_post_product_payload", side_effect=_count_post),
            mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ),
        ):
            result = mcp_server.register_product(
                name="정상이름",
                price=10000,
                category_id="50002366",
                preview_confirmed=False,  # approval bridge 경로 유도.
            )

        # 금지 표현 위반으로 차단되어야 한다.
        assert result["ok"] is False, f"승인 편집된 금지 표현이 통과함: {result.get('blocked_by')}"
        assert result.get("blocked_by") == "approval_edit_copy"
        assert http_calls["count"] == 0, "네이버 호출이 0회여야 함(차단)"
        assert result.get("gate") == "approval_edited"

    def test_b_edited_notice_missing_required_blocks(self, monkeypatch, isolated_prepared_dir):
        """(b) 승인 편집으로 고시 필수필드가 누락되면 등록 차단.

        회귀 시나리오: QA 가 통과한 notice 에서 승인 편집으로 필수 필드를
        빈 값으로 바꾸면 — 과거에는 그대로 전송되었다.
        """
        # WEAR 타입 필수 필드를 빈 문자열로 바꾼다.
        bad_edits = {
            "고시.returnCostReason": "",
        }
        fake_srv = _mock_approval_server(edits=bad_edits)
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _count_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {"originProductNo": "X"}

        # passing_payload 의 notice 를 WEAR 로 설정하여 tripwire(payload_type
        # vs gate_type 불일치) 없이 첫 게이트를 통과하게 한다.
        passing_payload = _make_passing_prepared("WEAR테스트상품")
        passing_payload["product"]["notice"] = {
            "productInfoProvidedNoticeType": "WEAR",
            "wear": {
                "returnCostReason": "왕복 배송비 6000원",
                "noRefundReason": "주문제작 청약철회 제한",
                "qualityAssuranceStandard": "관련법에 따름",
                "compensationProcedure": "소비자분쟁해결기준",
                "troubleShootingContents": "고객센터 문의",
                "certificationType": "0",
                "color": "블랙",
                "size": "95",
                "manufacturer": "테스트제조사",
                "importer": "테스트수입사",
                "afterServiceDirector": "테스트제조사 070-1234-5678",
                "countryOfOrigin": "중국",
                "material": "면 100%",
            },
        }

        # _run_compliance_gate: 첫 호출(원 게이트) 은 PASS, 두 번째 호출(재검사)
        # 은 FAIL. 재검사가 편집된 빈 필드를 잡는 시나리오.
        gate_pass = {
            "blocked": False,
            "violations": [],
            "needs_user": [],
            "pending_reviews": [],
        }
        gate_fail = {
            "blocked": True,
            "violations": [{"rule": "고시 필수필드", "detail": "returnCostReason 누락(승인 편집)"}],
            "needs_user": [],
            "pending_reviews": [],
        }

        # 트립와이어 우회: build_payload 가 WEAR 타입을 그대로 싣도록 mock.
        # product.notice 의 WEAR 타입이 payload 의 notice 타입과 일치하게 한다.
        wear_payload = {
            "originProduct": {
                "detailAttribute": {
                    "productInfoProvidedNotice": {
                        "productInfoProvidedNoticeType": "WEAR",
                        "wear": passing_payload["product"]["notice"]["wear"],
                    }
                }
            }
        }

        with (
            mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(mcp_server, "_config_enable_local_approval", return_value=True),
            mock.patch.object(
                mcp_server, "_config_require_preview_confirmation", return_value=True
            ),
            mock.patch.object(approval_server, "ApprovalServer", fake_srv),
            mock.patch.object(
                mcp_server, "_category_path_for", return_value="패션의류/남성의류/티셔츠"
            ),
            mock.patch.object(
                mcp_server, "_run_compliance_gate", side_effect=[gate_pass, gate_fail]
            ),
            mock.patch.object(naver_client, "build_payload", return_value=wear_payload),
            mock.patch.object(register, "load_prepared_payload", return_value=passing_payload),
            mock.patch.object(
                register, "resolve_prepared_for_register", return_value=(passing_payload, {})
            ),
            mock.patch.object(naver_client, "_post_product_payload", side_effect=_count_post),
            mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ),
        ):
            result = mcp_server.register_product(
                name="WEAR테스트상품",
                price=10000,
                category_id="50002366",
                preview_confirmed=False,
            )

        # 빈 필수 필드 위반으로 차단되어야 한다.
        assert (
            result["ok"] is False
        ), f"승인 편집된 빈 필수필드가 통과함: {result.get('blocked_by')}"
        assert result.get("blocked_by") == "approval_edit_compliance"
        assert http_calls["count"] == 0, "네이버 호출이 0회여야 함(차단)"

    def test_c_edited_tags_marks_unreviewed(self, monkeypatch, isolated_prepared_dir):
        """(c) 승인 편집으로 태그가 바뀌면 gate="approval_edited" + unreviewed.

        태그 편집 자체는 결정론 검사 대상이 아니지만(현재 코드에서), LLM 카피
        품질 검사가 재실행되지 않는다는 것을 라벨과 unreviewed 목록으로
        드러내야 한다 — 거짓 "full" 라벨 금지.

        ★ N60 — 본 테스트는 ``_post_product_payload`` 만 mock 했고 ``get_token``
        을 mock 하지 않아, 게이트를 통과한 뒤 등록 직전 ``naver_client.get_token``
        이 네이버 OAuth 토큰 엔드포인트(223.130.196.242:443) 로 실제 POST 를
        보냈다. IP 허용목록이 풀려 있으면 우연히 초록불, 불일치하면 빨간불 —
        테스트 통과가 남의 서버 상태에 좌우되는 사고(2026-08-08) 의 원인이었다.
        이제 ``get_token`` 도 mock 하여 네이버 없이도 통과한다.
        """
        fake_srv = _mock_approval_server(edits={"태그": "겨울, 신상품"})
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _count_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {"originProductNo": "X"}

        passing_payload = _make_passing_prepared("태그편집테스트")
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(mcp_server, "_config_enable_local_approval", return_value=True),
            mock.patch.object(
                mcp_server, "_config_require_preview_confirmation", return_value=True
            ),
            mock.patch.object(approval_server, "ApprovalServer", fake_srv),
            mock.patch.object(
                mcp_server,
                "_run_compliance_gate",
                return_value={
                    "blocked": False,
                    "violations": [],
                    "needs_user": [],
                    "pending_reviews": [],
                },
            ),
            mock.patch.object(register, "load_prepared_payload", return_value=passing_payload),
            mock.patch.object(
                register, "resolve_prepared_for_register", return_value=(passing_payload, {})
            ),
            # N60 — 네이버 OAuth 토큰 엔드포인트로의 실호출 차단.
            # 가짜 토큰을 반환한다 (실제 API 형태: access_token 문자열).
            # _post_product_payload 도 mock 했으므로 이 토큰은 쓰이지 않는다.
            mock.patch.object(naver_client, "get_token", return_value="test-token-mock"),
            mock.patch.object(naver_client, "_post_product_payload", side_effect=_count_post),
            mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ),
        ):
            result = mcp_server.register_product(
                name="태그편집테스트",
                price=10000,
                category_id="50002366",
                preview_confirmed=False,
            )

        # 태그 편집은 결정론 검사 대상이 아니므로 등록은 진행될 수 있다.
        # 하지만 gate 라벨이 "approval_edited" 여야 하고, unreviewed 에 태그가 있어야 한다.
        assert (
            result.get("gate") == "approval_edited"
        ), f"태그 편집 후 gate 가 approval_edited 가 아님: {result.get('gate')}"
        unreviewed = result.get("approval_edits_unreviewed") or []
        assert any(
            "태그" in item for item in unreviewed
        ), f"unreviewed 에 태그 항목이 없음: {unreviewed}"
        assert "tags" in (result.get("approval_edits_applied") or {})

    def test_d_no_edits_preserves_full_gate(self, monkeypatch, isolated_prepared_dir):
        """(d) 승인 편집이 없으면 gate="full" 유지.

        회귀 방지: 편집이 없을 때도 라벨이 "approval_edited" 로 깎이면
        정상 경로의 신뢰성 표시가 깨진다.

        ★ N60 — test_c 와 같은 이유로 ``get_token`` 이 네이버 OAuth 엔드포인트로
        실호출했다. ``_post_product_payload`` 옆에 ``get_token`` mock 을 추가하여
        네이버 없이도 통과한다 (IP 허용목록·서버 상태와 무관).
        """
        fake_srv = _mock_approval_server(edits=None)  # 편집 없음.
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _count_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {"originProductNo": "X"}

        passing_payload = _make_passing_prepared("편집없음테스트")
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(mcp_server, "_config_enable_local_approval", return_value=True),
            mock.patch.object(
                mcp_server, "_config_require_preview_confirmation", return_value=True
            ),
            mock.patch.object(approval_server, "ApprovalServer", fake_srv),
            mock.patch.object(
                mcp_server,
                "_run_compliance_gate",
                return_value={
                    "blocked": False,
                    "violations": [],
                    "needs_user": [],
                    "pending_reviews": [],
                },
            ),
            mock.patch.object(register, "load_prepared_payload", return_value=passing_payload),
            mock.patch.object(
                register, "resolve_prepared_for_register", return_value=(passing_payload, {})
            ),
            # N60 — 네이버 OAuth 토큰 엔드포인트로의 실호출 차단.
            mock.patch.object(naver_client, "get_token", return_value="test-token-mock"),
            mock.patch.object(naver_client, "_post_product_payload", side_effect=_count_post),
            mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ),
        ):
            result = mcp_server.register_product(
                name="편집없음테스트",
                price=10000,
                category_id="50002366",
                preview_confirmed=False,
            )

        # 편집이 없으므로 gate="full" 이어야 한다.
        assert (
            result.get("gate") == "full"
        ), f"편집이 없는데 gate 가 full 이 아님: {result.get('gate')}"
        assert result.get("approval_edits_applied") == {}
        assert result.get("approval_edits_unreviewed") == []


# =========================================================================== #
# 결함 2 — 카테고리 메타 조회 실패가 조용히 ETC 로 강등된다.
# =========================================================================== #


class TestCategoryMetaFailurePropagation:
    """``CategoryMetaUnavailableError`` 가 조용히 빈 문자열로 강등되지 않는다."""

    def test_e_mcp_server_category_path_for_propagates_error(self, monkeypatch):
        """(e) mcp_server._category_path_for 가 CategoryMetaUnavailableError 를 전파."""
        # load_category_meta 가 실패하도록 강제.
        with mock.patch.object(
            category_meta,
            "category_path",
            side_effect=CategoryMetaUnavailableError("test: meta unavailable"),
        ):
            with pytest.raises(CategoryMetaUnavailableError):
                mcp_server._category_path_for("50002366")

    def test_e_naver_client_category_path_for_propagates_error(self, monkeypatch):
        """(e) naver_client._category_path_for 가 CategoryMetaUnavailableError 를 전파."""
        with mock.patch.object(
            category_meta,
            "category_path",
            side_effect=CategoryMetaUnavailableError("test: meta unavailable"),
        ):
            with pytest.raises(CategoryMetaUnavailableError):
                naver_client._category_path_for("50002366")

    def test_e_register_category_path_for_propagates_error(self, monkeypatch):
        """(e) register._category_path_for 가 CategoryMetaUnavailableError 를 전파."""
        with mock.patch.object(
            category_meta,
            "category_path",
            side_effect=CategoryMetaUnavailableError("test: meta unavailable"),
        ):
            with pytest.raises(CategoryMetaUnavailableError):
                register._category_path_for("50002366")

    def test_f_register_product_fail_closed_on_meta_unavailable(
        self, monkeypatch, isolated_prepared_dir
    ):
        """(f) register_product 가 메타 데이터 파일 부재 시 fail-closed 차단.

        회귀 시나리오: 카테고리 메타 파일이 없거나 깨졌을 때, 과거에는
        ``_category_path_for`` 가 빈 문자열을 반환해 ETC 로 떨어졌고, 게이트가
        ETC 기준으로 통과시켜 잘못된 고시 타입으로 등록되었다.
        개정 후에는 ``CategoryMetaUnavailableError`` 가 전파되어 등록이 거부된다.
        """
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        http_calls = {"count": 0}

        def _count_post(*a, **kw):
            http_calls["count"] += 1
            return 200, {}

        with (
            mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(
                category_meta,
                "category_path",
                side_effect=CategoryMetaUnavailableError("test: meta unavailable"),
            ),
            mock.patch.object(naver_client, "_post_product_payload", side_effect=_count_post),
            mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ),
        ):
            result = mcp_server.register_product(
                name="메타조회실패테스트",
                price=10000,
                image_urls=["http://cdn.example/img.png"],
                category_id="50002366",
                detail_html="<html><body>detail</body></html>",
                preview_confirmed=True,
            )

        # 메타 조회 실패 → fail-closed 차단.
        assert result["ok"] is False, f"메타 조회 실패가 통과함: {result.get('error')}"
        assert http_calls["count"] == 0, "네이버 호출이 0회여야 함(차단)"

    def test_g_prepare_listing_translates_meta_failure_to_compliance_fail(
        self, isolated_prepared_dir, monkeypatch
    ):
        """(g) prepare_listing 이 메타 조회 실패를 컴플라이언스 FAIL 로 번역.

        회귀 시나리오: 과거에는 ``_category_path_for`` 가 빈 문자열을 반환해
        prepare_listing 이 ETC 로 진행되었다. 개정 후에는 예외가 전파되어
        prepare_listing 의 try/except 가 컴플라이언스 FAIL 로 번역한다.
        """
        d = {
            "name": "준비단계메타실패",
            "salePrice": 10000,
            "image_sources": ["a.png"],
            "category_id": "50002366",
        }
        with (
            mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
            mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
            mock.patch.object(
                category_meta,
                "category_path",
                side_effect=CategoryMetaUnavailableError("test: meta unavailable"),
            ),
            mock.patch.object(
                common, "cfg", return_value={"smartstore_notice_defaults": _NOTICE_CFG_FULL}
            ),
        ):
            payload = register.prepare_listing(d, attach_fn=_fake_attach_ok)

        # prepare_listing 자체는 죽지 않고 컴플라이언스 FAIL 로 번역한다.
        assert isinstance(payload, dict)
        qa = payload.get("qa") or {}
        comp_verdict = None
        for row in qa.get("agents") or []:
            if isinstance(row, dict) and row.get("agent") == "compliance":
                comp_verdict = str(row.get("verdict") or "").upper()
                break
        assert (
            comp_verdict == qa_agents.FAIL
        ), f"컴플라이언스가 FAIL 이 아님: {comp_verdict} (메타 조회 실패가 조용히 통과됨)"


# =========================================================================== #
# 결함 3 — 스캐너가 존재하지 않는 경로를 조용히 스킵한다.
# =========================================================================== #


@pytest.fixture
def scanner():
    """scan_repo 모듈을 fresh import 한다 (모듈 전역 상태 격리)."""
    sys.modules.pop("scan_repo", None)
    mod = importlib.import_module("scan_repo")
    importlib.reload(mod)
    return mod


class TestScannerNoSilentSkip:
    """``_iter_files`` 가 존재하지 않는 경로를 에러로 알리는가."""

    def test_h_iter_files_errors_on_nonexistent_path(self, scanner, tmp_path, monkeypatch):
        """(h) 존재하지 않는 경로 → FileNotFoundError.

        회귀 시나리오: 과거에는 ``_iter_files`` 가 존재하지 않는 경로를
        조용히 스킵했다. ``SCAN_PATHS`` 에 stale 항목이 있어도 누가 알 수 없었다.
        개정 후에는 ``FileNotFoundError`` 를 발생시킨다.
        """
        # 존재하는 디렉터리 하나와 존재하지 않는 경로 하나.
        real_dir = tmp_path / "src"
        real_dir.mkdir(parents=True)
        (real_dir / "file.py").write_text("# ok\n", encoding="utf-8")

        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        # 존재하지 않는 경로를 포함.
        paths = ["src", "nonexistent_dir"]

        # 첫 번째 경로는 순회되지만, 두 번째에서 FileNotFoundError.
        with pytest.raises(FileNotFoundError, match="nonexistent_dir"):
            list(scanner._iter_files(paths))

    def test_h_iter_files_succeeds_with_all_existing_paths(self, scanner, tmp_path, monkeypatch):
        """(h) 모든 경로가 존재하면 정상 순회 (회귀 없음)."""
        real_dir = tmp_path / "src"
        real_dir.mkdir(parents=True)
        (real_dir / "a.py").write_text("# a\n", encoding="utf-8")
        real_file = tmp_path / "config.toml"
        real_file.write_text("# config\n", encoding="utf-8")

        monkeypatch.setattr(scanner, "_REPO_ROOT", str(tmp_path))
        paths = ["src", "config.toml"]

        files = list(scanner._iter_files(paths))
        # 두 파일 모두 나와야 한다.
        assert any("a.py" in f for f in files)
        assert any("config.toml" in f for f in files)

    def test_i_scan_paths_has_no_stale_data(self, scanner):
        """(i) SCAN_PATHS 에 stale "data" 항목이 없다.

        회귀 시나리오: 저장소 루트 "data" 디렉터리는 스크립트 생성 산출물이며
        항상 존재하지 않는다. ``_iter_files`` 가 이제 에러를 내므로, stale 항목이
        SCAN_PATHS 에 남아있으면 스캐너 자체가 실패한다.
        """
        assert "data" not in scanner.SCAN_PATHS, (
            "SCAN_PATHS 에 stale 'data' 항목이 남아있다 — _iter_files 가 에러를 내므로 "
            "제거되어야 한다."
        )
        # 핵심 항목은 여전히 있어야 한다.
        assert "src" in scanner.SCAN_PATHS
        assert "scripts" in scanner.SCAN_PATHS
        assert "tests" in scanner.SCAN_PATHS

    def test_i_repo_scan_still_passes(self, scanner, monkeypatch):
        """(i) 실제 저장소를 스캔했을 때 여전히 exit 0 (stale 항목 제거 후).

        ``_iter_files`` 가 이제 에러를 내므로, SCAN_PATHS 의 모든 항목이
        실제로 존재해야 한다. 이 테스트는 stale "data" 제거 후에도
        스캐너가 정상 동작함을 확인한다.
        """
        import io

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = scanner.main()
        out = buf.getvalue()
        if rc != 0:
            pytest.fail(f"스캐너 위반 감지 (stale 제거 후 회귀):\n{out}")
        assert "PASS" in out
