# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""보기 전용(패널) 모드 검증.

본 파일은 티켓 "미리보기 — 보기 전용(패널)과 조작(브라우저)을 가른다" 의
계약 (a)~(g) 를 검증한다:

  (a) 보기 전용 HTML: ``<script>``·``contenteditable``·``<button>``·``<input>``
      ·``onclick``·``addEventListener`` 가 **각각 0개**.
  (b) 보기 전용 HTML 에 상품명·가격이 **그대로** 들어있다(정보 손실 없음).
  (c) 보기 전용 HTML 에 "보기 전용" 취지 표기가 있다.
  (d) 조작 모드 HTML 은 승인 폼(POST · hidden 토큰)을 **여전히** 갖는다(회귀).
  (e) 조작 모드 승인부에 ``fetch(`` 가 **없다**(회귀 — 프리플라이트 재발 방지).
  (f) 모드 인자 **없이** 호출하면 **보기 전용**이 나온다(안전 기본값).
  (g) 두 모드가 **같은 상품 정보**를 담는다(내용 동등성).

왜 별도 파일인가: 보기 전용 모드는 **죽은 UI 를 0개로** 만드는 정책이다.
죽은 UI 라는 결함은 정책·브라우저 동작을 함께 건드리므로, 전용 테스트가
정책 위반을 한 줄의 패턴으로 즉각 잡아야 한다. 또한 회귀(조작 모드에서
승인 바가 사라지는 등)도 같이 잡는다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# src 레이아웃 지원.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from clossify import naver_client, preview
from tests.test_preview_gate import _NOTICE_MOCK  # 기존 이스케이프 반례 재사용


# --------------------------------------------------------------------------- #
# 공통 픽스처 / 헬퍼.
# --------------------------------------------------------------------------- #
@pytest.fixture
def notice_config_mock(monkeypatch):
    """``naver_client._notice_config`` 를 테스트용 mock 으로 치환."""
    monkeypatch.setattr(naver_client, "_notice_config", lambda: _NOTICE_MOCK)
    return _NOTICE_MOCK


def _sample_payload(name="테스트상품", price=39000):
    """렌더러에 넘길 최소 payload dict."""
    return {
        "product": {
            "name": name,
            "salePrice": price,
            "categoryId": "50021299",
            "tags": ["겨울", "후드티"],
            "notice": {
                "etc": {
                    "returnCostReason": "단순변심 반품비용 구매자부담",
                },
            },
        },
        "images": {"listing_urls": ["https://cdn.example.com/a.png"]},
        "detail_html": "<html><body>상세</body></html>",
        "status": "SALE",
    }


def _render_view_only(payload=None, product_key="viewkey12345"):
    payload = payload or _sample_payload()
    return preview.render_preview_html(payload, product_key=product_key, mode="view_only")


def _render_interactive(payload=None, product_key="interkey1234"):
    payload = payload or _sample_payload()
    return preview.render_preview_html(payload, product_key=product_key, mode="interactive")


# =========================================================================== #
# (a) 보기 전용 HTML: script·contenteditable·button·input·onclick·addEventListener 0개.
# =========================================================================== #
class TestViewOnlyHasZeroDeadUI:
    """보기 전용 모드가 "죽은 UI" 0개를 보장하는가."""

    def test_no_script_tag(self, notice_config_mock):
        """``<script>`` 태그가 0개."""
        html = _render_view_only()
        # <script> 여는 태그가 있으면 안 됨(인라인·외부 모두).
        assert (
            "<script" not in html.lower()
        ), f"보기 전용 HTML 에 <script> 가 있음: {re.findall(r'<script[^>]*>', html, re.IGNORECASE)}"

    def test_no_contenteditable(self, notice_config_mock):
        """``contenteditable`` 속성이 0개."""
        html = _render_view_only()
        assert (
            "contenteditable" not in html.lower()
        ), "보기 전용 HTML 에 contenteditable 속성이 있음 — 편집 가능 UI 가 패널에 노출"

    def test_no_button_element(self, notice_config_mock):
        """``<button>`` 요소가 0개."""
        html = _render_view_only()
        assert (
            "<button" not in html.lower()
        ), f"보기 전용 HTML 에 <button> 이 있음: {re.findall(r'<button[^>]*>', html, re.IGNORECASE)}"

    def test_no_input_element(self, notice_config_mock):
        """``<input>`` 요소가 0개(hidden 포함)."""
        html = _render_view_only()
        assert (
            "<input" not in html.lower()
        ), f"보기 전용 HTML 에 <input> 이 있음: {re.findall(r'<input[^>]*>', html, re.IGNORECASE)}"

    def test_no_onclick_attribute(self, notice_config_mock):
        """``onclick`` 인라인 이벤트 핸들러가 0개."""
        html = _render_view_only()
        # onclick= 패턴이 없어야 한다(따옴표 종류 무관).
        assert not re.search(
            r"\sonclick\s*=", html, re.IGNORECASE
        ), "보기 전용 HTML 에 onclick 인라인 핸들러가 있음"

    def test_no_addEventListener_in_text(self, notice_config_mock):
        """``addEventListener`` 자바스크립트 호출이 0개."""
        html = _render_view_only()
        # 스크립트 자체가 없으므로 addEventListener 도 없어야 한다.
        assert (
            "addEventListener" not in html
        ), "보기 전용 HTML 에 addEventListener 텍스트가 있음 — 스크립트 잔존 가능성"

    def test_no_other_inline_event_handlers(self, notice_config_mock):
        """보기 전용 HTML 은 모든 ``on*`` 인라인 이벤트 핸들러 속성이 0개.

        티켓 계약은 ``onclick``/``addEventListener`` 0개지만, 보기 전용 모드는
        더 엄격하게 모든 인라인 이벤트 핸들러(``onerror``·``onload``·``onfocus``
        등)를 0개로 유지한다 — 패널에서 실행 가능한 코드 경로를 원천 차단.
        단, CSS 클래스명·텍스트 안의 단어 "onload" 등은 속성이 아니므로 제외.
        """
        html = _render_view_only()
        # 속성 형태(공백 + onXXX =)만 잡는다.
        inline_handlers = re.findall(r"\son[a-z]+\s*=", html, re.IGNORECASE)
        assert (
            inline_handlers == []
        ), f"보기 전용 HTML 에 인라인 이벤트 핸들러 속성이 있음: {inline_handlers}"

    def test_no_form_element(self, notice_config_mock):
        """``<form>`` 요소가 0개 — 폼도 패널에서는 제출 불가하므로."""
        html = _render_view_only()
        assert (
            "<form" not in html.lower()
        ), f"보기 전용 HTML 에 <form> 이 있음: {re.findall(r'<form[^>]*>', html, re.IGNORECASE)}"

    def test_no_edit_field_class(self, notice_config_mock):
        """``edit-field`` 클래스(편집 단서)가 0개."""
        html = _render_view_only()
        assert (
            "edit-field" not in html
        ), "보기 전용 HTML 에 edit-field 클래스가 있음 — 편집 단서가 패널에 노출"

    def test_no_data_field_attribute(self, notice_config_mock):
        """``data-field`` 속성이 0개 — 편집 단서 속성도 빠져야."""
        html = _render_view_only()
        assert (
            "data-field" not in html
        ), "보기 전용 HTML 에 data-field 속성이 있음 — 편집 단서가 패널에 노출"


# =========================================================================== #
# (b) 보기 전용 HTML 에 상품명·가격이 그대로 들어있다(정보 손실 없음).
# =========================================================================== #
class TestViewOnlyPreservesInformation:
    """컨트롤만 사라지고 정보는 사라지면 안 된다."""

    def test_product_name_present(self, notice_config_mock):
        """상품명이 보기 전용 HTML 에 그대로 있다."""
        html = _render_view_only(_sample_payload(name="고유상품명XYZ"))
        assert "고유상품명XYZ" in html, "보기 전용 HTML 에 상품명이 없음 — 정보 손실"

    def test_product_price_present(self, notice_config_mock):
        """판매가가 보기 전용 HTML 에 쉼표 포함 형태로 있다."""
        html = _render_view_only(_sample_payload(price=42000))
        assert "42,000원" in html, "보기 전용 HTML 에 판매가(42,000원)가 없음 — 정보 손실"

    def test_category_id_present(self, notice_config_mock):
        """카테고리 ID 가 보기 전용 HTML 에 있다."""
        html = _render_view_only()
        assert "50021299" in html, "보기 전용 HTML 에 카테고리 ID 가 없음"

    def test_sale_status_present(self, notice_config_mock):
        """판매상태(SALE) 표시가 보기 전용 HTML 에 있다."""
        html = _render_view_only()
        assert "SALE" in html, "보기 전용 HTML 에 판매상태 SALE 이 없음"

    def test_image_url_present(self, notice_config_mock):
        """이미지 CDN URL 이 보기 전용 HTML 에 있다(대표 이미지 포함)."""
        html = _render_view_only()
        assert (
            "https://cdn.example.com/a.png" in html
        ), "보기 전용 HTML 에 이미지 URL 이 없음 — 정보 손실"

    def test_notice_value_present(self, notice_config_mock):
        """고시 값(returnCostReason) 이 보기 전용 HTML 에 있다."""
        html = _render_view_only()
        assert (
            "단순변심 반품비용 구매자부담" in html
        ), "보기 전용 HTML 에 고시 값이 없음 — 정보 손실"

    def test_tags_present(self, notice_config_mock):
        """태그 값이 보기 전용 HTML 에 있다."""
        html = _render_view_only()
        assert "겨울" in html and "후드티" in html, "보기 전용 HTML 에 태그가 없음 — 정보 손실"

    def test_detail_html_present(self, notice_config_mock):
        """상세 HTML 이 iframe srcdoc 으로 들어있다(두 모드 공통)."""
        html = _render_view_only()
        assert 'srcdoc="' in html, "보기 전용 HTML 에 iframe srcdoc 이 없음"


# =========================================================================== #
# (c) 보기 전용 HTML 에 "보기 전용" 취지 표기가 있다.
# =========================================================================== #
class TestViewOnlyBanner:
    """보기 전용임을 알리고 조작이 가능한 자리를 안내하는 표기가 있는가."""

    def test_view_only_phrase_present(self, notice_config_mock):
        """'보기 전용' 이라는 문구가 있다."""
        html = _render_view_only()
        assert (
            "보기 전용" in html
        ), "보기 전용 HTML 에 '보기 전용' 표기가 없음 — 사용자가 조작 가능한 줄 착각"

    def test_view_only_banner_class_present(self, notice_config_mock):
        """``view-only-banner`` CSS 클래스가 있다."""
        html = _render_view_only()
        assert "view-only-banner" in html, "보기 전용 배너 클래스(view-only-banner)가 없음"

    def test_browser_hint_present(self, notice_config_mock):
        """브라우저 창에서 조작이 가능하다는 안내가 있다."""
        html = _render_view_only()
        # '브라우저' 라는 단어가 안내에 있어야 한다(어디서 조작하는지).
        assert "브라우저" in html, "보기 전용 안내에 '브라우저' 안내가 없음 — 조작 자리를 모름"

    def test_view_only_banner_is_not_interactive(self, notice_config_mock):
        """보기 전용 배너 자체가 인터랙티브 요소(버튼·폼)가 아니다."""
        html = _render_view_only()
        # view-only-banner div 안에 button/input/form 이 없어야 한다.
        banner_match = re.search(
            r'<div class="view-only-banner">.*?</div>',
            html,
            re.DOTALL,
        )
        assert banner_match is not None, "view-only-banner div 를 찾을 수 없음"
        banner = banner_match.group(0)
        assert "<button" not in banner.lower(), "보기 전용 배너 안에 <button> 이 있음"
        assert "<input" not in banner.lower(), "보기 전용 배너 안에 <input> 이 있음"
        assert "onclick" not in banner.lower(), "보기 전용 배너 안에 onclick 이 있음"


# =========================================================================== #
# (d) 조작 모드 HTML 은 승인 폼(POST · hidden 토큰)을 여전히 갖는다(회귀).
# =========================================================================== #
class TestInteractiveApprovalFormNoRegression:
    """조작 모드가 승인 폼 POST + hidden 토큰을 유지하는가."""

    def _render_with_approval(self):
        payload = _sample_payload()
        return preview.render_preview_html(
            payload,
            product_key="approvalkey1",
            approval_token="dummytoken",
            approval_port=54321,
            mode="interactive",
        )

    def test_approval_form_present(self, notice_config_mock):
        """조작 모드에 승인 폼이 POST 로 있다."""
        html = self._render_with_approval()
        assert '<form id="approval-form"' in html, "조작 모드에 승인 폼이 없음(회귀)"
        assert 'method="POST"' in html, "조작 모드 승인 폼이 POST 가 아님(회귀)"

    def test_approval_form_has_hidden_token(self, notice_config_mock):
        """조작 모드 승인 폼에 hidden 토큰 필드가 있다."""
        html = self._render_with_approval()
        assert 'name="token"' in html, "조작 모드 승인 폼에 hidden token 이 없음(회귀)"
        assert 'type="hidden"' in html, "조작 모드 승인 폼에 hidden 필드가 없음(회귀)"

    def test_approval_form_has_hidden_product_key(self, notice_config_mock):
        """조작 모드 승인 폼에 hidden product_key 필드가 있다."""
        html = self._render_with_approval()
        assert 'name="product_key"' in html, "조작 모드 승인 폼에 hidden product_key 가 없음(회귀)"

    def test_submit_buttons_present(self, notice_config_mock):
        """조작 모드에 [승인]/[수정 후 승인] submit 버튼이 있다."""
        html = self._render_with_approval()
        assert "승인" in html, "조작 모드에 [승인] 버튼 텍스트가 없음"
        assert "수정 후 승인" in html, "조작 모드에 [수정 후 승인] 버튼 텍스트가 없음"
        # 두 버튼 모두 type=submit.
        submit_btns = re.findall(
            r'<button[^>]*type="submit"[^>]*>(승인|수정 후 승인)</button>', html
        )
        assert "승인" in submit_btns, "[승인] 버튼이 type=submit 이 아님(회귀)"
        assert "수정 후 승인" in submit_btns, "[수정 후 승인] 버튼이 type=submit 이 아님(회귀)"


# =========================================================================== #
# (e) 조작 모드 승인부에 fetch( 가 없다(회귀 — 프리플라이트 재발 방지).
# =========================================================================== #
class TestInteractiveNoFetchRegression:
    """조작 모드 승인부가 fetch 를 쓰지 않는가(CORS 프리플라이트 회피)."""

    def test_no_fetch_in_interactive_mode(self, notice_config_mock):
        """조작 모드 전체 HTML 에 ``fetch(`` 가 없다."""
        payload = _sample_payload()
        html = preview.render_preview_html(
            payload,
            product_key="nofetchkey1",
            approval_token="dummytoken",
            approval_port=54321,
            mode="interactive",
        )
        assert (
            "fetch(" not in html
        ), "조작 모드에 fetch() 호출이 있음 — CORS 프리플라이트 재발 위험(회귀)"

    def test_no_custom_header_in_interactive_mode(self, notice_config_mock):
        """조작 모드에 X-Approval-Token 커스텀 헤더 리터럴이 없다."""
        payload = _sample_payload()
        html = preview.render_preview_html(
            payload,
            product_key="nohdrkey1",
            approval_token="dummytoken",
            approval_port=54321,
            mode="interactive",
        )
        assert (
            "X-Approval-Token" not in html
        ), "조작 모드에 X-Approval-Token 헤더 리터럴이 있음 — 커스텀 헤더 회귀"


# =========================================================================== #
# (f) 모드 인자 없이 호출하면 보기 전용이 나온다(안전 기본값).
# =========================================================================== #
class TestDefaultModeIsViewOnly:
    """모드 인자 생략 시 안전한 쪽(보기 전용)이 기본인가."""

    def test_default_render_has_no_script(self, notice_config_mock):
        """``mode`` 인자 없이 호출해도 ``<script>`` 가 0개."""
        html = preview.render_preview_html(_sample_payload(), product_key="defaultkey1")
        assert (
            "<script" not in html.lower()
        ), "기본값(모드 생략) HTML 에 <script> 가 있음 — 보기 전용이 기본이 아님"

    def test_default_render_has_no_button(self, notice_config_mock):
        """``mode`` 인자 없이 호출해도 ``<button>`` 이 0개."""
        html = preview.render_preview_html(_sample_payload(), product_key="defaultkey2")
        assert (
            "<button" not in html.lower()
        ), "기본값(모드 생략) HTML 에 <button> 이 있음 — 보기 전용이 기본이 아님"

    def test_default_render_has_no_contenteditable(self, notice_config_mock):
        """``mode`` 인자 없이 호출해도 ``contenteditable`` 이 0개."""
        html = preview.render_preview_html(_sample_payload(), product_key="defaultkey3")
        assert (
            "contenteditable" not in html.lower()
        ), "기본값(모드 생략) HTML 에 contenteditable 이 있음 — 보기 전용이 기본이 아님"

    def test_default_render_has_view_only_banner(self, notice_config_mock):
        """``mode`` 인자 없이 호출해도 보기 전용 배너가 있다."""
        html = preview.render_preview_html(_sample_payload(), product_key="defaultkey4")
        assert (
            "보기 전용" in html
        ), "기본값(모드 생략) HTML 에 '보기 전용' 표기가 없음 — 보기 전용이 기본이 아님"

    def test_default_render_still_has_product_info(self, notice_config_mock):
        """기본값(보기 전용)도 상품명·가격을 담는다(정보 동등성)."""
        html = preview.render_preview_html(_sample_payload(), product_key="defaultkey5")
        assert "테스트상품" in html, "기본값 HTML 에 상품명이 없음"
        assert "39,000원" in html, "기본값 HTML 에 판매가가 없음"

    def test_unknown_mode_falls_back_to_view_only(self, notice_config_mock):
        """알 수 없는 모드 문자열도 안전하게 보기 전용으로 강등된다."""
        html = preview.render_preview_html(
            _sample_payload(),
            product_key="unknownmode1",
            mode="totally-unknown",  # type: ignore[arg-type]
        )
        assert (
            "<script" not in html.lower()
        ), "알 수 없는 모드가 조작 모드로 해석됨 — 안전 기본값 위반"
        assert "보기 전용" in html, "알 수 없는 모드에서 보기 전용 배너가 없음"


# =========================================================================== #
# (g) 두 모드가 같은 상품 정보를 담는다(내용 동등성).
# =========================================================================== #
class TestModeContentEquivalence:
    """두 모드가 동일한 상품 정보를 담는가."""

    def test_both_modes_have_same_name(self, notice_config_mock):
        payload = _sample_payload(name="동등성상품명")
        vo = preview.render_preview_html(payload, product_key="eq1", mode="view_only")
        iv = preview.render_preview_html(payload, product_key="eq1", mode="interactive")
        assert "동등성상품명" in vo
        assert "동등성상품명" in iv

    def test_both_modes_have_same_price(self, notice_config_mock):
        payload = _sample_payload(price=123456)
        vo = preview.render_preview_html(payload, product_key="eq2", mode="view_only")
        iv = preview.render_preview_html(payload, product_key="eq2", mode="interactive")
        assert "123,456원" in vo, "보기 전용 모드에 가격이 없음"
        assert "123,456원" in iv, "조작 모드에 가격이 없음"

    def test_both_modes_have_same_image_url(self, notice_config_mock):
        payload = _sample_payload()
        url = "https://cdn.example.com/equivalence-test.png"
        payload["images"]["listing_urls"] = [url]
        vo = preview.render_preview_html(payload, product_key="eq3", mode="view_only")
        iv = preview.render_preview_html(payload, product_key="eq3", mode="interactive")
        assert url in vo, "보기 전용 모드에 이미지 URL 이 없음"
        assert url in iv, "조작 모드에 이미지 URL 이 없음"

    def test_both_modes_have_same_notice_value(self, notice_config_mock):
        payload = _sample_payload()
        payload["product"]["notice"]["etc"]["returnCostReason"] = "동등고시값XYZ"
        vo = preview.render_preview_html(payload, product_key="eq4", mode="view_only")
        iv = preview.render_preview_html(payload, product_key="eq4", mode="interactive")
        assert "동등고시값XYZ" in vo, "보기 전용 모드에 고시 값이 없음"
        assert "동등고시값XYZ" in iv, "조작 모드에 고시 값이 없음"

    def test_both_modes_have_same_status_banner(self, notice_config_mock):
        """두 모드 모두 판매상태 배너를 갖는다(판매중지/판매중 거짓 표시 방지)."""
        payload = _sample_payload()
        payload["status"] = "SUSPENSION"
        vo = preview.render_preview_html(payload, product_key="eq5", mode="view_only")
        iv = preview.render_preview_html(payload, product_key="eq5", mode="interactive")
        assert "SUSPENSION" in vo, "보기 전용 모드에 SUSPENSION 표시가 없음"
        assert "SUSPENSION" in iv, "조작 모드에 SUSPENSION 표시가 없음"
        # 판매중지 배너 문구가 두 모드 모두에 있어야 한다.
        assert "판매중지" in vo, "보기 전용 모드에 판매중지 배너가 없음"
        assert "판매중지" in iv, "조작 모드에 판매중지 배너가 없음"


# =========================================================================== #
# 보기 전용 모드가 승인 토큰·포트를 무시하는지 확인 (패널에서 승인 바 금지).
# =========================================================================== #
class TestViewOnlyIgnoresApprovalArgs:
    """보기 전용 모드는 approval_token/port 가 주어져도 승인 바를 렌더하지 않는다.

    패널은 폼을 제출할 수 없으므로, 보기 전용 모드에서 승인 버튼은 죽은 UI 다.
    """

    def test_view_only_has_no_approval_form_even_with_token(self, notice_config_mock):
        """approval_token/port 를 줘도 보기 전용 모드에는 승인 폼이 없다."""
        html = preview.render_preview_html(
            _sample_payload(),
            product_key="voignore1",
            approval_token="dummytoken",
            approval_port=54321,
            mode="view_only",
        )
        assert (
            '<form id="approval-form"' not in html
        ), "보기 전용 모드가 approval_token 을 무시하지 않음 — 패널에 죽은 승인 바 노출"
        assert "<button" not in html.lower(), "보기 전용 모드에 승인 바로 인해 <button> 이 생김"
        assert (
            "승인" not in html or "보기 전용" in html
        ), "보기 전용 모드에 승인 버튼 텍스트가 승인 바 형태로 있음"

    def test_view_only_does_not_leak_token(self, notice_config_mock):
        """보기 전용 모드 HTML 에 approval 토큰 값이 노출되지 않는다."""
        html = preview.render_preview_html(
            _sample_payload(),
            product_key="voleak1",
            approval_token="SECRET_TOKEN_NOT_SHOWN",
            approval_port=54321,
            mode="view_only",
        )
        assert (
            "SECRET_TOKEN_NOT_SHOWN" not in html
        ), "보기 전용 모드 HTML 에 approval 토큰이 노출됨 — 패널에 비밀값 유출"
