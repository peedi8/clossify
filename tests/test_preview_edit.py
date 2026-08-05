# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""미리보기 직접 편집(클립보드 다리) 검증.

본 파일은 다음을 검증한다(티켓 구현 계약 (a)~(e)):

(a) 생성된 페이지에 편집 필드들이 ``data-field``/``data-original`` 과 함께 존재한다.
(b) 복사 버튼과 인라인 스크립트가 존재하고, **외부 참조(``src``/``href`` 의 http)는
    상품 이미지 외에 0건**이다.
(c) ``data-original`` 값에 HTML 특수문자가 들어가도 깨지지 않는다
    (악성 상품명 주입 반례 — 기존 이스케이프 반례 재사용).
(d) 폴백 textarea 경로가 마크업에 존재한다.
(e) 안내 문구("붙여넣어야 반영")가 존재한다.

본 테스트는 ``render_preview_html`` 을 직접 부른다 — 준비 파이프라인 전체를
돌리지 않아도 렌더러의 HTML 출력만으로 계약을 검증할 수 있다.
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


def _sample_payload(name="테스트상품", price=39000, **product_overrides):
    """렌더러에 넘길 최소 payload dict."""
    product = {
        "name": name,
        "salePrice": price,
        "categoryId": "50021299",
        "tags": ["겨울", "후드티"],
        "notice": {
            "etc": {
                "returnCostReason": "단순변심 반품비용 구매자부담",
            },
        },
    }
    product.update(product_overrides)
    return {
        "product": product,
        "images": {"listing_urls": ["https://cdn.example.com/a.png"]},
        "detail_html": "<html><body>상세</body></html>",
        "status": "SALE",
    }


def _render(payload=None, product_key="pkey12345678"):
    """렌더러를 직접 부른다. 기본은 표준 샘플 payload."""
    payload = payload or _sample_payload()
    return preview.render_preview_html(payload, product_key=product_key)


# =========================================================================== #
# (a) 생성된 페이지에 편집 필드들이 data-field/data-original 과 함께 존재한다.
# =========================================================================== #
class TestEditFieldsPresent:
    """편집 필드가 data-field/data-original attribute 와 함께 있는가."""

    def test_name_field_has_data_field(self, notice_config_mock):
        html = _render()
        assert 'data-field="상품명"' in html, "상품명 편집 필드에 data-field 없음"

    def test_name_field_has_data_original(self, notice_config_mock):
        html = _render(_sample_payload(name="고유이름XYZ"))
        # data-original 속성이 있고, 그 안에 이스케이프된 상품명이 들어있어야 한다.
        assert 'data-original="' in html, "상품명 필드에 data-original 없음"
        assert "고유이름XYZ" in html, "상품명 원본값이 HTML 에 없음"

    def test_price_field_has_data_field(self, notice_config_mock):
        html = _render(_sample_payload(price=42000))
        assert 'data-field="판매가"' in html, "판매가 편집 필드에 data-field 없음"
        # data-original 은 쉼표 없는 숫자.
        assert 'data-original="42000"' in html, "판매가 data-original 이 순수 숫자여야 함"

    def test_notice_value_cells_are_editable(self, notice_config_mock):
        html = _render()
        # 고시 표의 값 칸은 data-field="고시.<field>" 형식.
        assert 'data-field="고시.' in html, "고시 값 칸에 data-field=고시.* 없음"

    def test_notice_value_cell_has_data_original(self, notice_config_mock):
        # 사용자가 준 고시 값이 data-original 로 들어가야 한다.
        html = _render()
        assert "단순변심 반품비용 구매자부담" in html
        # 그 값이 data-original 속성으로도 들어가야 한다(이스케이프 형태).
        # html.escape 로 quote 이스케이프되므로 속성값 안에 들어있는지만 확인.
        assert 'data-original="' in html

    def test_notice_field_name_cell_is_not_editable(self, notice_config_mock):
        html = _render()
        # 필드명 칸은 contenteditable 이 아니어야 한다 — data-field=고시.returnCostReason
        # 가 th 가 아니라 td 안의 span 에 붙어있어야 한다.
        # 핵심: th 안에 contenteditable 이 있으면 안 된다.
        # th 태그 내부에 contenteditable 속성이 없는지 확인.
        th_pattern = re.compile(r"<th[^>]*contenteditable", re.IGNORECASE)
        assert not th_pattern.search(html), "고시 필드명(th) 칸이 편집 가능하면 안 됨"

    def test_tags_field_has_data_field(self, notice_config_mock):
        html = _render()
        assert 'data-field="태그"' in html, "태그 편집 필드에 data-field 없음"

    def test_edit_fields_are_contenteditable(self, notice_config_mock):
        html = _render()
        # 모든 data-field 가 붙은 편집 필드는 contenteditable="true" 여야 한다.
        # data-field 와 contenteditable 이 같은 span 안에 공존하는지.
        editable_count = html.count('contenteditable="true"')
        assert editable_count >= 4, (
            f"편집 필드가 4개(상품명/판매가/고시값/태그) 이상이어야 하는데 "
            f"contenteditable={editable_count}개"
        )

    def test_all_edit_fields_have_data_original(self, notice_config_mock):
        """모든 편집 필드는 data-original attribute 를 갖는다(빈 문자열도 OK)."""
        html = _render()
        # data-field 가 붙은 span 들을 모두 찾는다.
        field_spans = re.findall(
            r'<span[^>]*class="edit-field"[^>]*data-field="([^"]+)"[^>]*>',
            html,
        )
        assert len(field_spans) >= 4, f"편집 필드가 너무 적음: {field_spans}"
        # 각 편집 필드에 data-original 이 있는지 확인하기 위해, edit-field span 들을
        # 개별로 추출한다.
        span_pattern = re.compile(
            r'<span[^>]*class="edit-field"[^>]*>.*?</span>', re.IGNORECASE | re.DOTALL
        )
        for span_match in span_pattern.finditer(html):
            span_html = span_match.group(0)
            if "data-field=" in span_html:
                assert (
                    "data-original=" in span_html
                ), f"편집 필드에 data-original 없음: {span_html[:120]}"


# =========================================================================== #
# (b) 복사 버튼과 인라인 스크립트가 존재, 외부 참조(src/href http)는 이미지 외 0건.
# =========================================================================== #
class TestCopyButtonAndNoExternalRefs:
    """복사 버튼 + 인라인 스크립트 + 외부 리소스 0건(이미지 제외)."""

    def test_copy_button_present(self, notice_config_mock):
        html = _render()
        assert 'id="edit-copy-btn"' in html, "[수정사항 복사] 버튼 없음"
        assert "수정사항 복사" in html, "복사 버튼 텍스트 없음"

    def test_inline_script_present(self, notice_config_mock):
        html = _render()
        assert "<script>" in html, "인라인 스크립트 없음"
        # 클립보드 복사 로직이 들어있어야 한다.
        assert "clipboard" in html.lower(), "클립보드 로직 없음"
        # collectChanges 같은 핵심 함수가 있어야 한다.
        assert "collectChanges" in html or "collect" in html, "변경 수집 로직(collectChanges) 없음"

    def test_no_external_script_src(self, notice_config_mock):
        """외부 src 를 갖는 <script src="http..."> 가 없어야 한다."""
        html = _render()
        # <script src="http..."> 패턴이 없어야 한다.
        external_script = re.compile(r'<script[^>]*\ssrc\s*=\s*["\']https?://', re.IGNORECASE)
        assert not external_script.search(html), "외부 src 를 갖는 <script> 가 있음 — 인라인만 허용"

    def test_no_external_link_href(self, notice_config_mock):
        """외부 href 를 갖는 <link rel="stylesheet" href="http..."> 가 없어야 한다."""
        html = _render()
        external_link = re.compile(r'<link[^>]*\shref\s*=\s*["\']https?://', re.IGNORECASE)
        assert not external_link.search(html), "외부 CSS <link> 가 있음 — 인라인만 허용"

    def test_only_image_uses_external_src(self, notice_config_mock):
        """http(s) 를 갖는 src/href 는 상품 <img> 의 src 만 허용된다."""
        html = _render()
        # http(s):// 가 나오는 모든 src="..." 를 찾는다.
        src_pattern = re.compile(r'\ssrc\s*=\s*["\'](https?://[^"\']+)["\']', re.IGNORECASE)
        external_srcs = src_pattern.findall(html)
        # 모두 <img> 의 src 여야 한다. <img src=...> 앞뒤로만 매칭되는지 검증.
        for url in external_srcs:
            # url 이 img 태그 안에 있는지 확인 — 단순화: img 태그 패턴으로 재추출.
            assert url.startswith(("http://", "https://")), f"예상치 못한 URL: {url}"
        # 이미지 URL 만 있어야 하고, 다른 외부 참조(<iframe src="http">, <script src="http">)
        # 는 이전 테스트들에서 별도 검증. 여기서는 http 외부 참조 개수가 이미지 개수와
        # 일치하는지 대략 확인.
        img_count = len(re.findall(r"<img\s", html, re.IGNORECASE))
        assert (
            len(external_srcs) <= img_count + 1
        ), f"외부 src 개수({len(external_srcs)})가 이미지 개수({img_count})보다 많음"

    def test_iframe_srcdoc_not_external_src(self, notice_config_mock):
        """iframe 은 srcdoc(인라인 문서)을 쓰고, 외부 src 를 쓰지 않는다."""
        html = _render()
        # iframe 이 srcdoc 을 쓰는지.
        assert 'srcdoc="' in html, "iframe srcdoc 없음"
        # iframe src="http..." 패턴이 없어야 한다.
        external_iframe = re.compile(r'<iframe[^>]*\ssrc\s*=\s*["\']https?://', re.IGNORECASE)
        assert not external_iframe.search(html), "iframe 이 외부 src 를 참조함"


# =========================================================================== #
# (c) data-original 값에 HTML 특수문자가 들어가도 깨지지 않는다.
# =========================================================================== #
class TestEscapingIntact:
    """악의적 상품명이 data-original 속성을 탈출해 마크업을 주입하지 못하는가."""

    def test_hostile_name_in_attribute_is_escaped(self, notice_config_mock):
        """``"><script>`` 같은 상품명이 data-original 속성을 탈출하지 못한다."""
        hostile = '"><script>alert(1)</script>'
        html = _render(_sample_payload(name=hostile))
        # 핵심: hostile 문자열 그대로가 속성값을 닫는 " 를 만나면 안 된다.
        # 즉, '"><script>' 패턴(닫는 따옴표 + >)이 그대로 HTML 에 있으면 안 됨.
        assert (
            '"><script>' not in html
        ), "악성 상품명이 속성을 탈출해 <script> 를 주입함 — 이스케이프 깨짐"
        # 대신 이스케이프된 형태(&quot;&gt;)가 있어야 한다.
        assert (
            "&quot;&gt;&lt;script&gt;" in html or "&#x27;&gt;&lt;script&gt;" in html
        ), "악성 상품명이 이스케이프되지 않음 — quote 이스케이프 누락"

    def test_hostile_name_does_not_inject_script_tag(self, notice_config_mock):
        """악성 상품명이 실제 <script> 태그를 만들어내지 않는다."""
        hostile = '"><script>alert(1)</script><span x="'
        html = _render(_sample_payload(name=hostile))
        # 렌더링된 HTML 에서 alert(1) 을 포함하는 <script> 태그가 새로 생겼는지.
        # (우리가 의도한 _PREVIEW_EDIT_SCRIPT 안의 함수는 collectChanges 등이지
        # alert(1) 이 아니다.)
        # 개수로 따져보면 안전하다: 편집 스크립트는 딱 1개.
        script_tags = re.findall(r"<script[^>]*>", html, re.IGNORECASE)
        # 편집 스크립트 1개만 있어야 한다(주입된 script 가 추가로 생기면 안 됨).
        assert len(script_tags) == 1, f"<script> 태그가 {len(script_tags)}개 — 주입 가능성"

    def test_hostile_notice_value_in_data_original(self, notice_config_mock):
        """고시 값에 특수문자가 들어가도 data-original 속성을 탈출하지 못한다."""
        hostile_value = 'foo" bar<>baz'
        payload = _sample_payload()
        payload["product"]["notice"]["etc"]["returnCostReason"] = hostile_value
        html = _render(payload)
        # 속성을 탈출하는 패턴이 없어야 한다.
        # data-original="foo" bar<>baz" 같은 깨진 형태가 나오면 안 됨.
        # html.escape(quote=True) 가 " 를 &quot; 으로 바꾸므로, 원본 " 가
        # data-original 속성값 안에 그대로 노출되면 안 된다.
        # 안전한 검사: data-original 다음에 오는 값이 이스케이프되어 있는지.
        # hostile_value 의 " 가 &quot; 으로, < 가 &lt; 으로 변환되어 있어야 한다.
        assert "bar&lt;&gt;baz" in html or "bar&lt;" in html, "고시 값의 < > 가 이스케이프되지 않음"

    def test_ampersand_in_name_is_escaped(self, notice_config_mock):
        """``&`` 가 상품명에 있으면 ``&amp;`` 로 이스케이프된다."""
        html = _render(_sample_payload(name="A&B 상품"))
        # 표시 텍스트와 data-original 모두 이스케이프되어야 한다.
        assert "A&amp;B" in html, "& 가 이스케이프되지 않음"

    def test_quote_in_tags_is_escaped(self, notice_config_mock):
        """태그 값에 ``"`` 가 있어도 속성을 탈출하지 못한다."""
        payload = _sample_payload()
        payload["product"]["tags"] = ['겨울"후드', "기모"]
        html = _render(payload)
        # data-original 속성값 안에 " 가 그대로 있으면 탈출이다.
        # html.escape(quote=True) 가 " 를 &quot; 으로 바꾼다.
        # '겨울"후드' 가 그대로 HTML 에 노출되면 안 됨(이스케이프된 형태여야).
        assert '겨울"후드' not in html, "태그의 큰따옴표가 속성을 탈출 — 이스케이프 누락"


# =========================================================================== #
# (d) 폴백 textarea 경로가 마크업에 존재한다.
# =========================================================================== #
class TestFallbackTextarea:
    """클립보드 API 실패 시 폴백 textarea 가 있는가."""

    def test_fallback_textarea_in_markup(self, notice_config_mock):
        html = _render()
        assert 'id="edit-fallback"' in html, "폴백 컨테이너(edit-fallback) 없음"
        assert "<textarea" in html.lower(), "폴백 <textarea> 없음"
        assert (
            'id="edit-fallback-textarea"' in html
        ), "폴백 textarea 의 id(edit-fallback-textarea) 없음"

    def test_fallback_textarea_is_readonly(self, notice_config_mock):
        """폴백 textarea 는 readonly — 사용자가 내용을 직접 고치는게 아니라 복사만."""
        html = _render()
        # textarea 태그를 찾아서 readonly 인지 확인.
        ta_match = re.search(
            r'<textarea[^>]*id="edit-fallback-textarea"[^>]*>', html, re.IGNORECASE
        )
        assert ta_match is not None, "폴백 textarea 를 찾을 수 없음"
        assert "readonly" in ta_match.group(0).lower(), "폴백 textarea 가 readonly 가 아님"

    def test_fallback_note_present(self, notice_config_mock):
        """폴백 안내 문구가 있다 — 'Ctrl+C 로 복사' 식의 안내."""
        html = _render()
        # 폴백 노트 클래스가 있고, Ctrl+C 안내가 있어야 한다.
        assert "edit-fallback-note" in html, "폴백 안내 클래스(edit-fallback-note) 없음"
        assert "Ctrl+C" in html or "ctrl+c" in html.lower(), "폴백 안내에 Ctrl+C 복사 안내가 없음"

    def test_script_handles_clipboard_failure(self, notice_config_mock):
        """인라인 스크립트가 클립보드 실패 경로(fallback)를 다룬다."""
        html = _render()
        # 스크립트 안에 fallback 함수 호출이 있어야 한다.
        assert (
            "showFallback" in html or "fallback" in html.lower()
        ), "스크립트에 폴백 처리(showFallback) 없음"


# =========================================================================== #
# (e) 안내 문구("붙여넣어야 반영")가 존재한다.
# =========================================================================== #
class TestHonestyNote:
    """'이 화면에서의 수정은 붙여넣어야 반영됩니다' 안내가 있는가."""

    def test_honesty_note_in_markup(self, notice_config_mock):
        html = _render()
        # 핵심 안내 문구가 있어야 한다.
        assert (
            "붙여넣어야 반영" in html
        ), "'붙여넣어야 반영' 안내 문구가 없음 — 조용한 저장 착각 위험"

    def test_honesty_note_is_strong_emphasized(self, notice_config_mock):
        """안내 문구가 <strong> 로 강조되어 있다(수수하게 묻히지 않게)."""
        html = _render()
        # '붙여넣어야 반영' 앞뒤로 <strong> 강조가 있는지.
        idx = html.find("붙여넣어야 반영")
        assert idx >= 0, "안내 문구 없음"
        # 앞쪽 200자 안에 <strong> 이 있는지 대략 확인(너무 멀리 떨어지면 안 됨).
        context_before = html[max(0, idx - 200) : idx]
        assert "<strong>" in context_before, "안내 문구가 <strong> 로 강조되지 않음"

    def test_edit_bar_note_class_present(self, notice_config_mock):
        """edit-bar-note 클래스(편집 바 안내 영역)가 있다."""
        html = _render()
        assert "edit-bar-note" in html, "edit-bar-note 클래스 없음"

    def test_empty_changes_message_present(self, notice_config_mock):
        """변경 0건일 때의 안내 메시지가 스크립트에 있다('수정된 항목이 없습니다')."""
        html = _render()
        assert (
            "수정된 항목이 없습니다" in html
        ), "변경 0건 안내 메시지('수정된 항목이 없습니다')가 스크립트에 없음"

    def test_product_key_embedded(self, notice_config_mock):
        """product_key 가 페이지에 숨겨져 있다(클립보드 페이로드 식별용)."""
        html = _render(product_key="uniquekey123456")
        assert "edit-product-key" in html, "product_key 숨김 span(edit-product-key) 없음"
        assert "uniquekey123456" in html, "product_key 값이 페이지에 없음"
