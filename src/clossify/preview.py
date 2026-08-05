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

본 모듈은 **판매자가 직접 눈으로 확인하는 화면**을 제공한다. 정확한 값을
이미 아는 수정(상품명·가격·고시 값·태그)은 페이지에서 **직접 편집**할 수 있다.
단, 브라우저에서 연 로컬 파일은 서버로 되돌아갈 수 없다 — 편집한 내용은
**[수정사항 복사] 버튼**으로 클립보드에 담아 채팅에 **한 번 붙여넣어야 반영**된다.
페이지에서 고치기만 하고 닫으면 반영되지 않으며, 인터페이스는 이 사실을
명시적으로 안내한다(조용한 저장 착각 금지).

편집 기능은 이스케이프 규율을 깨지 않는다. 편집 필드의 ``data-original``
속성값은 ``html.escape(value, quote=True)`` 로 이스케이프되어 들어가므로,
악의적 상품명이 속성을 탈출해 마크업을 주입하는 경로가 없다.

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
/* 직접 편집 필드. 정확한 값을 이미 아는 수정은 페이지에서 고치는 쪽이 빠르다.
   변경(원본과 다른) 필드는 테두리/배경으로 조용히 표시한다. */
.edit-field{outline:none;border-radius:3px;padding:1px 4px;margin:-1px -4px;
  border:1px solid transparent;cursor:text;min-width:1ch;display:inline-block;
  white-space:pre-wrap;word-break:break-word}
.edit-field:hover{background:#fafafa;border-color:#e0e0e0}
.edit-field:focus{background:#fff;border-color:#1a73e8}
.edit-field.edit-changed{background:#fff8e1;border-color:#ffe082}
.notice-table .edit-field{display:block}
.edit-bar{background:#fff;border:1px solid #e0e0e0;border-radius:8px;
  padding:12px 16px;margin-top:24px;display:flex;flex-wrap:wrap;gap:10px;
  align-items:center;box-shadow:0 -1px 4px rgba(0,0,0,0.06)}
.edit-bar-note{flex:1 1 320px;color:#5a5a5a;font-size:12px;line-height:1.5}
.edit-bar-note strong{color:#a50e0e}
.edit-copy-btn{background:#137333;color:#fff;border:0;border-radius:6px;
  padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer}
.edit-copy-btn:hover{background:#0f5c2b}
.edit-fallback{display:none;width:100%;margin-top:8px}
.edit-fallback textarea{width:100%;min-height:160px;font-family:monospace;
  font-size:12px;border:1px solid #e0e0e0;border-radius:6px;padding:10px;
  box-sizing:border-box}
.edit-fallback-note{color:#a50e0e;font-size:12px;margin-top:6px}
.edit-status{flex:1 1 100%;font-size:13px;color:#555;min-height:1.2em}
/* 로컬 승인 다리: enable_local_approval 켜짐 + 포트 확정 후에만 노출.
   기본 OFF 이므로 token/port 가 없으면 이 바는 렌더되지 않는다. */
.approval-bar{background:#eef6ff;border:1px solid #1a73e8;border-radius:8px;
  padding:14px 18px;margin-top:16px;display:flex;flex-wrap:wrap;gap:10px;
  align-items:center}
.approval-bar-note{flex:1 1 320px;color:#1a4d8f;font-size:12px;line-height:1.5}
.approval-bar-note strong{color:#0b3d7a}
.approval-btn{border:0;border-radius:6px;padding:10px 18px;font-size:14px;
  font-weight:600;cursor:pointer}
.approval-btn-approve{background:#137333;color:#fff}
.approval-btn-approve:hover{background:#0f5c2b}
.approval-btn-approve-edit{background:#1a73e8;color:#fff}
.approval-btn-approve-edit:hover{background:#1557b0}
.approval-btn:disabled{background:#bbb;cursor:default}
.approval-status{flex:1 1 100%;font-size:13px;color:#1a4d8f;min-height:1.2em}
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
    """고시 정보 표 HTML.

    고시 표의 **값 칸** 은 직접 편집 가능하다(``contenteditable``). 필드명 칸은
    잠금 — 필드명을 바꾸면 모델이 어느 고시 항목인지 못 찾는다. 각 값 칸은
    ``data-field="고시.<field>"`` 와 ``data-original="<이스케이프된 원본값>"``
    을 갖는다. ``data-original`` 은 ``html.escape(value, quote=True)`` 로
    이스케이프되어 들어가므로 악의적 값이 속성을 탈출해 마크업을 주입하는
    경로가 없다.
    """
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
        # 필드명 칸은 잠금 — 편집 불가.
        parts.append(f"<th>{html.escape(field)}</th>")
        # 값 칸은 편집 가능. data-original 은 quote 이스케이프.
        safe_field = html.escape(f"고시.{field}", quote=True)
        safe_original = html.escape(value, quote=True)
        # 표시 텍스트: 빈 값은 placeholder 처럼 보이되 data-original 도 빈 문자열.
        if value:
            display = html.escape(value)
        else:
            display = '<em style="color:#999">(비어 있음 — 클릭해 입력)</em>'
        parts.append(
            "<td>"
            f'<span class="edit-field" contenteditable="true" '
            f'data-field="{safe_field}" data-original="{safe_original}">'
            f"{display}</span></td>"
        )
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


# ---------------------------------------------------------------------------
# 직접 편집: [수정사항 복사] 버튼 + 클립보드 폴백.
#
# 브라우저에서 ``file://`` 로 연 페이지는 MCP 서버로 되돌아갈 수 없다.
# 그래서 편집한 내용을 **클립보드**가 다리가 된다 — 버튼이 바뀐 필드만 모아
# 변경 전→후를 사람이 읽을 수 있고 모델이 파싱할 수 있는 형태로 클립보드에
# 넣는다. 판매자가 그것을 채팅에 한 번 붙여넣으면 모델이 명시 인자로 재호출한다
# (명시 입력 우선 원칙 그대로).
#
# 클립보드 API 는 ``file://`` 페이지에서 거부될 수 있다 — **폴백 필수**:
# 실패 시 같은 내용을 담은 textarea 를 펼쳐 전체선택 상태로 보여주고 안내한다.
# **조용히 실패하는 버튼 금지.** 변경이 0건이면 "수정된 항목이 없습니다" 고지
# (빈 복사 금지).
#
# 아래 스크립트는 모두 인라인이다(외부 src 참조 0건).
# ---------------------------------------------------------------------------
_PREVIEW_EDIT_SCRIPT = r"""<script>
(function(){
  "use strict";
  function normalizeText(s){return String(s==null?"":s).replace(/\s+/g," ").trim();}
  // contenteditable 의 현재 텍스트를 안전하게 읽는다(textContent — HTML 아님).
  function fieldText(el){
    // placeholder <em> 은 빈 문자열로 취급.
    var em=el.querySelector("em");
    if(em && !el.getAttribute("data-original")){return "";}
    return normalizeText(el.textContent);
  }
  // 바뀐 필드만 수집: data-original(이스케이프 해제 전 원본)과 현재 텍스트 비교.
  function collectChanges(){
    var changed=[];
    var nodes=document.querySelectorAll(".edit-field[data-field]");
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      var field=el.getAttribute("data-field")||"";
      var original=el.getAttribute("data-original")||"";
      original=normalizeText(original);
      var current=fieldText(el);
      if(current!==original){
        changed.push({field:field,before:original,after:current});
      }
    }
    return changed;
  }
  // 변경 표시 갱신.
  function refreshMarks(){
    var nodes=document.querySelectorAll(".edit-field[data-field]");
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      var original=normalizeText(el.getAttribute("data-original")||"");
      var current=fieldText(el);
      if(current!==original){el.classList.add("edit-changed");}
      else{el.classList.remove("edit-changed");}
    }
  }
  // 빈 placeholder 복원: 편집 시작 시 placeholder <em> 지우기.
  function clearPlaceholder(el){
    var em=el.querySelector("em");
    if(em){el.textContent="";}
  }
  // 클립보드에 넣을 사람/모델 양쪽이 읽을 수 있는 텍스트 조립.
  function buildChangesText(changes,productKey){
    var lines=[];
    lines.push("[클로시파이 수정사항]");
    lines.push("product_key: "+productKey);
    for(var i=0;i<changes.length;i++){
      var c=changes[i];
      lines.push(c.field+": \""+c.before+"\" → \""+c.after+"\"");
    }
    lines.push("이 내용으로 반영해 주세요.");
    return lines.join("\n");
  }
  function showStatus(msg){
    var st=document.getElementById("edit-status");
    if(st){st.textContent=msg;}
  }
  function showFallback(text){
    var fb=document.getElementById("edit-fallback");
    var ta=document.getElementById("edit-fallback-textarea");
    if(fb&&ta){
      ta.value=text;
      fb.style.display="block";
      // 전체선택.
      ta.focus();ta.select();
    }
  }
  function onCopy(){
    var changes=collectChanges();
    refreshMarks();
    var pkeyEl=document.getElementById("edit-product-key");
    var productKey=pkeyEl?pkeyEl.getAttribute("data-value")||"":"";
    if(changes.length===0){
      showStatus("수정된 항목이 없습니다. 변경된 필드가 있을 때만 복사됩니다.");
      return;
    }
    var text=buildChangesText(changes,productKey);
    var done=function(){showStatus(changes.length+"개 항목의 수정사항을 복사했습니다. 채팅에 붙여넣으세요.");};
    var fail=function(){showFallback(text);showStatus("클립보드 복사에 실패했습니다. 아래 상자를 Ctrl+C 로 복사해 채팅에 붙여넣으세요.");};
    // 클립보드 API 가 있으면 시도한다 — file:// 에서 거부될 수 있다.
    if(navigator.clipboard&&typeof navigator.clipboard.writeText==="function"){
      navigator.clipboard.writeText(text).then(done,fail);
    }else{fail();}
  }
  // 초기화: 각 편집 필드에 이벤트 연결.
  function init(){
    var nodes=document.querySelectorAll(".edit-field[data-field]");
    for(var i=0;i<nodes.length;i++){
      (function(el){
        el.addEventListener("focus",function(){clearPlaceholder(el);});
        el.addEventListener("input",refreshMarks);
        el.addEventListener("blur",refreshMarks);
      })(nodes[i]);
    }
    var btn=document.getElementById("edit-copy-btn");
    if(btn){btn.addEventListener("click",onCopy);}
  }
  if(document.readyState==="loading"){
    document.addEventListener("DOMContentLoaded",init);
  }else{init();}
})();
</script>"""


# ---------------------------------------------------------------------------
# 로컬 승인 다리 스크립트.
#
# [승인] / [수정 후 승인] 버튼이 ``http://127.0.0.1:<port>/`` 로 POST 요청을
# 보낸다. 토큰은 ``X-Approval-Token`` 헤더로 전달 — URL 에 넣으면 브라우저
# 기록·서버 로그에 남을 수 있다. 헤더가 더 안전하다.
#
# [수정 후 승인] 은 _PREVIEW_EDIT_SCRIPT 의 collectChanges() 와 같은 로직으로
# 바뀐 필드만 수집해 body.edits 로 보낸다. 명시값 우선 원칙은 서버 쪽에서
# 처리한다 — 이 스크립트는 "바뀐 필드"만 보낼 뿐 덮어쓰기 판단은 하지 않는다.
#
# POST 이기 때문에 CORS preflight 가 발생할 수 있으나, 서버가 CORS 헤더를
# 내보내지 않으므로 브라우저가 응답을 읽을 수 없다(nopaque response). 이것은
# 의도된 동작이다 — 서버는 요청을 *처리* 하되 응답을 *숨긴다*. 성공/실패는
# 응답 본문이 아니라 네트워크 오류 여부(reject)로만 대략 알 수 있다.
# 토큰 검증 실패 등은 서버가 4xx 를 주지만 CORS 차단으로 페이지가 본문을 읽지
# 못한다 — 이것도 의도된 것이다. 사용자는 "승인 요청을 보냈습니다" 라는
# 안내만 보고, 실제 등록 결과는 채팅에서 확인한다.
_PREVIEW_APPROVAL_SCRIPT = r"""<script>
(function(){
  "use strict";
  function normalizeText(s){return String(s==null?"":s).replace(/\s+/g," ").trim();}
  function fieldText(el){
    var em=el.querySelector("em");
    if(em && !el.getAttribute("data-original")){return "";}
    return normalizeText(el.textContent);
  }
  function collectChanges(){
    var changed={};
    var nodes=document.querySelectorAll(".edit-field[data-field]");
    for(var i=0;i<nodes.length;i++){
      var el=nodes[i];
      var field=el.getAttribute("data-field")||"";
      var original=normalizeText(el.getAttribute("data-original")||"");
      var current=fieldText(el);
      if(current!==original){changed[field]=current;}
    }
    return changed;
  }
  function showStatus(msg){
    var st=document.getElementById("approval-status");
    if(st){st.textContent=msg;}
  }
  function disableButtons(){
    var b1=document.getElementById("approval-btn");
    var b2=document.getElementById("approval-btn-edit");
    if(b1){b1.disabled=true;}
    if(b2){b2.disabled=true;}
  }
  function sendApproval(includeEdits){
    var btn=document.getElementById("approval-btn");
    if(!btn){return;}
    var token=btn.getAttribute("data-token")||"";
    var port=parseInt(btn.getAttribute("data-port")||"0",10);
    var productKey=btn.getAttribute("data-product-key")||"";
    if(!token||!port){
      showStatus("승인 정보가 없습니다. 미리보기를 다시 여세요.");
      return;
    }
    var body={"token":token,"product_key":productKey};
    if(includeEdits){
      var edits=collectChanges();
      body["edits"]=edits;
    }
    showStatus("승인 요청을 보내는 중...");
    disableButtons();
    // fetch 는 no-cors 모드를 쓰지 않는다 — 서버가 CORS 헤더를 주지 않으므로
    // 브라우저가 응답을 차단한다(reject). 이것은 의도된 동작이다: 서버는
    // 요청을 처리하되 응답을 숨긴다.
    fetch("http://127.0.0.1:"+port+"/",{
      method:"POST",
      headers:{"Content-Type":"application/json","X-Approval-Token":token},
      body:JSON.stringify(body)
    }).then(function(){
      showStatus("승인 요청을 보냈습니다. 등록 결과는 채팅에서 확인하세요.");
    },function(){
      // CORS 차단으로 reject 되더라도 서버는 요청을 처리했을 수 있다.
      // 에러를 "실패" 로 단정하지 않는다 — 사용자에게 "요청을 보냄" 으로
      // 안내하고 결과는 채팅에서 확인하라고 한다.
      showStatus("승인 요청을 보냈습니다. 등록 결과는 채팅에서 확인하세요.");
    });
  }
  function init(){
    var b1=document.getElementById("approval-btn");
    var b2=document.getElementById("approval-btn-edit");
    if(b1){b1.addEventListener("click",function(){sendApproval(false);});}
    if(b2){b2.addEventListener("click",function(){sendApproval(true);});}
  }
  if(document.readyState==="loading"){
    document.addEventListener("DOMContentLoaded",init);
  }else{init();}
})();
</script>"""


def render_preview_html(
    payload: dict[str, Any],
    *,
    api_payload: dict[str, Any] | None = None,
    product_key: str | None = None,
    approval_token: str | None = None,
    approval_port: int | None = None,
) -> str:
    """prepared payload 로부터 미리보기 HTML 문자열을 만든다.

        본 함수는 외부 CSS/JS/폰트를 참조하지 않는 단일 HTML 문자열을 반환한다.
        상품 이미지는 네이버 CDN 의 ``<img src>`` 로 렌더된다 — 사진이 실제
        상품과 일치하는지 사람이 확인하는 것이 이 화면의 핵심 목적이다.
        상세 HTML(``payload.detail_html``)은 ``<iframe srcdoc="...">`` 으로
        끼워넣는다 — iframe 의 srcdoc 은 인라인 문서이므로 네트워크 요청을
    일으키지 않는다.

        상품명·판매가·고시 값·태그는 페이지에서 **직접 편집**할 수 있다. 편집한
        내용은 [수정사항 복사] 버튼으로 클립보드에 담아 채팅에 붙여넣어야 반영된다.
        ``product_key`` 가 주어지면 클립보드 페이로드에 포함되어 모델이 어느
        상품인지 정확히 식별할 수 있다.

        ``approval_token`` 과 ``approval_port`` 가 모두 주어지면(설정
        ``enable_local_approval`` 켜짐 + 포트 확정 후), [승인] / [수정 후 승인]
        버튼이 페이지에 포함된다. 이 버튼은 ``http://127.0.0.1:<port>/`` 로
        POST 요청을 보내 승인을 전달한다. 둘 중 하나라도 없으면 승인 바는
        렌더되지 않는다(기본 OFF).

        Args:
            payload: prepared payload dict.
            api_payload: 등록 단계가 만들 페이로드(``naver_client.build_payload``
                결과). 고시 타입·출처 표시를 위해 쓴다. 없으면 payload 에서
                최소 정보만 읽는다.
            product_key: prepared payload 의 product_key. 클립보드 수정사항 페이
                로드에 포함된다(모델이 어느 상품인지 확실히 알도록).
            approval_token: 로컬 승인 다리의 일회용 토큰. ``approval_port`` 와
                함께 주어져야 승인 바가 렌더된다.
            approval_port: 로컬 승인 서버의 포트. ``approval_token`` 과 함께
                주어져야 승인 바가 렌더된다.

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

    # 태그(편집 가능). payload.product.tags 가 스마트스토어 검색태그 목록이다.
    tags_list = product.get("tags") if isinstance(product.get("tags"), list) else []
    tags_value = ", ".join(str(t) for t in tags_list if t)

    # 가격 표시/편집용 원본 문자열(쉼표 없는 숫자). 편집 필드는 원본 숫자를
    # data-original 로 갖고, 표시는 쉼표 포함 문자열로 보여준다.
    if sale_price is not None:
        try:
            price_display = f"{int(sale_price):,}원"
            price_original = str(int(sale_price))
        except (TypeError, ValueError):
            price_display = str(sale_price)
            price_original = str(sale_price)
    else:
        price_display = "(가격 미지정)"
        price_original = ""

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

    # 헤더: 상품명(편집 가능)·판매상태·판매가(편집 가능).
    parts.append('<div class="preview-header">')
    # 상품명: contenteditable. data-original 은 quote 이스케이프.
    name_display = name or "(상품명 없음)"
    parts.append(
        '<h1 class="preview-title">'
        f'<span class="edit-field" contenteditable="true" '
        f'data-field="상품명" data-original="{html.escape(name, quote=True)}">'
        f"{html.escape(name_display)}</span></h1>"
    )
    parts.append('<div class="preview-meta">')
    parts.append(f"<strong>카테고리 ID:</strong> {html.escape(category_id or '(미지정)')} ")
    parts.append(_status_badge(status))
    parts.append("<br />")
    # 판매가: contenteditable. 쉼표 없는 원본 숫자를 data-original 로.
    parts.append(
        '<span class="preview-price">'
        f'<span class="edit-field" contenteditable="true" '
        f'data-field="판매가" data-original="{html.escape(price_original, quote=True)}">'
        f"{html.escape(price_display)}</span></span>"
    )
    parts.append("</div>")
    parts.append("</div>")  # preview-header

    # 이미지 섹션 (읽기 전용 — 사진은 URL 로만 확인).
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

    # 고시 정보 섹션 (값 칸 편집 가능).
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

    # 태그 섹션 (편집 가능). 검색태그(sellerTags) — 쉼표 구분.
    parts.append('<div class="preview-section">')
    parts.append("<h2>검색태그</h2>")
    parts.append('<p class="preview-meta">쉼표로 구분. (예: 겨울, 후드티, 기모)</p>')
    parts.append(
        '<p class="preview-meta"><span class="edit-field" contenteditable="true" '
        f'data-field="태그" data-original="{html.escape(tags_value, quote=True)}">'
        f"{html.escape(tags_value) or '<em style=\"color:#999\">(태그 없음 — 클릭해 입력)</em>'}"
        "</span></p>"
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

    # 직접 편집 바: [수정사항 복사] 버튼 + 클립보드 폴백 textarea.
    # product_key 를 페이지에 숨겨둔다(클립보드 페이로드에 포함용).
    safe_pkey = html.escape(str(product_key or ""), quote=True)
    parts.append(
        '<div class="edit-bar">'
        '<span id="edit-product-key" class="edit-product-key" '
        f'data-value="{safe_pkey}"></span>'
        '<button type="button" id="edit-copy-btn" class="edit-copy-btn">'
        "수정사항 복사</button>"
        '<span class="edit-bar-note">'
        "<strong>이 화면에서의 수정은 복사해 채팅에 붙여넣어야 반영됩니다.</strong> "
        "여기서 고치고 닫으면 저장되지 않습니다."
        "</span>"
        '<span id="edit-status" class="edit-status"></span>'
        '<div id="edit-fallback" class="edit-fallback">'
        '<textarea id="edit-fallback-textarea" readonly></textarea>'
        '<p class="edit-fallback-note">'
        "클립보드 복사가 막혔습니다(file:// 페이지). 위 상자를 Ctrl+C 로 복사해 "
        "채팅에 붙여넣으세요."
        "</p>"
        "</div>"
        "</div>"
    )

    # 로컬 승인 바: approval_token 과 approval_port 가 모두 있을 때만 렌더.
    # 기본 OFF — 설정이 꺼져 있으면 token/port 가 None 이므로 이 바는 나오지
    # 않는다. 기존 클립보드 경로(수정사항 복사)는 그대로 동작한다.
    if approval_token and approval_port:
        safe_token = html.escape(str(approval_token), quote=True)
        # data-* 속성으로 token/port/product_key 를 페이지에 심는다.
        # 스크립트는 이 값들을 읽어 POST / 로 승인을 보낸다.
        parts.append(
            '<div class="approval-bar">'
            '<span class="approval-bar-note">'
            "<strong>[승인] 버튼을 누르면 이 상품이 등록됩니다.</strong><br />"
            "[수정 후 승인] 은 페이지에서 바꾼 값을 함께 보냅니다. "
            "10분 안에 누르지 않으면 자동 만료됩니다."
            "</span>"
            '<button type="button" id="approval-btn" '
            f'class="approval-btn approval-btn-approve" '
            f'data-token="{safe_token}" data-port="{int(approval_port)}" '
            f'data-product-key="{safe_pkey}">'
            "승인</button>"
            '<button type="button" id="approval-btn-edit" '
            f'class="approval-btn approval-btn-approve-edit" '
            f'data-token="{safe_token}" data-port="{int(approval_port)}" '
            f'data-product-key="{safe_pkey}">'
            "수정 후 승인</button>"
            '<span id="approval-status" class="approval-status"></span>'
            "</div>"
        )

    parts.append("</div>")  # preview-wrap
    parts.append(_PREVIEW_EDIT_SCRIPT)
    # 승인 바가 있을 때만 승인 스크립트를 포함한다(불필요한 코드 노출 금지).
    if approval_token and approval_port:
        parts.append(_PREVIEW_APPROVAL_SCRIPT)
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def write_preview_html(
    product_key: str,
    payload: dict[str, Any],
    *,
    api_payload: dict[str, Any] | None = None,
    approval_token: str | None = None,
    approval_port: int | None = None,
) -> Path:
    """미리보기 HTML 을 디스크에 쓰고 경로를 반환한다.

    prepared payload 디렉터리 규약 하위의 ``preview.html`` 로 쓴다.
    ``product_key`` 는 클립보드 수정사항 페이로드에 포함되도록 페이지에
    싣는다(모델이 어느 상품인지 정확히 식별).

    ``approval_token`` 과 ``approval_port`` 가 모두 주어지면 승인 바가
    페이지에 포함된다. 등록 도구가 포트를 확정한 뒤 이 함수로 미리보기를
    갱신할 때 쓴다(포트를 모르는 상태에서는 토큰만 발급하고 서버는 띄우지
    않는다 — 불필요한 포트 개방 금지).

    Args:
        product_key: prepared payload 의 product_key.
        payload: prepared payload dict.
        api_payload: 등록 단계 페이로드(선택). 고시 출처 표시에 쓴다.
        approval_token: 로컬 승인 다리 토큰(선택).
        approval_port: 로컬 승인 서버 포트(선택).

    Returns:
        쓴 파일의 경로.
    """
    path = _preview_path(product_key)
    html_doc = render_preview_html(
        payload,
        api_payload=api_payload,
        product_key=product_key,
        approval_token=approval_token,
        approval_port=approval_port,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")
    return path


__all__ = [
    "render_preview_html",
    "write_preview_html",
]
