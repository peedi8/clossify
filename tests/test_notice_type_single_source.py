# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""고시 타입 단일 진실 공급원 검증 — 게이트 판정 타입과 페이로드에 실리는
``productInfoProvidedNoticeType`` 이 항상 같아야 한다.

실등록에서 발견된 결함: 게이트(mcp_server 컴플라이언스 컨텍스트)는
``FURNITURE`` 로 검사해 판매자에게 가구 필드 17개를 물었고, 페이로드
(naver_client.build_payload)는 ``ETC`` 로 신고했다. 판매자가 가구 질문에
답하고 통과했는데 실제 신고는 ETC 로 나가는 조용한 잘못 신고.

근본 원인: ``build_payload`` 가 카테고리 경로를 호출자가 넘겨주기를 기대했고,
호출자가 안 넘기니 휴리스틱이 ETC 로 떨어졌다. 게이트는 ``categoryId`` 로
스스로 경로를 조회했으므로 FURNITURE 가 나왔다. 두 판정 지점이 서로 다른
입력을 본 것이다.

본 테스트는:
  (a) ``build_payload`` 에 ``categoryId`` 만 주어도 스스로 경로를 조회해
      ``50001060`` 을 ``FURNITURE`` 로 신고한다.
  (b) 게이트 판정 타입과 페이로드 타입이 여러 카테고리에서 **항상 같다**.
  (c) **트립와이어**: 페이로드 타입을 강제로 다르게 만들면 ``register_product``
      가 등록을 **차단**하고 네이버 HTTP 호출이 0회 발생한다.
  (d) 명시 타입(``notice.productInfoProvidedNoticeType``)이 주어지면 그것이
      양쪽 모두에서 우선한다 (기존 규칙 회귀 방지).
  (e) 미확정/알 수 없는 카테고리에서 예외로 죽지 않는다.

``COMMERCE_DRY_RUN`` 은 끈 상태로, 실제 네이버 HTTP 호출은 mock 으로 차단하고
호출 횟수를 센다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

# src 레이아웃 지원: 프로젝트 루트를 path 에 추가.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import mcp_server, naver_client
from clossify.text_props import CATEGORY_PATH_NOTICE_HINTS

# ============================================================================
# 공통 픽스처·헬퍼.
# ============================================================================

# 실등록 회귀 카테고리 — 가구/DIY자재/용품/목재.
_FURNITURE_CATEGORY = "50001060"

# 원산지·AS·공통 5필드가 모두 채워진 config 로 notice/kc 의존을 끊는다.
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
}


def _patch_cfg():
    """notice/kc config 의존을 끊는 context manager 들을 반환한다."""
    return (
        mock.patch.object(naver_client, "_notice_config", return_value=_NOTICE_CFG_FULL),
        mock.patch.object(naver_client, "_kc_config", return_value=({}, "")),
    )


def _make_product(**overrides) -> dict:
    """build_payload 에 넘길 최소 상품 dict. categoryId 만으로 타입 추론을
    유도하기 위해 category_name/path 는 의도적으로 넣지 않는다."""
    base = {
        "name": "테스트상품",
        "categoryId": _FURNITURE_CATEGORY,
        "salePrice": 10000,
        "origin_code": "05",
        "made_in": "한국",
    }
    base.update(overrides)
    return base


def _build_payload(p: dict, status: str = "SALE") -> dict:
    """config 의존을 끊고 build_payload 만 실행."""
    with _patch_cfg()[0], _patch_cfg()[1]:
        return naver_client.build_payload(p, "<html></html>", ["http://x/img.png"], status=status)


# ============================================================================
# (a) categoryId 만으로 build_payload 가 FURNITURE 로 신고한다.
#     핵심 회귀: 호출자가 category_path 를 안 넘겨도 build_payload 스스로 조회.
# ============================================================================
class TestBuildPayloadSelfResolvesCategoryPath:
    def test_furniture_from_category_id_only(self):
        """categoryId=50001060 만 주고 경로 키가 전혀 없어도 FURNITURE."""
        p = _make_product()
        # category_name/path/categoryPath 키가 전부 없는 것을 확인.
        assert "category_name" not in p
        assert "category_path" not in p
        assert "categoryPath" not in p

        payload = _build_payload(p)
        notice = payload["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]
        assert notice["productInfoProvidedNoticeType"] == "FURNITURE", (
            "categoryId 만 주어졌을 때 build_payload 가 스스로 경로를 조회해 "
            "FURNITURE 로 신고해야 한다 (과거에는 ETC 로 떨어졌다)."
        )

    def test_furniture_path_known_for_category(self):
        """전제 확인: 50001060 의 경로에 '가구' 가 포함되어 있다."""
        path = naver_client._category_path_for(_FURNITURE_CATEGORY)
        assert "가구" in path, (
            f"카테고리 {_FURNITURE_CATEGORY} 경로에 '가구' 가 없다: {path!r}. "
            "data 파일이 이 ID 를 모르면 본 테스트 묶음의 전제가 성립하지 않는다."
        )


# ============================================================================
# (b) 게이트 판정 타입과 페이로드 타입이 여러 카테고리에서 항상 같다.
#     양쪽이 같은 CATEGORY_PATH_NOTICE_HINTS 정본을 쓰는지 구조적으로 확인.
# ============================================================================
class TestGateAndPayloadAlwaysAgree:
    def test_agree_on_furniture_category(self):
        """50001060 — 양쪽 모두 FURNITURE."""
        p = _make_product()
        payload = _build_payload(p)

        payload_type = mcp_server._payload_notice_type(payload)
        gate_type = mcp_server._gate_notice_type(_FURNITURE_CATEGORY, p)

        assert payload_type == gate_type == "FURNITURE", (
            f"게이트({gate_type!r}) 와 페이로드({payload_type!r}) 가 다르다 "
            "(회귀: 과거에는 게이트=FURNITURE, 페이로드=ETC 였다)."
        )

    def test_agree_on_etc_category(self):
        """카테고리 경로 조회가 빈 문자열로 떨어지면 양쪽 모두 ETC."""
        unknown_id = "00000000"
        p = _make_product(categoryId=unknown_id)
        payload = _build_payload(p)

        payload_type = mcp_server._payload_notice_type(payload)
        gate_type = mcp_server._gate_notice_type(unknown_id, p)

        assert payload_type == gate_type == "ETC"

    def test_hints_table_is_single_shared_object(self):
        """두 판정 지점이 text_props 의 동일 테이블 객체를 참조한다."""
        assert (
            naver_client.CATEGORY_PATH_NOTICE_HINTS is CATEGORY_PATH_NOTICE_HINTS
        ), "naver_client 가 text_props 정본 테이블을 직접 참조하지 않는다"


# ============================================================================
# (c) 트립와이어 — 페이로드 타입을 강제로 다르게 만들면 register_product 가
#     등록을 차단하고 네이버 HTTP 호출이 0회다. 조용히 통과시키지 않는다.
# ============================================================================
class TestNoticeTypeTripwire:
    def test_mismatch_blocks_registration(self, monkeypatch):
        """게이트=FURNITURE, 페이로드=ETC 인 상황을 강제로 만들면 차단."""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)

        # 게이트는 FURNITURE 로 판정하도록 둔다 (categoryId 기반 정상 경로).
        # 페이로드의 noticeType 을 ETC 로 강제 변조해 불일치를 만든다.
        original_build = naver_client.build_payload

        def _tampered_build(product, html, images, status="SALE"):
            payload = original_build(product, html, images, status=status)
            # 페이로드의 타입을 강제로 ETC 로 바꿔 트립와이어를 격발시킨다.
            notice = payload["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]
            notice["productInfoProvidedNoticeType"] = "ETC"
            return payload

        # HTTP 송신이 일어나면 안 된다 — 호출 카운터로 확인.
        post_calls = []
        monkeypatch.setattr(
            naver_client,
            "_post_product_payload",
            lambda payload, tk: post_calls.append(1) or (200, {}),
        )
        monkeypatch.setattr(naver_client, "get_token", lambda: "t")
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

        with mock.patch.object(naver_client, "build_payload", side_effect=_tampered_build):
            result = mcp_server.register_product(
                name="트립와이어테스트",
                price=10000,
                category_id=_FURNITURE_CATEGORY,
                image_urls=["http://x/img.png"],
                detail_html="<html></html>",
            )

        assert result["ok"] is False, "타입 불일치임에도 성공으로 보고하면 안 된다"
        assert (
            result.get("blocked_by") == "notice_type_tripwire"
        ), f"트립와이어로 차단되어야 한다. blocked_by={result.get('blocked_by')!r}"
        assert result.get("gate_notice_type") == "FURNITURE"
        assert result.get("payload_notice_type") == "ETC"
        assert (
            len(post_calls) == 0
        ), "트립와이어 격발 시 네이버 HTTP 호출이 0회여야 한다 (차단 우선)."


# ============================================================================
# (d) 명시 타입이 주어지면 그것이 양쪽 모두에서 우선한다.
#     회귀 방지: "명시 타입 우선" 기존 규칙이 휴리스틱 자동 추론에 묻히지 않는다.
# ============================================================================
class TestExplicitTypeWins:
    def test_explicit_furniture_wins_over_etc_category(self):
        """categoryId 가 ETC 로 떨어지는 카테고리라도 명시 FURNITURE 가 이긴다."""
        # 00000000 은 경로 조회가 빈 문자열 → 휴리스틱은 ETC.
        p = _make_product(
            categoryId="00000000",
            notice={"productInfoProvidedNoticeType": "FURNITURE"},
        )
        payload = _build_payload(p)
        notice = payload["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]
        assert (
            notice["productInfoProvidedNoticeType"] == "FURNITURE"
        ), "명시 타입이 휴리스틱보다 우선해야 한다 (기존 규칙 회귀 방지)."

    def test_explicit_type_passes_tripwire(self, monkeypatch):
        """명시 타입을 주면 게이트/페이로드 양쪽이 같은 값을 써 트립와이어를
        통과한다. (불일치가 아님.)"""
        monkeypatch.delenv("COMMERCE_DRY_RUN", raising=False)
        monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_CFG_FULL)
        monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))

        payload_type = mcp_server._gate_notice_type(
            "00000000",
            {"notice": {"productInfoProvidedNoticeType": "FURNITURE"}},
        )
        assert payload_type == "FURNITURE", "명시 타입이 게이트 판정에서 우선해야 한다"


# ============================================================================
# (e) 미확정/알 수 없는 카테고리에서 예외로 죽지 않는다 (fail-closed 유지).
# ============================================================================
class TestUnknownCategoryDoesNotCrash:
    def test_build_payload_unknown_category_returns_etc(self):
        """category_meta 가 모르는 ID 여도 build_payload 는 예외 없이 ETC 폴백."""
        p = _make_product(categoryId="99999999")
        payload = _build_payload(p)  # 예외 없이 반환되어야 한다.
        notice = payload["originProduct"]["detailAttribute"]["productInfoProvidedNotice"]
        assert notice["productInfoProvidedNoticeType"] == "ETC"

    def test_category_path_for_unknown_returns_empty(self):
        """알 수 없는 ID 의 경로 조회는 빈 문자열 (예외 없음)."""
        path = naver_client._category_path_for("99999999")
        assert path == "", f"알 수 없는 ID 는 빈 문자열이어야 한다: {path!r}"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
