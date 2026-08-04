# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""미리보기 HTML 생성 + 승인 게이트 검증.

본 파일은 다음을 검증한다:

(a) ``prepare_listing`` 이 미리보기 파일을 만들고 ``preview_path`` 를 반환한다.
(b) 미리보기 HTML 에 상품명·가격·판매상태·대표이미지 URL·고시 타입이 들어 있다.
(c) 설정에서 채운 고시 값이 출처 표시와 함께 나타난다.
(d) 설정 켬 + ``preview_confirmed`` 미지정 → 등록 거부, 네이버 호출 0회,
    사유에 미리보기 경로 포함.
(e) 설정 켬 + ``preview_confirmed=True`` → 기존대로 진행(다른 게이트는 그대로 적용).
(f) 설정 끔 → ``preview_confirmed`` 없이도 진행(하위호환).
(g) 미리보기 생성이 실패해도 준비 자체가 죽지 않는다(그 사실은 반환에 드러난다).

모든 테스트는 HTTP mock 으로 네이버 호출 횟수를 세고, 실제 네트워크 요청은
일으키지 않는다.
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

from clossify import common, mcp_server, naver_client, register


# --------------------------------------------------------------------------- #
# 공통 픽스처 / 헬퍼.
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_prepared_dir(tmp_path, monkeypatch):
    """``common.PREPARED_DIR`` 을 tmp_path 로 격리."""
    fake_dir = tmp_path / "prepared"
    fake_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "PREPARED_DIR", fake_dir)
    return fake_dir


# notice_config mock — 원산지/AS/제조사 + WEAR 공통 5필드.
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

_COMMON_CFG_MOCK = {
    "smartstore_notice_defaults": {
        "origin_area_code": "04",
        "origin_content": "중국",
    },
}


def _apply_common_mocks(monkeypatch):
    """게이트가 config 의 placeholder 값에 부딪히지 않도록 공통 mock 을 적용."""
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    monkeypatch.setattr(naver_client, "_kc_config", lambda: ({}, ""))
    monkeypatch.setattr(common, "cfg", lambda: _COMMON_CFG_MOCK)


def _make_attach_result(urls):
    """images.attach_images 대체 — rejected 없이 URL 리스트만 반환."""
    return {"urls": list(urls), "rejected": [], "detail": []}


def _compliant_product_input(name="미리보기테스트상품", price=39000):
    """prepare_listing 에 넘길 상품 입력 dict."""
    return {
        "name": name,
        "salePrice": price,
        "image_sources": ["http://cdn/img1.png", "http://cdn/img2.png"],
        "categoryId": "50021299",
    }


# =========================================================================== #
# (a) prepare_listing 이 미리보기 파일을 만들고 preview_path 를 반환한다.
# =========================================================================== #
class TestPreviewFileCreated:
    """prepare_listing 이 미리보기 HTML 파일을 생성하는가."""

    def test_prepare_creates_preview_file(self, isolated_prepared_dir, monkeypatch):
        """prepare_listing 반환에 preview_path 가 있고, 파일이 실제로 존재한다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())

        assert result["ok"] is True, f"prepare 실패: {result.get('error')}"
        preview_path = result.get("preview_path")
        assert preview_path is not None, "preview_path 가 반환에 없음"
        assert Path(preview_path).is_file(), f"미리보기 파일이 디스크에 없음: {preview_path}"

    def test_preview_file_is_html(self, isolated_prepared_dir, monkeypatch):
        """미리보기 파일이 HTML 파일이다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())
        preview_path = result["preview_path"]
        content = Path(preview_path).read_text(encoding="utf-8")
        assert "<html" in content.lower(), "HTML 문서가 아님"
        assert "</html>" in content.lower(), "HTML 닫기 태그가 없음"


# =========================================================================== #
# (b) 미리보기 HTML 에 상품명·가격·판매상태·대표이미지 URL·고시 타입이 들어 있다.
# =========================================================================== #
class TestPreviewHtmlContent:
    """미리보기 HTML 에 핵심 정보가 들어 있는가."""

    def test_html_contains_product_name(self, isolated_prepared_dir, monkeypatch):
        """상품명이 미리보기 HTML 에 있다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        name = "고유상품명ABC"
        product_input = _compliant_product_input(name=name)

        result = mcp_server.prepare_listing(product_input)
        content = Path(result["preview_path"]).read_text(encoding="utf-8")
        assert name in content, "상품명이 HTML 에 없음"

    def test_html_contains_price(self, isolated_prepared_dir, monkeypatch):
        """판매가가 미리보기 HTML 에 있다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        product_input = _compliant_product_input(price=42000)

        result = mcp_server.prepare_listing(product_input)
        content = Path(result["preview_path"]).read_text(encoding="utf-8")
        assert "42,000" in content, "판매가(42,000원)가 HTML 에 없음"

    def test_html_contains_sale_status(self, isolated_prepared_dir, monkeypatch):
        """판매상태(SALE)가 미리보기 HTML 에 있다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())
        content = Path(result["preview_path"]).read_text(encoding="utf-8")
        assert "SALE" in content, "판매상태 SALE 이 HTML 에 없음"

    def test_html_contains_representative_image_url(self, isolated_prepared_dir, monkeypatch):
        """대표 이미지 URL 이 미리보기 HTML 에 있다."""
        _apply_common_mocks(monkeypatch)
        rep_url = "http://cdn/representative-unique.png"
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result([rep_url]),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())
        content = Path(result["preview_path"]).read_text(encoding="utf-8")
        assert rep_url in content, "대표 이미지 URL 이 HTML 에 없음"

    def test_html_contains_notice_type(self, isolated_prepared_dir, monkeypatch):
        """고시 타입 정보가 미리보기 HTML 에 있다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())
        content = Path(result["preview_path"]).read_text(encoding="utf-8")
        # 고시 정보 섹션 헤더가 있어야 한다.
        assert "상품정보제공고시" in content, "고시 정보 섹션이 HTML 에 없음"


# =========================================================================== #
# (c) 설정에서 채운 고시 값이 출처 표시와 함께 나타난다.
# =========================================================================== #
class TestPreviewNoticeSourceLabels:
    """config 에서 채운 고시 값에 출처 표시가 있는가."""

    def test_config_filled_value_has_source_label(self, isolated_prepared_dir, monkeypatch):
        """설정 기본값으로 채운 필드에 '설정 기본값' 출처가 표시된다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())
        content = Path(result["preview_path"]).read_text(encoding="utf-8")
        # config 에서 채워지는 공통 5필드(returnCostReason 등)는 출처 표시가
        # 있어야 한다. source-config CSS 클래스로 표시된다.
        assert "설정 기본값" in content, "config 채움 필드에 '설정 기본값' 출처 표시가 없음"


# =========================================================================== #
# (d) 설정 켬 + preview_confirmed 미지정 → 등록 거부, 네이버 호출 0회.
# =========================================================================== #
class TestPreviewGateBlocks:
    """미리보기 승인 없이 등록을 거부하는가."""

    def test_blocks_without_preview_confirmed(self, isolated_prepared_dir, monkeypatch):
        """require_preview_confirmation 켬 + preview_confirmed=False → 거부."""
        # conftest 가 기본으로 게이트를 끄므로, 여기서 명시적으로 다시 켠다.
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: True)
        _apply_common_mocks(monkeypatch)

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "x"}),
        )

        result = mcp_server.register_product(
            name="게이트테스트",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
        )
        assert result["ok"] is False
        assert result.get("blocked_by") == "preview_confirmation"
        assert len(naver_calls) == 0, f"미리보기 게이트 차단인데 naver 호출됨: {len(naver_calls)}회"

    def test_block_message_contains_preview_path(self, isolated_prepared_dir, monkeypatch):
        """거부 사유에 미리보기 파일 경로가 포함된다.

        prepare_listing 을 먼저 호출해 prepared payload 에 preview_path 를
        기록한 뒤, register_product 를 호출하면 차단 메시지에 경로가 있다.
        """
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: True)
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )

        # prepare_listing 으로 prepared payload + 미리보기 생성.
        prep = mcp_server.prepare_listing(_compliant_product_input())
        assert prep["ok"] is True
        product_key = prep["product_key"]
        preview_path = prep["preview_path"]
        assert preview_path is not None

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "x"}),
        )

        result = mcp_server.register_product(
            name="미리보기테스트상품",
            price=39000,
            category_id="50021299",
            product_key=product_key,
            # image_urls/detail_html 생략 → prepared 에서 채움.
        )
        assert result["ok"] is False
        assert result.get("blocked_by") == "preview_confirmation"
        # 차단 응답에 preview_path 가 있어야 한다.
        assert (
            result.get("preview_path") == preview_path
        ), f"차단 응답의 preview_path 불일치: {result.get('preview_path')}"
        # 메시지에 미리보기 파일 경로가 포함되어야 한다.
        message = result.get("message", "")
        assert preview_path in message, "거부 메시지에 미리보기 파일 경로가 포함되지 않음"
        assert len(naver_calls) == 0

    def test_block_response_has_dry_run_marker(self, isolated_prepared_dir, monkeypatch):
        """차단 응답에 dry_run 마커가 있다."""
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: True)
        monkeypatch.setenv("COMMERCE_DRY_RUN", "1")
        _apply_common_mocks(monkeypatch)

        result = mcp_server.register_product(
            name="드라이런게이트",
            price=10000,
            category_id="50021299",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
        )
        assert result["ok"] is False
        assert result.get("blocked_by") == "preview_confirmation"
        assert result.get("dry_run") is True


# =========================================================================== #
# (e) 설정 켬 + preview_confirmed=True → 진행 (다른 게이트는 그대로 적용).
# =========================================================================== #
class TestPreviewGatePassesWithConfirmation:
    """preview_confirmed=True 면 게이트를 통과하는가."""

    def test_proceeds_with_preview_confirmed_true(self, isolated_prepared_dir, monkeypatch):
        """require_preview_confirmation 켬 + preview_confirmed=True → 진행."""
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: True)
        _apply_common_mocks(monkeypatch)
        # 카테고리 50000000 은 고시 타입 ETC 로 추론된다 — build_payload mock 의
        # ETC 타입과 일치해야 notice_type_tripwire 에 걸리지 않는다.
        # etc 본문에 공통 5필드 + ETC 필수 필드를 채워 컴플라이언스 게이트도 통과.
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: {
                "originProduct": {
                    "images": {
                        "representativeImage": {"url": "http://cdn/x.png"},
                    },
                    "detailAttribute": {
                        "productInfoProvidedNotice": {
                            "productInfoProvidedNoticeType": "ETC",
                            "etc": {
                                "returnCostReason": "단순변심 반품비용 구매자부담",
                                "noRefundReason": "주문제작 청약철회 제한",
                                "qualityAssuranceStandard": "관련법에 따름",
                                "compensationProcedure": "소비자분쟁해결기준",
                                "troubleShootingContents": "고객센터 문의",
                                "itemName": "테스트상품",
                                "modelName": "테스트모델",
                                "certificateDetails": "KOR-2024-001",
                                "manufacturer": "테스트제조사",
                                "afterServiceDirector": "070-1234-5678",
                            },
                        },
                        "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                        "afterServiceInfo": {"afterServiceTelephoneNumber": "070-1234-5678"},
                    },
                },
            },
        )

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "ok"}),
        )

        result = mcp_server.register_product(
            name="승인통과",
            price=10000,
            category_id="50000000",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        assert result["ok"] is True, f"preview_confirmed=True 인데 실패: {result.get('blocked_by')}"
        assert result.get("blocked_by") is None
        assert len(naver_calls) == 1, "승인 통과 시 1회 호출"

    def test_other_gates_still_apply_with_confirmation(self, isolated_prepared_dir, monkeypatch):
        """preview_confirmed=True 여도 컴플라이언스 게이트는 그대로 차단한다."""
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: True)
        _apply_common_mocks(monkeypatch)
        # 카테고리 50000000 (ETC 추론) — tripwire 를 피하고 컴플라이언스 게이트로.
        # 빈 대표 이미지 페이로드 → 컴플라이언스 게이트가 반드시 차단.
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: {
                "originProduct": {
                    "images": {
                        "representativeImage": {"url": ""},
                    },
                    "detailAttribute": {
                        "productInfoProvidedNotice": {
                            "productInfoProvidedNoticeType": "ETC",
                        },
                    },
                },
            },
        )

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "x"}),
        )

        result = mcp_server.register_product(
            name="컴플라이언스차단",
            price=10000,
            category_id="50000000",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            preview_confirmed=True,
        )
        # 미리보기 게이트는 통과했지만 컴플라이언스 게이트가 차단.
        assert result["ok"] is False
        assert result.get("blocked_by") == "compliance"
        assert len(naver_calls) == 0


# =========================================================================== #
# (f) 설정 끔 → preview_confirmed 없이도 진행 (하위호환).
# =========================================================================== #
class TestPreviewGateDisabled:
    """설정에서 게이트를 끄면 preview_confirmed 없이도 진행하는가."""

    def test_proceeds_without_preview_confirmed_when_disabled(
        self, isolated_prepared_dir, monkeypatch
    ):
        """require_preview_confirmation=False → preview_confirmed 없이 진행."""
        # conftest 가 기본으로 끄지만, 명시적으로 확인.
        monkeypatch.setattr(mcp_server, "_config_require_preview_confirmation", lambda: False)
        _apply_common_mocks(monkeypatch)
        # 카테고리 50000000 (ETC 추론) — tripwire 를 피한다.
        # etc 본문에 공통 5필드 + ETC 필수 필드를 채워 컴플라이언스 게이트도 통과.
        monkeypatch.setattr(
            naver_client,
            "build_payload",
            lambda *a, **kw: {
                "originProduct": {
                    "images": {
                        "representativeImage": {"url": "http://cdn/x.png"},
                    },
                    "detailAttribute": {
                        "productInfoProvidedNotice": {
                            "productInfoProvidedNoticeType": "ETC",
                            "etc": {
                                "returnCostReason": "단순변심 반품비용 구매자부담",
                                "noRefundReason": "주문제작 청약철회 제한",
                                "qualityAssuranceStandard": "관련법에 따름",
                                "compensationProcedure": "소비자분쟁해결기준",
                                "troubleShootingContents": "고객센터 문의",
                                "itemName": "테스트상품",
                                "modelName": "테스트모델",
                                "certificateDetails": "KOR-2024-001",
                                "manufacturer": "테스트제조사",
                                "afterServiceDirector": "070-1234-5678",
                            },
                        },
                        "originAreaInfo": {"originAreaCode": "04", "content": "중국"},
                        "afterServiceInfo": {"afterServiceTelephoneNumber": "070-1234-5678"},
                    },
                },
            },
        )

        naver_calls: list = []
        monkeypatch.setattr(
            naver_client,
            "register_product",
            lambda payload: naver_calls.append(payload) or (200, {"originProductNo": "ok"}),
        )

        result = mcp_server.register_product(
            name="게이트끔",
            price=10000,
            category_id="50000000",
            image_urls=["http://cdn/x.png"],
            detail_html="<html></html>",
            # preview_confirmed 생략 — 기본값 False.
        )
        assert result["ok"] is True, f"게이트 끔인데 실패: {result.get('blocked_by')}"
        assert len(naver_calls) == 1


# =========================================================================== #
# (g) 미리보기 생성 실패해도 준비 자체가 죽지 않는다.
# =========================================================================== #
class TestPreviewFailureGraceful:
    """미리보기 생성 실패 시 prepare_listing 이 죽지 않는가."""

    def test_prepare_survives_preview_failure(self, isolated_prepared_dir, monkeypatch):
        """preview.write_preview_html 이 예외를 던져도 prepare_listing 은 산다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        # 미리보기 쓰기를 실패시킨다.
        monkeypatch.setattr(
            "clossify.preview.write_preview_html",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("preview boom")),
        )

        result = mcp_server.prepare_listing(_compliant_product_input())
        assert result["ok"] is True, f"미리보기 실패로 prepare 가 죽음: {result.get('error')}"
        # preview_path 가 None 으로 드러난다 (조용한 성공이 아님).
        assert (
            result.get("preview_path") is None
        ), "미리보기 생성 실패인데 preview_path 가 None 이 아님"

    def test_prepare_payload_has_preview_path_none_on_failure(
        self, isolated_prepared_dir, monkeypatch
    ):
        """미리보기 실패 시 prepared payload 의 preview_path 가 None 이다."""
        _apply_common_mocks(monkeypatch)
        monkeypatch.setattr(
            "clossify.images.attach_images",
            lambda srcs: _make_attach_result(["http://cdn/a.png"]),
        )
        monkeypatch.setattr(
            "clossify.preview.write_preview_html",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("preview boom")),
        )

        product_input = _compliant_product_input(name="고유실패테스트")
        result = mcp_server.prepare_listing(product_input)
        assert result["ok"] is True

        # 디스크의 prepared payload 에도 preview_path 가 None 이다.
        product_key = result["product_key"]
        prepared = register.load_prepared_payload(product_key=product_key)
        assert prepared.get("preview_path") is None
