# -*- coding: utf-8 -*-
"""상세 페이지 HTML 렌더 (T-201d).

본 모듈은 ``templates`` 가 제공하는 상세 템플릿·CSS·레이아웃 상수를 사용해
최종 상세 HTML 문자열을 만든다. 원본 렌더 함수는 함수명에 금칙어가 포함되어
있어 이식하지 않는다(작업지시 요구 1). 대신 새 이름으로 구현하되 레이아웃
규칙(폭·마진·섹션 순서·CSS 조각)은 원본 상수를 그대로 쓴다.

제품 전제:
  - 텍스트 추론은 MCP 클라이언트의 LLM 이 담당한다.
  - 본 서버는 결정론 검증만 책임진다(최종 방어선).
  - 따라서 LLM 판단이 필요한 부분(카피 문구 확정 등)은 ``common._llm_hint``
    위임 디스크립터로 내보내고, 여기서 임의 문구를 창작하지 않는다.

의존 방향(DAG): ``common``, ``text_props``, ``templates`` (상위) →
``detail_render`` (본 모듈). ``detail_render`` → ``register`` 순서로
하위 모듈이 import 한다. 본 모듈은 상위 모듈만 import 한다.

입력: 상품 정보 dict + 이미지 CDN URL 리스트 + 옵션표.
출력: HTML 문자열 — 단, LLM 판단이 필요한 카피는 ``llm_hint`` 위임
디스크립터를 인라인 placeholder 로 남기고, 최종 HTML 확정은 위임 회신 후
호출자가 다시 합성하도록 구조화한다.
"""
from __future__ import annotations

from typing import Any

from . import common
from . import templates
from .text_props import DETAIL_RENDER_WIDTH, _hesc, _detail_safe_text


# ---------------------------------------------------------------------------
# 섹션 순서(원본 레이아웃 상수를 그대로 사용).
#
# 원본 상세 페이지는 고정된 섹션 순서를 가졌다. 본 이식판도 동일 순서로
# HTML 을 조립한다. 순서를 바꾸면 스캐너/게이트가 레이아웃 위반으로 잡을 수
# 있다. 단, 각 섹션의 *내용* 이 빈 값이면 해당 섹션은 생략한다(사용자가
# 주지 않은 사실을 채우지 않는다 — 작업지시 요구 1).
# ---------------------------------------------------------------------------
_SECTION_ORDER = ("hero", "intro", "specs", "options", "notice", "footer")


def _coerce_str_list(values, *, label):
    """문자열 리스트로 정규화. 비문자열/빈 항목은 거부하지 않고 건너뛴다."""
    if values is None:
        return []
    if isinstance(values, str):
        # 문자열 단독 입력은 1장으로 간주하지 않는다 — 리스트가 아니면 빈 리스트.
        return []
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for v in values:
        if isinstance(v, str) and v.strip():
            out.append(v)
    return out


def _coerce_options(options):
    """옵션표를 검증된 리스트로 정규화.

    각 옵션은 dict 형태여야 한다. 빈 옵션표는 빈 리스트로 둔다.
    """
    if options is None:
        return []
    if not isinstance(options, (list, tuple)):
        return []
    out = []
    for opt in options:
        if isinstance(opt, dict):
            out.append(opt)
    return out


def _section_hero(image_urls):
    """대표/히어로 이미지 섹션 HTML 조각.

    원본 레이아웃: 단일 컬럼, 첫 번째 이미지가 대표.
    """
    if not image_urls:
        return ""
    urls = [_hesc(u) for u in image_urls]
    parts = [
        '<section class="detail-hero photo-stack">',
    ]
    for idx, url in enumerate(urls):
        cls = "hero" if idx == 0 else "detail-band"
        parts.append(
            f'<div class="photo-block {cls}">'
            f'<img src="{url}" alt="detail-image-{idx + 1}" />'
            f'</div>'
        )
    parts.append('</section>')
    return "\n".join(parts)


def _section_intro(product):
    """도입부 섹션 — 상품명/요약. 빈 값이면 빈 문자열.

    사용자가 주지 않은 사실을 채우지 않는다(작업지시 요구 1).
    """
    if not isinstance(product, dict):
        return ""
    name = _detail_safe_text(product.get("name") or product.get("title_ko") or "")
    summary = _detail_safe_text(product.get("summary") or product.get("desc") or "")
    if not name and not summary:
        return ""
    parts = ['<section class="detail-intro">']
    if name:
        parts.append(f'<h2 class="detail-title">{_hesc(name)}</h2>')
    if summary:
        parts.append(f'<p class="detail-summary">{_hesc(summary)}</p>')
    parts.append('</section>')
    return "\n".join(parts)


def _section_specs(product):
    """스펙/속성 섹션 — product.props 등에서 읽는다. 빈 값이면 생략."""
    if not isinstance(product, dict):
        return ""
    props = product.get("props") or product.get("attributes")
    if not props:
        return ""
    rows = []
    if isinstance(props, dict):
        for key, value in props.items():
            k = _detail_safe_text(key)
            v = _detail_safe_text(value)
            if k and v:
                rows.append((k, v))
    elif isinstance(props, (list, tuple)):
        for item in props:
            if isinstance(item, dict):
                k = _detail_safe_text(item.get("name") or item.get("label") or "")
                v = _detail_safe_text(item.get("value") or item.get("text") or "")
                if k and v:
                    rows.append((k, v))
    if not rows:
        return ""
    parts = [
        '<section class="detail-specs">',
        '<table class="spec-table">',
        "<tbody>",
    ]
    for k, v in rows:
        parts.append(
            f'<tr><th class="spec-key">{_hesc(k)}</th>'
            f'<td class="spec-val">{_hesc(v)}</td></tr>'
        )
    parts.append("</tbody></table></section>")
    return "\n".join(parts)


def _section_options(options):
    """옵션표 섹션. templates.OPTION_GRID_SECTION_CSS 를 사용한다."""
    opts = _coerce_options(options)
    if not opts:
        return ""
    parts = [
        '<section class="detail-options">',
        templates.OPTION_GRID_SECTION_CSS,
        '<div class="options-grid">',
    ]
    for idx, opt in enumerate(opts, start=1):
        label = _detail_safe_text(opt.get("name") or opt.get("label") or "")
        desc = _detail_safe_text(opt.get("desc") or opt.get("description") or "")
        price = opt.get("price")
        price_text = ""
        if price is not None:
            try:
                price_text = f"{int(price):,}"
            except (TypeError, ValueError):
                price_text = ""
        parts.append('<div class="option-card">')
        parts.append(f'<div class="option-badge">{idx}</div>')
        parts.append('<div class="option-card-info">')
        if label:
            parts.append(f'<div class="option-label">{_hesc(label)}</div>')
        if desc:
            parts.append(f'<div class="option-desc">{_hesc(desc)}</div>')
        if price_text:
            parts.append(f'<div class="option-price">{_hesc(price_text)}</div>')
        parts.append("</div></div>")
    parts.append("</div></section>")
    return "\n".join(parts)


def _section_notice(product):
    """고시/안내 섹션 — notice dict 가 있으면 표로. 빈 값이면 생략."""
    if not isinstance(product, dict):
        return ""
    notice = product.get("notice")
    if not isinstance(notice, dict) or not notice:
        return ""
    rows = []
    for key, value in notice.items():
        k = _detail_safe_text(key)
        v = _detail_safe_text(value)
        if k and v:
            rows.append((k, v))
    if not rows:
        return ""
    parts = [
        '<section class="detail-notice">',
        '<table class="notice-table">',
        "<tbody>",
    ]
    for k, v in rows:
        parts.append(
            f'<tr><th>{_hesc(k)}</th><td>{_hesc(v)}</td></tr>'
        )
    parts.append("</tbody></table></section>")
    return "\n".join(parts)


def _wrap_document(body_html, *, image_urls=None):
    """단일 컬럼 락 CSS + body 로 완전 HTML 문서를 만든다."""
    css = templates.DETAIL_SINGLE_COLUMN_LOCK_CSS
    width = DETAIL_RENDER_WIDTH
    parts = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8" />',
        f'<meta name="viewport" content="width=device-width, initial-scale=1" />',
        f"<style>",
        f"body{{margin:0;padding:0;background:#fff;"
        f"font-family:'Pretendard',sans-serif;color:#222}}",
        f".detail-wrap{{max-width:{width}px;margin:0 auto;padding:0}}",
        css,
        "</style>",
        "</head>",
        "<body>",
        '<div class="detail-wrap">',
        body_html,
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)


def render_detail_html(product, image_urls, options=None):
    """상품 정보 + 이미지 CDN URL + 옵션표 로 상세 HTML 문자열을 만든다.

    본 함수는 결정론적이다 — LLM 추론을 호출하지 않는다. 사용자가 주지 않은
    값은 채우지 않고 빈 섹션은 생략한다(작업지시 요구 1). 카피 문구(마케팅
    카피) 확정이 필요한 부분은 호출자가 ``common._llm_hint`` 위임을 통해
    별도로 얻어야 한다 — 본 함수는 직접 LLM 호출하지 않는다.

    Args:
        product: 상품 정보 dict (``name``, ``summary``, ``props``,
            ``notice`` 등을 임의로 포함).
        image_urls: 이미지 CDN URL 문자열 리스트. 첫 번째가 대표 이미지.
            빈 리스트/None 이면 히어로 섹션을 생략한다.
        options: 옵션표 리스트. 각 원소는 dict.

    Returns:
        HTML 문자열 (완전한 ``<!DOCTYPE html>`` 문서). 어떤 섹션도 채울 수
        없으면 최소 뼈대 문서를 반환한다(빈 문자열을 반환하지 않는다 —
        호출자가 등록 페이로드의 ``detail_html`` 자리에 바로 쓸 수 있도록).
    """
    if not isinstance(product, dict):
        product = {}
    urls = _coerce_str_list(image_urls, label="image_urls")
    opts = _coerce_options(options if options is not None else product.get("options"))

    sections = []
    # 섹션 순서는 원본 레이아웃 상수를 따른다.
    hero = _section_hero(urls)
    if hero:
        sections.append(hero)
    intro = _section_intro(product)
    if intro:
        sections.append(intro)
    specs = _section_specs(product)
    if specs:
        sections.append(specs)
    opt_html = _section_options(opts)
    if opt_html:
        sections.append(opt_html)
    notice_html = _section_notice(product)
    if notice_html:
        sections.append(notice_html)

    body = "\n".join(sections)
    return _wrap_document(body, image_urls=urls)


def needs_llm_for_copy(product):
    """카피 문구 LLM 위임 디스크립터를 반환(있으면).

    상세 페이지의 마케팅 카피는 LLM 판단이 필요하다. 본 함수는
    ``common._llm_hint`` 디스크립터를 만들어 호출자(위임 왕복 호스트)에게
    넘긴다. 본 모듈 자체는 LLM 을 호출하지 않는다(작업지시 요구 1:
    "LLM 판단이 필요하면 ``common._llm_hint`` 위임(직접 호출 금지)").

    Returns:
        ``llm_hint`` dict. ``product`` 가 dict 가 아니거나 상품명이 없으면
        ``None``.
    """
    if not isinstance(product, dict):
        return None
    name = str(product.get("name") or product.get("title_ko") or "").strip()
    if not name:
        return None
    return common._llm_hint(
        "detail_copy",
        input={
            "name": _hesc(name),
            "summary": _hesc(str(product.get("summary") or "")),
        },
        instruction=(
            "한국어 상세페이지 카피 문구를 작성하라. 금지 표현(정품/진품/"
            "100%/최고급/프리미엄)은 사용 금지. 사용자가 제공한 정보만 사용하고 "
            "추측으로 채우지 말 것. 결과는 HTML 조각(인라인 카피)으로 반환."
        ),
    )


__all__ = [
    "render_detail_html",
    "needs_llm_for_copy",
]
