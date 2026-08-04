# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""등록 전 미리보기 HTML 파일을 만든다.

본 모듈은 prepared payload 에 있는 값을 읽어 **단일 HTML 파일**로
렌더한다. 파일은 외부 CSS/JS/웹폰트를 일절 불러오지 않는다 — 스타일과
스크립트는 모두 인라인으로 들어간다. **유일한 예외는 상품 이미지다**:
업로드된 이미지는 네이버 CDN 에만 존재하므로 ``<img src>`` 로 CDN 에서
불러온다. 사진이 실제 상품과 일치하는지는 사람만 잡을 수 있으므로,
미리보기에서 반드시 렌더해야 한다. 상세 HTML 본문은 prepared 의
``detail_html`` 을 ``<iframe srcdoc>`` 으로 끼워넣는다.

본 모듈은 **읽기 전용 화면을 제공할 뿐**이다. 어떤 값도 검수·판정·수정하지
않는다. 미리보기가 있다고 해서 등록 내용이 검토되었다는 뜻은 아니다 —
판매자가 직접 눈으로 확인하는 것을 돕는 화면일 뿐이다.

의존 방향(DAG): ``common``, ``naver_client``, ``qa_agents``, ``text_props``
(상위) → ``preview`` (본 모듈). 본 모듈은 상위 모듈만 import 한다.
``preview`` → ``register`` 순서로 하위 모듈이 import 한다.
"""

from __future__ import annotations

import datetime
import html
from pathlib import Path
from typing import Any

from . import naver_client, qa_agents

# ---------------------------------------------------------------------------
# 미리보기 파일 경로.
#
# prepared payload 와 **같은 디렉터리**에 둔다(새 디렉터리 규약을 만들지
# 않는다). ``register._prepared_item_dir`` 규약을 따라 같은 product_key
# 하위의 ``preview.html``.
# ---------------------------------------------------------------------------


def _preview_path(product_key: str) -> Path:
    """``product_key`` 에 해당하는 미리보기 HTML 파일 경로.

    prepared payload 디렉터리 규약을 그대로 따른다(새 규약 금지).
    """
    from . import register as _register_mod

    return _register_mod._prepared_item_dir(product_key) / "preview.html"


# ---------------------------------------------------------------------------
# 고시 정보 표 빌딩.
#
# 등록 단계가 만들 페이로드(``naver_client.build_payload``)와 *동일한 해석*으로
# 고시 값을 산출한다 — 준비 단계와 등록 단계가 서로 다른 고시 값을 보는
# 불일치를 만들지 않는다. 빌더가 config 폴백으로 채운 값은 출처 표시와 함께
# 드러낸다(조용한 자동 채움 금지).
# ---------------------------------------------------------------------------


def _payload_notice_type(payload: dict[str, Any]) -> str:
    """페이로드에서 신고되는 고시 타입을 추출한다 (대문자)."""
    try:
        notice = (
            payload.get("originProduct", {})
            .get("detailAttribute", {})
            .get("productInfoProvidedNotice", {})
        )
        return str(notice.get("productInfoProvidedNoticeType") or "").strip().upper()
    except (AttributeError, TypeError):
        return ""


def _notice_type_label(notice_type: str) -> str:
    """고시 타입 코드를 한국어 라벨로. 알 수 없으면 코드 그대로."""
    spec = qa_agents._notice_type_spec(notice_type)
    if isinstance(spec, dict):
        label = spec.get("label_ko") or spec.get("type") or notice_type
        return str(label)
    return notice_type or "(알 수 없음)"


def _collect_notice_rows(
    product: dict[str, Any],
    notice_filled_from_config: list[str],
) -> list[dict[str, str]]:
    """고시 값을 (필드, 값, 출처) 행 리스트로 모은다.

    출처 표시:
      - ``"사용자 입력"`` — 상품 입력에 명시된 값.
      - ``"설정 기본값"`` — config 의 smartstore_notice_defaults 에서 채워진 값
        (``notice_filled_from_config`` 목록에 있는 필드).
      - ``"미제공"`` — 값이 비어 있거나 사용자·config 어디에도 없는 필드.
    """
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(field: str, value: Any, source: str) -> None:
        text = "" if value is None else str(value).strip()
        if field in seen:
            return
        seen.add(field)
        rows.append({"field": field, "value": text, "source": source})

    cfg_notice = naver_client._notice_config()
    cfg_filled = {str(f) for f in (notice_filled_from_config or [])}

    # 공통 고시 필드(본문에 들어가는 camelCase 필드)를 우선 모은다.
    user_notice = product.get("notice") if isinstance(product, dict) else None
    user_body: dict[str, Any] = {}
    if isinstance(user_notice, dict):
        for node_key in ("etc", "wear", "furniture", "shoes", "bag"):
            body = user_notice.get(node_key)
            if isinstance(body, dict):
                user_body.update(body)
        # 사용자가 준 최상위 키도 후보로.
        for key, value in user_notice.items():
            if isinstance(value, str | int | bool):
                user_body.setdefault(str(key), value)

    # 공통 5필드 + 고시 본문에 있는 모든 필드를 행으로.
    candidate_fields = list(naver_client._NOTICE_COMMON_FIELDS)
    for key in user_body:
        if key not in candidate_fields:
            candidate_fields.append(str(key))

    for field in candidate_fields:
        if field in user_body:
            _add(field, user_body[field], "사용자 입력")
        elif field in cfg_filled:
            cfg_value = cfg_notice.get(field)
            if cfg_value:
                _add(field, cfg_value, "설정 기본값")
            else:
                _add(field, "", "미제공")
        else:
            _add(field, "", "미제공")

    return rows


# ---------------------------------------------------------------------------
# HTML 빌딩. 외부 리소스를 전혀 참조하지 않는 인라인 CSS 만 쓴다.
# ---------------------------------------------------------------------------

_PREVIEW_CSS = """
body{margin:0;padding:24px;background:#f5f5f5;font-family:-apple-system,
  BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  color:#222;font-size:14px;line-height:1.6}
.preview-wrap{max-width:900px;margin:0 auto;background:#fff;padding:32px;
  border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,0.08)}
.preview-header{border-bottom:2px solid #333;padding-bottom:16px;margin-bottom:24px}
.preview-title{font-size:22px;font-weight:700;margin:0 0 8px}
.preview-meta{color:#555;font-size:13px}
.preview-meta strong{color:#222}
.preview-status{display:inline-block;padding:2px 10px;border-radius:12px;
  font-size:12px;font-weight:600;margin-left:8px}
.preview-status-sale{background:#e6f4ea;color:#137333}
.preview-status-suspension{background:#fce8e6;color:#a50e0e}
.preview-status-banner{padding:14px 18px;border-radius:8px;margin-bottom:20px;
  font-size:18px;font-weight:700;display:flex;align-items:center;gap:10px}
.preview-status-banner-sale{background:#e6f4ea;color:#137333;
  border:2px solid #137333}
.preview-status-banner-suspension{background:#fce8e6;color:#a50e0e;
  border:2px solid #a50e0e}
.preview-status-banner-icon{font-size:24px}
.preview-section{margin:24px 0}
.preview-section h2{font-size:16px;font-weight:600;margin:0 0 12px;
  padding-bottom:6px;border-bottom:1px solid #e0e0e0}
.preview-price{font-size:26px;font-weight:700;color:#137333}
.preview-images{display:grid;grid-template-columns:repeat(auto-fill,
  minmax(180px,1fr));gap:12px}
.preview-image{position:relative;border:1px solid #e0e0e0;border-radius:6px;
  overflow:hidden;background:#fafafa;min-height:120px;display:flex;
  align-items:center;justify-content:center;color:#999;font-size:12px;
  word-break:break-all;padding:0;text-align:center}
.preview-image img{width:100%;height:auto;display:block;object-fit:contain;
  max-height:320px}
.preview-badge{position:absolute;top:6px;left:6px;background:#137333;color:#fff;
  font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px}
.notice-table{width:100%;border-collapse:collapse;font-size:13px}
.notice-table th,.notice-table td{border:1px solid #e0e0e0;padding:8px 10px;
  text-align:left;vertical-align:top}
.notice-table th{background:#f5f5f5;font-weight:600;width:30%}
.source-user{color:#137333;font-size:11px}
.source-config{color:#a50e0e;font-size:11px;font-weight:600}
.source-missing{color:#a50e0e;background:#fff3cd;font-weight:600}
.missing-row td{background:#fff8e1}
.missing-banner{background:#fff3cd;border:1px solid #ffe082;color:#7a5900;
  padding:10px 14px;border-radius:6px;margin:8px 0;font-size:13px}
.detail-frame{border:1px solid #e0e0e0;border-radius:6px;overflow:hidden}
.detail-frame iframe{width:100%;min-height:400px;border:0;display:block}
.preview-note{margin-top:32px;padding:14px;background:#eef6ff;border-radius:6px;
  color:#1a4d8f;font-size:12px;line-height:1.7}
.preview-note strong{color:#0b3d7a}
"""


def _status_badge(status: str) -> str:
    """판매상태 배지 HTML."""
    s = str(status or "").strip().upper()
    if s == "SUSPENSION":
        return (
            '<span class="preview-status preview-status-suspension">' "판매중지(SUSPENSION)</span>"
        )
    if s == "SALE":
        return '<span class="preview-status preview-status-sale">판매중(SALE)</span>'
    return f'<span class="preview-status">{html.escape(s or "?")}</span>'


def _status_banner(status: str) -> str:
    """판매상태 배너 HTML — 페이지 최상단에서 가장 눈에 띄는 위치.

    판매중지로 등록하려는데 판매중으로 나가는 조용한 잘못된 상태를
    사람이 한눈에 잡을 수 있게 한다.
    """
    s = str(status or "").strip().upper()
    if s == "SUSPENSION":
        return (
            '<div class="preview-status-banner preview-status-banner-suspension">'
            '<span class="preview-status-banner-icon">&#9888;</span>'
            "<span>이 상품은 <strong>판매중지(SUSPENSION)</strong> 상태로 "
            "등록됩니다. 즉시 노출되지 않습니다.</span>"
            "</div>"
        )
    if s == "SALE":
        return (
            '<div class="preview-status-banner preview-status-banner-sale">'
            '<span class="preview-status-banner-icon">&#9989;</span>'
            "<span>이 상품은 <strong>판매중(SALE)</strong> 상태로 등록됩니다. "
            "등록 즉시 노출됩니다.</span>"
            "</div>"
        )
    return (
        '<div class="preview-status-banner">'
        f'<span>판매상태: {html.escape(s or "알 수 없음")}</span>'
        "</div>"
    )


def _render_notice_table(rows: list[dict[str, str]]) -> str:
    """고시 정보 표 HTML."""
    if not rows:
        return '<p class="preview-meta">(고시 정보 없음)</p>'
    parts = [
        '<table class="notice-table">',
        "<thead><tr><th>고시 필드</th><th>값</th><th>출처</th></tr></thead>",
        "<tbody>",
    ]
    missing_count = 0
    for row in rows:
        field = row["field"]
        value = row["value"]
        source = row["source"]
        is_missing = not value
        if is_missing:
            missing_count += 1
        cls = " missing-row" if is_missing else ""
        parts.append(f'<tr class="notice-row{cls}">')
        parts.append(f"<th>{html.escape(field)}</th>")
        if value:
            parts.append(f"<td>{html.escape(value)}</td>")
        else:
            parts.append("<td><em>(비어 있음)</em></td>")
        if source == "사용자 입력":
            parts.append('<td><span class="source-user">사용자 입력</span></td>')
        elif source == "설정 기본값":
            parts.append('<td><span class="source-config">설정 기본값</span></td>')
        else:
            parts.append('<td><span class="source-missing">미제공</span></td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")
    if missing_count > 0:
        parts.append(
            f'<div class="missing-banner">필수 고시 항목 중 {missing_count}개가 '
            "비어 있습니다. 등록 단계의 컴플라이언스 검사가 이 필드를 요구하면 "
            "등록이 거부됩니다.</div>"
        )
    return "\n".join(parts)


def _render_images(listing_urls: list[str]) -> str:
    """이미지 갤러리 HTML. CDN URL 을 ``<img src>`` 로 렌더한다.

    미리보기의 목적은 판매자가 **사진을 직접 눈으로 확인**하는 것이다 — 사진이
    실제 상품과 일치하는지는 오직 사람만 잡을 수 있다. 업로드된 이미지는 네이버
    CDN 에만 있으므로 ``<img src>`` 로 CDN 에서 불러온다. 이것은 허용된 유일한
    외부 리소스이며, 스타일시트·폰트·스크립트는 여전히 인라인만 쓴다.

    첫 번째 이미지가 대표 이미지이며 '대표' 배지로 표시한다.
    """
    if not listing_urls:
        return '<p class="preview-meta">(이미지 없음)</p>'
    parts = ['<div class="preview-images">']
    for idx, url in enumerate(listing_urls):
        safe_url = html.escape(str(url), quote=True)
        badge = ""
        if idx == 0:
            badge = '<span class="preview-badge">대표</span>'
        parts.append(
            f'<div class="preview-image">{badge}'
            f'<img src="{safe_url}" alt="상품 이미지 {idx + 1}" loading="lazy" '
            f"onerror=\"this.style.display='none'\" />"
            f"</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


def render_preview_html(
    payload: dict[str, Any],
    *,
    api_payload: dict[str, Any] | None = None,
) -> str:
    """prepared payload 로부터 미리보기 HTML 문자열을 만든다.

    본 함수는 외부 CSS/JS/폰트를 참조하지 않는 단일 HTML 문자열을 반환한다.
    상품 이미지는 네이버 CDN 의 ``<img src>`` 로 렌더된다 — 사진이 실제
    상품과 일치하는지 사람이 확인하는 것이 이 화면의 핵심 목적이다.
    상세 HTML(``payload.detail_html``)은 ``<iframe srcdoc="...">`` 으로
    끼워넣는다 — iframe 의 srcdoc 은 인라인 문서이므로 네트워크 요청을
    일으키지 않는다.

    Args:
        payload: prepared payload dict.
        api_payload: 등록 단계가 만들 페이로드(``naver_client.build_payload``
            결과). 고시 타입·출처 표시를 위해 쓴다. 없으면 payload 에서
            최소 정보만 읽는다.

    Returns:
        완전한 HTML 문서 문자열.
    """
    product = payload.get("product") if isinstance(payload.get("product"), dict) else {}
    name = str(product.get("name") or "")
    sale_price = product.get("salePrice")
    category_id = str(product.get("categoryId") or "")
    status = str(payload.get("status") or product.get("status") or "SALE")

    images_block = payload.get("images") if isinstance(payload.get("images"), dict) else {}
    listing_urls = [
        str(u).strip()
        for u in (images_block.get("listing_urls") or [])
        if isinstance(u, str) and u.strip()
    ]

    detail_html = str(payload.get("detail_html") or "")

    # 고시 타입·출처 표시를 위해 등록 단계 페이로드에서 정보를 읽는다.
    notice_type = ""
    notice_filled: list[str] = []
    if api_payload is not None:
        notice_type = _payload_notice_type(api_payload)
        notice_filled = list(api_payload.get("notice_filled_from_config") or [])
    notice_rows = _collect_notice_rows(product, notice_filled)
    notice_type_label = _notice_type_label(notice_type) if notice_type else "(미확정)"

    # srcdoc 은 HTML 의 " 속성을 escape 해야 한다.
    srcdoc = html.escape(detail_html, quote=True)
    # 생성 시각(UTC).
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    parts = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        "<title>",
        html.escape(name or "상품 미리보기"),
        " 미리보기</title>",
        "<style>",
        _PREVIEW_CSS,
        "</style>",
        "</head>",
        "<body>",
        '<div class="preview-wrap">',
    ]

    # 판매상태 배너 — 가장 눈에 띄는 최상단 위치.
    # 판매중지로 올리려는데 판매중으로 나가는 조용한 잘못된 상태를
    # 사람이 한눈에 잡을 수 있게 한다.
    parts.append(_status_banner(status))

    # 헤더: 상품명·판매상태.
    parts.append('<div class="preview-header">')
    parts.append(f'<h1 class="preview-title">{html.escape(name or "(상품명 없음)")}</h1>')
    parts.append('<div class="preview-meta">')
    parts.append(f"<strong>카테고리 ID:</strong> {html.escape(category_id or '(미지정)')} ")
    parts.append(_status_badge(status))
    parts.append("<br />")
    if sale_price is not None:
        try:
            price_text = f"{int(sale_price):,}원"
        except (TypeError, ValueError):
            price_text = str(sale_price)
        parts.append(f'<span class="preview-price">{html.escape(price_text)}</span>')
    else:
        parts.append('<span class="preview-price">(가격 미지정)</span>')
    parts.append("</div>")
    parts.append("</div>")  # preview-header

    # 이미지 섹션.
    parts.append('<div class="preview-section">')
    parts.append("<h2>이미지</h2>")
    parts.append(_render_images(listing_urls))
    parts.append("</div>")

    # 상세 페이지 섹션 (iframe srcdoc — 외부 리소스 참조 없음).
    parts.append('<div class="preview-section">')
    parts.append("<h2>상세 페이지 (렌더 결과)</h2>")
    if detail_html:
        parts.append('<div class="detail-frame">')
        parts.append(f'<iframe srcdoc="{srcdoc}"></iframe>')
        parts.append("</div>")
    else:
        parts.append('<p class="preview-meta">(상세 HTML 없음)</p>')
    parts.append("</div>")

    # 고시 정보 섹션.
    parts.append('<div class="preview-section">')
    parts.append("<h2>상품정보제공고시</h2>")
    parts.append(
        '<p class="preview-meta"><strong>고시 타입:</strong> '
        f"{html.escape(notice_type_label)}"
        f" ({html.escape(notice_type or '미확정')})</p>"
    )
    parts.append(_render_notice_table(notice_rows))
    parts.append(
        '<p class="preview-meta"><span class="source-config">설정 기본값</span>'
        " 표시가 있는 필드는 판매자가 입력하지 않았지만 config 의 "
        "smartstore_notice_defaults 에서 자동으로 채워진 값입니다. "
        "의도한 값인지 확인하세요.</p>"
    )
    parts.append("</div>")

    # 안내문.
    parts.append('<div class="preview-note">')
    parts.append("<strong>이 화면은 판매자가 직접 눈으로 확인하는 용도입니다.</strong><br />")
    parts.append(
        "본 도구는 이 화면을 제공할 뿐 등록 내용을 검수하지 않습니다. "
        "사진이 실제 상품과 일치하는지, 문구가 자연스러운지, "
        "옵션 구성이 적절한지는 판매자가 직접 판단해야 합니다. "
        "이 미리보기를 보았다는 사실은 누군가가 화면을 봤다는 "
        "선언일 뿐, 내용이 올바르다는 증명이 아닙니다."
    )
    parts.append(
        f'<br /><span style="color:#5a7da8">생성 시각(UTC): {html.escape(generated_at)}</span>'
    )
    parts.append("</div>")

    parts.append("</div>")  # preview-wrap
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def write_preview_html(
    product_key: str,
    payload: dict[str, Any],
    *,
    api_payload: dict[str, Any] | None = None,
) -> Path:
    """미리보기 HTML 을 디스크에 쓰고 경로를 반환한다.

    prepared payload 디렉터리 규약 하위의 ``preview.html`` 로 쓴다.

    Args:
        product_key: prepared payload 의 product_key.
        payload: prepared payload dict.
        api_payload: 등록 단계 페이로드(선택). 고시 출처 표시에 쓴다.

    Returns:
        쓴 파일의 경로.
    """
    path = _preview_path(product_key)
    html_doc = render_preview_html(payload, api_payload=api_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")
    return path


__all__ = [
    "render_preview_html",
    "write_preview_html",
]
