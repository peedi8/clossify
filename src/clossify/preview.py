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
from typing import Any, Literal

from . import naver_client, qa_agents

# ---------------------------------------------------------------------------
# 미리보기 모드.
#
# **왜 두 모드인가**: 미리보기 HTML 은 두 곳에서 소비된다 — MCP 우측 패널(정적
# 미리보기)과 브라우저 창(승인/편집 조작). 패널은 JS 를 실행하지 않고 폼도
# 제출할 수 없다. 그런데 이전에는 한 가지 HTML 을 양쪽에 썼다 — 패널에
# ``contenteditable``·``<button>``·``<input>``·``<script>`` 가 "누를 수 있게
# 생겼는데 아무 반응이 없는" 죽은 UI 가 됐다(최근 승인 버튼의 거짓 성공과 같은
# 계열 — "불가능을 가능처럼 표시").
#
# 안전한 쪽이 기본이다: 모드 인자 없이 부르면 **보기 전용**이 나간다. 모르고
# 패널에 뿌려도 죽은 UI 가 나가지 않는다. 조작 모드는 브라우저로 가는 경로가
# **명시적으로** 전환해야만 나온다.
#
# 의미:
#   - ``"view_only"`` — 패널용. ``<script>``·``contenteditable``·``<button>``·
#     ``<input>``·``onclick``·``addEventListener`` 가 **0개**. 상품 정보(상품명·
#     가격·이미지·상세 본문·고시)는 조작 모드와 동일하게 보인다. "보기 전용"
#     표기와 "브라우저 창에서 조작" 안내가 들어간다.
#   - ``"interactive"`` — 브라우저용. 현재 거동 유지(승인 폼 POST · hidden 토큰 ·
#     클립보드 편집 바). 회귀 금지.
# ---------------------------------------------------------------------------
PreviewMode = Literal["view_only", "interactive"]

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
      - ``"사용자 입력 (설정에도 있음)"`` — 고시 본문에 사용자가 넣은 값과
        config 유래 값이 같은 필드에 겹칠 때, 중첩 고시값과 설정 유래를
        구분해 표시한다 (감리 ⑧ — 같은 출처 표기로 그려지는 것을 고친다).
      - ``"미제공"`` — 값이 비어 있거나 사용자·config 어디에도 없는 필드.

    **설정 유래 규제 필드의 top-level 명시값을 먼저 본다** (회귀 수정).
    ``origin_content``·``importer``·``manufacturer``·``delivery_fee`` 는
    ``naver_client._notice_defaults`` 가 top-level 상품 입력에서 읽는다
    (``p.made_in``/``p.origin_content``, ``p.importer``, ``p.manufacturer``
    및 판매자 별칭, ``p.delivery_fee``). 과거 이 함수는 고시 본문 노드
    (``user_body``)만 읽어서, top-level 에 명시한 값을 **빈 값 + 미제공**
    으로 그렸다 — 실제 전송값과 다른 거짓 미리보기. 이제 해석기가 읽는
    같은 후보를 같은 순서로 먼저 본다.
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
    # 설정 유래 규제 보고 필드(origin_content·importer·manufacturer·delivery_fee)는
    # 사용자 고시 본문에 보통 없으므로 후보에 명시적으로 포함시킨다.
    # 이 필드들이 config 유래일 때 미리보기에 각각 한 줄씩 등장해야 한다
    # (감리 지적: "보고 필드가 안 보인다" — 기능 목적 자체가 무력화).
    for n7_field in ("origin_content", "importer", "manufacturer", "delivery_fee"):
        if n7_field not in candidate_fields:
            candidate_fields.append(n7_field)
    for key in user_body:
        if key not in candidate_fields:
            candidate_fields.append(str(key))

    # config 키 별칭 맵: 보고명 → config 에서 값을 읽을 때 쓸 키 목록.
    # delivery_fee 는 config 에서 deliveryFee(camelCase) 로도 올 수 있다.
    _cfg_key_aliases: dict[str, tuple[str, ...]] = {
        "delivery_fee": ("delivery_fee", "deliveryFee"),
    }

    # 설정 유래 규제 필드의 top-level 명시값 후보 맵.
    # naver_client._notice_defaults 해석기가 실제로 읽는 top-level 키와 동일한
    # 후보를 쓴다 — 해석기와 미리보기가 다른 값을 그리는 불일치를 막는다.
    # manufacturer 의 판매자 별칭 후보는 naver_client._seller_manufacturer_default
    # 의 후보와 같다.
    _manufacturer_top_keys = (
        "manufacturer",
        "seller_name_ko",
        "sellerNameKo",
        "seller_name",
        "sellerName",
        "shop_name_ko",
        "shopNameKo",
        "shop_name",
        "shopName",
        "nick",
        "nickName",
    )
    _n7_top_keys: dict[str, tuple[str, ...]] = {
        "origin_content": ("made_in", "origin_content"),
        "importer": ("importer",),
        "manufacturer": _manufacturer_top_keys,
        "delivery_fee": ("delivery_fee",),
    }

    for field in candidate_fields:
        # 설정 유래 규제 필드는 top-level 상품 입력에서 먼저 찾는다. 해석기가 읽는 후보와
        # 같은 키를 본다 — top-level 에 명시한 값을 "미제공" 으로 그리는
        # 회귀를 고친다.
        top_keys = _n7_top_keys.get(field)
        if top_keys and isinstance(product, dict):
            top_value: Any = None
            for tk in top_keys:
                cv = product.get(tk)
                # **감리 (6라운드)**: 자리표시자 판정은 해석기의 단일 진실 공급원
                # (``naver_client._has_text``) 을 그대로 호출한다 — 새 판정 함수를
                # 만들면 같은 결함이 다섯 번째로 재발한다.
                # 참고: ``_has_text(int 0)`` 은 ``False`` 를 반환하는 기존 한계가
                # 있으므로, 숫자(int/float, bool 제외) 는 유효한 명시값으로 본다
                # (배송비 0 = 무료배송). 숫자는 자리표시자 토큰이 될 수 없다.
                if isinstance(cv, bool):
                    # bool 은 int 서브클래스지만 배송비·규제값으로 불리언은 입력
                    # 오류다 — 유효하지 않은 것으로 본다.
                    continue
                if isinstance(cv, int | float):
                    top_value = cv
                    break
                if naver_client._has_text(cv):
                    top_value = cv
                    break
            if top_value is not None:
                _add(field, top_value, "사용자 입력")
                continue
        if field in user_body:
            # **감리 (6라운드)**: ``user_body`` 의 값이 자리표시자(TBD/TODO/
            # REPLACE_WITH_...) 이면 "사용자 입력" 으로 받아들이지 않는다 —
            # 해석기(``_first_value``/``_has_text``)가 같은 자리표시자를
            # 건너뛰고 config 폴백으로 가기 때문에, 미리보기도 같은 판정으로
            # 건너뛴다. ``naver_client._has_text`` 를 호출한다 (판정 두 벌 금지).
            uv = user_body[field]
            if naver_client._has_text(uv):
                # 감리 ⑧ (4라운드): 고시 본문에 사용자가 넣은 값과 config 유래 값이
                # 같은 필드에 겹칠 때 출처를 갈라 표시한다. 과거에는 무조건
                # "사용자 입력" 으로 그려서, config 도 같은 필드에 값을 가지고 있다는
                # 사실이 묻혔다 — 중첩 고시값과 설정 유래를 구분 안 하는 결함.
                _src = "사용자 입력 (설정에도 있음)" if field in cfg_filled else "사용자 입력"
                _add(field, uv, _src)
                continue
            # 자리표시자면 폴백으로 — 아래 cfg_filled 분기로 넘어간다.
        if field in cfg_filled:
            # config 에서 값을 읽는다. 별칭이 있으면 별칭 키도 확인.
            cfg_keys = _cfg_key_aliases.get(field, (field,))
            cfg_value = None
            for ck in cfg_keys:
                cv = cfg_notice.get(ck)
                # **감리 (6라운드)**: 자리표시자 판정을 해석기의 단일 진실 공급원
                # (``naver_client._has_text``) 에 위임한다. 과거에는 ``REPLACE_WITH_``
                # 접두사만 하드코딩으로 걸렀으나, TBD/TODO/해당없음 등
                # ``qa_agents._is_placeholder_value`` 토큰은 잡지 못해 판정이
                # 어긋났다. 판정을 새로 만들지 않고 해석기가 쓰는 것을 호출한다.
                # 숫자(int/float, bool 제외) 는 자리표시자가 될 수 없으므로 유효.
                if isinstance(cv, bool):
                    continue
                if isinstance(cv, int | float):
                    cfg_value = cv
                    break
                if naver_client._has_text(cv):
                    cfg_value = cv
                    break
            if cfg_value is not None:
                _add(field, cfg_value, "설정 기본값")
            else:
                _add(field, "", "미제공")
        else:
            _add(field, "", "미제공")

    return rows


# ---------------------------------------------------------------------------
# 상품속성(productAttributes) 행 빌딩.
#
# 등록 단계가 만들 페이로드(``naver_client.build_payload``)와 *같은 값을*
# 그린다 — ``build_payload`` 가 ``_validate_product_attributes(p.get("attributes"))``
# 로 검증한 뒤 ``detailAttribute.productAttributes`` 에 싣는다. 미리보기도
# 같은 ``product.attributes`` 를 읽어 같은 형태로 행을 만든다. 화면과 payload
# 가 어긋나면(오늘 네 번 어긋났던 자리) 안 된다.
#
# 출처 표기는 기존 어휘를 쓴다 — **"사용자 입력"** (속성은 config 에 두지
# 않으므로 "설정 기본값" 은 없다). 속성이 없으면 행을 억지로 만들지 않는다
# (미제공 한 줄).
# ---------------------------------------------------------------------------


def _collect_attribute_rows(product: dict[str, Any]) -> list[dict[str, str]]:
    """상품속성을 (필드, 값, 출처) 행 리스트로 모은다.

    ``product.attributes`` (명시적 ID 리스트) 를 읽어 행으로 만든다.
    각 속성은 ``attributeSeq`` · ``attributeValueSeq`` (필수) 와
    ``attributeRealValue`` · ``attributeRealValueUnitCode`` (범위형 선택) 를
    가진다. 화면에 그리는 형태는 페이로드에 실리는 형태와 같아야 한다 —
    ``naver_client._validate_product_attributes`` 가 허용하는 키 4종만 쓴다.

    출처는 항상 "사용자 입력" 이다 — 속성은 config 에 두지 않는다(설정 유래 없음).

    속성이 없으면 빈 리스트를 반환한다 — 행을 억지로 만들지 마라.
    """
    if not isinstance(product, dict):
        return []
    raw_attrs = product.get("attributes")
    if not isinstance(raw_attrs, list) or not raw_attrs:
        return []
    rows: list[dict[str, str]] = []
    for item in raw_attrs:
        if not isinstance(item, dict):
            continue
        seq = item.get("attributeSeq")
        vseq = item.get("attributeValueSeq")
        # 필수 키가 없거나 정수가 아니면 행을 만들지 않는다 — payload 에도
        # 실리지 않을 값이므로 화면에도 그리지 않는다 (불일치 금지).
        if not isinstance(seq, int) or isinstance(seq, bool):
            continue
        if not isinstance(vseq, int) or isinstance(vseq, bool):
            continue
        # 값 조합: "seq / vseq" 가 기본. 범위형이면 실값+단위를 덧붙인다.
        value_parts = [f"attributeSeq={seq}", f"attributeValueSeq={vseq}"]
        real_val = item.get("attributeRealValue")
        if isinstance(real_val, str) and real_val.strip():
            unit = item.get("attributeRealValueUnitCode")
            unit_text = f" {unit}" if isinstance(unit, str) and unit.strip() else ""
            value_parts.append(f"attributeRealValue={real_val}{unit_text}")
        rows.append(
            {
                "field": f"속성 #{len(rows) + 1}",
                "value": ", ".join(value_parts),
                "source": "사용자 입력",
            }
        )
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
.source-user-config-overlap{color:#7a5900;font-size:11px;font-weight:600;
  background:#fff8e1;padding:1px 4px;border-radius:3px}
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

# 조작 모드 전용 CSS — 보기 전용 모드에서는 이 블록 전체를 뺀다(죽은 CSS 방지).
# 클래스 정의가 없으면 보기 전용 HTML 을 grep 해도 ".edit-field" 등의 단어가
# 아예 등장하지 않는다(티켓 계약의 "누를 수 있게 생긴 것 0개" 를 글자 그대로
# 만족).
_PREVIEW_CSS_INTERACTIVE = """
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

# 보기 전용 배너 CSS — 보기 전용(패널) 모드에서만 나타난다. 인터랙티브 요소가
# 이 화면에 없다는 사실을 분명히 한다(죽은 UI 방지).
_PREVIEW_CSS_VIEW_ONLY = """
.view-only-banner{background:#fff8e1;border:1px solid #ffe082;color:#7a5900;
  padding:12px 16px;border-radius:8px;margin-bottom:20px;font-size:13px;
  line-height:1.6}
.view-only-banner strong{color:#5d4400}
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
        elif source == "사용자 입력 (설정에도 있음)":
            parts.append(
                '<td><span class="source-user-config-overlap">'
                "사용자 입력 (설정에도 있음)</span></td>"
            )
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

    본 함수는 **조작 모드**용이다. ``onerror`` 인라인 핸들러가 있어 CDN 이미지
    로드 실패 시 박스를 숨긴다. 보기 전용 모드는 ``_render_images_readonly``
    를 쓴다 — 이벤트 핸들러 속성(``onerror``)도 빠진다(티켓 계약: ``onclick``
    /``addEventListener`` 0건 — 인라인 이벤트 핸들러 속성까지 0개로 범위를
    좁힌다, 더 안전한 쪽).
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


def _render_images_readonly(listing_urls: list[str]) -> str:
    """보기 전용 모드의 이미지 갤러리 HTML.

    조작 모드의 ``_render_images`` 와 **동일한 정보**(같은 URL·같은 대표 배지·
    같은 순서)를 보여주되 이벤트 핸들러 속성(``onerror``)을 뺀다. 티켓 계약이
    ``onclick``/``addEventListener`` 0건이지만, 보기 전용 모드는 더 엄격하게
    **모든 인라인 이벤트 핸들러 속성**을 0개로 유지한다 — 보기 전용 HTML 이
    실행 가능한 코드를 품는 경로를 원천 차단.

    이미지 로드 실패 시 박스가 빈 칸으로 남는다(조작 모드처럼 숨겨지지 않는다).
    이것은 정보 손실이 아니다 — 빈 칸도 "이미지가 있어야 할 자리" 라는 정보를
    주며, 판매자가 브라우저로 직접 확인할 수 있다.
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
            f'<img src="{safe_url}" alt="상품 이미지 {idx + 1}" loading="lazy" />'
            f"</div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


def _render_notice_table_readonly(rows: list[dict[str, str]]) -> str:
    """보기 전용 모드의 고시 정보 표 HTML.

    조작 모드의 ``_render_notice_table`` 과 **동일한 값·동일한 출처 표시**를
    보여주되 값 칸을 ``contenteditable`` 이 아니라 순수 텍스트 ``<td>`` 로
    렌더한다. ``data-field``/``data-original`` 속성도 뺀다 — 보기 전용 HTML 은
    편집 단서 자체를 품지 않는다(죽은 UI 가 아니라 처음부터 UI 가 아님).

    빈 값 표시는 "(비어 있음)" 텍스트로 통일한다 — 조작 모드의 "(클릭해 입력)"
    안내는 행동을 유도하므로 보기 전용에서는 빼고, 비어 있다는 사실만 알린다.
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
        parts.append(f"<th>{html.escape(field)}</th>")
        if value:
            parts.append(f"<td>{html.escape(value)}</td>")
        else:
            parts.append('<td><em style="color:#999">(비어 있음)</em></td>')
        if source == "사용자 입력":
            parts.append('<td><span class="source-user">사용자 입력</span></td>')
        elif source == "사용자 입력 (설정에도 있음)":
            parts.append(
                '<td><span class="source-user-config-overlap">'
                "사용자 입력 (설정에도 있음)</span></td>"
            )
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


def _render_attribute_section(rows: list[dict[str, str]]) -> str:
    """상품속성 표 HTML — 보기 전용·조작 모드 공통 (편집 불가).

    속성은 ID 참조(attributeSeq·attributeValueSeq) 이므로 페이지에서 직접
    편집하는 것이 의미 없다 — 고시 값처럼 contenteditable 으로 열지 않는다.
    대신 전송될 값을 행으로 보여준다. 출처는 항상 "사용자 입력" 이고,
    "설정 기본값" 은 없다(속성은 config 에 두지 않는다).

    속성이 없으면 한 줄("상품속성 미제공") 만 반환한다 — 행을 억지로 만들지 마라.
    """
    if not rows:
        return '<p class="preview-meta">(상품속성 미제공)</p>'
    parts = [
        '<table class="notice-table">',
        "<thead><tr><th>속성</th><th>값</th><th>출처</th></tr></thead>",
        "<tbody>",
    ]
    for row in rows:
        field = row["field"]
        value = row["value"]
        source = row["source"]
        parts.append('<tr class="notice-row">')
        parts.append(f"<th>{html.escape(field)}</th>")
        parts.append(f"<td>{html.escape(value)}</td>")
        if source == "사용자 입력":
            parts.append('<td><span class="source-user">사용자 입력</span></td>')
        else:
            parts.append('<td><span class="source-missing">미제공</span></td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _view_only_banner_html() -> str:
    """보기 전용 모드임을 알리고 조작이 가능한 자리를 안내하는 배너.

    **과장 금지**: 이 배너는 "보기 전용" 임을 사실대로 알린다. 브라우저 창에서
    조작(편집·승인)이 가능하다는 안내만 넣고, 없는 기능을 있는 것처럼 말하지
    않는다. 인라인 이벤트 핸들러(``onclick`` 등)를 쓰지 않는다 — 배너 자체가
    보기 전용 HTML 의 계약을 깨면 안 된다.

    템플릿 저장 안내: 보기 전용 화면에서도 "이 구성을 템플릿으로 저장할
    수 있다"는 사실을 알린다. 단, 이 패널에서는 버튼·입력란·스크립트가
    0개여야 하므로 **방법만 안내**한다 — 브라우저 창을 열거나, ``submit_reviews``
    의 ``save_prepared_as_template`` 인자로 이름을 주면 저장된다. 없는 기능을
    말하지 않는다: 저장은 실제로 일어나는 일이며, 이 패널에서 "누를 버튼"만
    없을 뿐이다.
    """
    return (
        '<div class="view-only-banner">'
        "<strong>보기 전용 화면</strong> — 이 화면에서는 값을 고치거나 승인할 수 없습니다. "
        "편집·승인은 이 미리보기를 브라우저 창에서 열 때 가능합니다."
        "<br />"
        "<strong>이 구성을 템플릿으로 저장할 수 있습니다.</strong> "
        "고시 타입·AS 정보·원산지·배송 정보 등 규제값만 빠져 저장되며, "
        "상품명·가격·이미지·재고는 담기지 않습니다. "
        "저장하려면 (1) 이 미리보기를 브라우저 창에서 열어 [수정사항 복사] 처럼 "
        "채팅에 저장 요청을 붙여넣거나, (2) ``submit_reviews`` 도구의 "
        "``save_prepared_as_template`` 인자에 템플릿 이름을 주세요. "
        "네트워크 호출 없이 로컬에서 처리됩니다."
        "</div>"
    )


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
# 로컬 승인 다리: 순수 HTML 폼 POST.
#
# **이전 설계의 결함**: ``fetch`` 로 ``Content-Type: application/json`` +
# 커스텀 헤더 ``X-Approval-Token`` 을 보냈다. 브라우저가 **CORS 프리플라이트
# (OPTIONS)** 를 먼저 보내고, 서버가 OPTIONS 를 거부하므로 본 요청이 발송되지
# 않았다. 더 나쁜 것은 fetch 의 성공 콜백과 실패 콜백이 **모두** "승인 요청을
# 보냈습니다" 를 표시했다 — 아무것도 안 갔는데 갔다고 말하는 조용한 거짓 성공.
#
# **현재 설계**: ``<form method="POST" action="http://127.0.0.1:<port>/">``
# + ``<button type="submit">``. ``enctype`` 은 기본값
# (``application/x-www-form-urlencoded``) 을 그대로 써서 **CORS 프리플라이트를
# 유발하지 않는다**. 커스텀 헤더는 일절 쓰지 않는다(프리플라이트를 부르는
# 조건). 토큰은 헤더가 아니라 **hidden 필드** ``<input name="token">`` 로 보낸다
# (서버의 ``_extract_token`` 본문 폴백 규약에 맞춤). ``product_key`` 도 hidden.
#
# **전송 자체는 JS 없이 성립한다.** [승인] 버튼은 순수 ``<button type="submit">``
# 이고 JS 가 없어도 폼이 전송된다. JS 는 [수정 후 승인] 의 편집값을 hidden
# ``edits[<field>]`` 입력으로 채우는 용도로만 쓴다 — 그것도 실패하면 폼 전송을
# 막지 않고 그냥 빈 상태로 전송된다(사용자는 결과 페이지에서 확인).
#
# 결과 페이지는 서버가 직접 렌더한다(승인 접수/거부/만료/사유). 브라우저가
# 그 페이지로 이동하므로 미리보기 쪽 상태 문구는 불필요하다 — "전송 중" 수준
# 의 최소 안내만 남긴다. **결과를 모르면서 "보냈다"고 단정하는 문구는 없다.**
# ---------------------------------------------------------------------------
_PREVIEW_APPROVAL_SCRIPT = r"""<script>
(function(){
  "use strict";
  // [수정 후 승인] 버튼: contenteditable 의 바뀐 값만 hidden 입력으로 만들어
  // 폼에 끼워넣은 뒤 폼을 제출한다. 전송 자체는 순수 폼 POST (이 스크립트가
  // 실패해도 폼 제출 자체는 일어난다 — form.submit() 이 아닌 submit 버튼 클릭).
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
  function fillEditsIntoForm(form){
    // 이전에 채워둔 edits 입력이 있으면 모두 지운다(재사용 대비).
    var old=form.querySelectorAll('input[name^="edits["]');
    for(var i=0;i<old.length;i++){old[i].parentNode.removeChild(old[i]);}
    var changes=collectChanges();
    for(var field in changes){
      if(!Object.prototype.hasOwnProperty.call(changes,field)){continue;}
      var inp=document.createElement("input");
      inp.type="hidden";
      inp.name="edits["+field+"]";
      inp.value=changes[field];
      form.appendChild(inp);
    }
  }
  function onApproveEdit(ev){
    var form=document.getElementById("approval-form");
    if(!form){return;}
    fillEditsIntoForm(form);
    // 폼의 기본 제출을 막지 않는다 — hidden 입력이 채워진 상태로 폼이 전송된다.
    // (여기서 form.submit() 을 부르면 폼의 submit 이벤트가 아니라 직접 제출이라
    //  브라우저의 기본 폼 제출 UX 와 다를 수 있다. submit 버튼의 기본 동작에 맡긴다.)
  }
  function init(){
    var b2=document.getElementById("approval-btn-edit");
    if(b2){b2.addEventListener("click",onApproveEdit);}
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
    mode: PreviewMode = "view_only",
) -> str:
    """prepared payload 로부터 미리보기 HTML 문자열을 만든다.

    본 함수는 외부 CSS/JS/폰트를 참조하지 않는 단일 HTML 문자열을 반환한다.
    상품 이미지는 네이버 CDN 의 ``<img src>`` 로 렌더된다 — 사진이 실제
    상품과 일치하는지 사람이 확인하는 것이 이 화면의 핵심 목적이다.
    상세 HTML(``payload.detail_html``)은 ``<iframe srcdoc="...">`` 으로
    끼워넣는다 — iframe 의 srcdoc 은 인라인 문서이므로 네트워크 요청을
    일으키지 않는다.

    **두 모드** 로 나뉜다(티켓: 보기 전용(패널)과 조작(브라우저)을 가른다):

    - ``mode="view_only"`` (**기본값**) — MCP 우측 패널용 정적 미리보기. 이 모드는
      ``<script>``·``contenteditable``·``<button>``·``<input>``·``onclick``·
      ``addEventListener`` 가 **0개** 나온다 — 패널은 JS 를 실행하지도 폼을
      제출하지도 못하므로, "누를 수 있게 생겼는데 안 눌리는" 죽은 UI 를
      원천 차단한다. 상품 정보(상품명·가격·이미지·상세 본문·고시)는 조작 모드와
      **동일하게** 보인다(정보 손실 없음). "보기 전용" 표기와 "조작은 브라우저
      창에서" 안내가 들어간다. **안전한 쪽이 기본** — 모르고 패널에 뿌려도
      죽은 UI 가 나가지 않는다.

    - ``mode="interactive"`` — 브라우저 창용. 상품명·판매가·고시 값·태그를
      페이지에서 직접 편집할 수 있고 [수정사항 복사] 버튼으로 클립보드에
      담아 채팅에 붙여넣어야 반영된다. ``approval_token``/``approval_port`` 까지
      주어지면 [승인] / [수정 후 승인] 버튼이 추가된다(순수 HTML 폼 POST,
      hidden 토큰). 회귀 금지.

    호출부 배정(어느 경로가 어느 모드인지):
      - ``register.prepare_listing`` → 최초 prepared 생성 직후. **보기 전용**.
        이 파일이 MCP 우측 패널에 바로 표시되기 때문.
      - ``mcp_server.register_product`` (승인 대기 진입) → 로컬 승인 서버를
        띄운 직후 파일을 갱신할 때. **조작 모드**. 사용자가 브라우저에서
        [승인] 버튼을 누르는 자리다.
      - 단위 테스트(``test_preview_edit``/``test_approval_form_post``) →
        **조작 모드** 명시 호출(회귀 검증).

    Args:
        payload: prepared payload dict.
        api_payload: 등록 단계가 만들 페이로드(``naver_client.build_payload``
            결과). 고시 타입·출처 표시를 위해 쓴다. 없으면 payload 에서
            최소 정보만 읽는다.
        product_key: prepared payload 의 product_key. 조작 모드에서 클립보드
            수정사항 페이로드에 포함된다(모델이 어느 상품인지 확실히 알도록).
        approval_token: 로컬 승인 다리의 일회용 토큰. ``approval_port`` 와
            함께 주어져야 승인 바가 렌더된다(**조작 모드에서만 의미 있음** —
            보기 전용 모드에서는 무시되고 승인 바가 나오지 않는다).
        approval_port: 로컬 승인 서버의 포트. ``approval_token`` 과 함께
            주어져야 승인 바가 렌더된다(조작 모드에서만).
        mode: ``"view_only"`` (기본) 또는 ``"interactive"``. 위 참조.

    Returns:
        완전한 HTML 문서 문자열.
    """
    # mode 정규화. 알 수 없는 값은 안전한 쪽(보기 전용)으로 강등 — 로깅/경고
    # 없이 조용히 바꾼다(호출자가 잘못 줘도 죽은 UI 가 나가지 않게).
    if mode != "interactive":
        mode = "view_only"

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

    # 상품속성 행 — payload.product.attributes 에서 읽는다. 화면과 payload 가
    # 같아야 한다 (오늘 네 번 어긋났던 자리). 속성이 없으면 빈 리스트.
    attribute_rows = _collect_attribute_rows(product)

    # srcdoc 은 HTML 의 " 속성을 escape 해야 한다.
    srcdoc = html.escape(detail_html, quote=True)
    # 생성 시각(UTC).
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # 태그. payload.product.tags 가 스마트스토어 검색태그 목록이다.
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

    interactive = mode == "interactive"

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
        # 보기 전용 배너 CSS 는 보기 전용 모드에서만. 조작 모드 전용 CSS
        # (.edit-field 등) 는 조작 모드에서만 — 죽은 CSS 까지 0개로.
        _PREVIEW_CSS_VIEW_ONLY if not interactive else _PREVIEW_CSS_INTERACTIVE,
        "</style>",
        "</head>",
        "<body>",
        '<div class="preview-wrap">',
    ]

    # 보기 전용 배너 — 보기 전용 모드에서만. 패널에 표시될 때 "조작 불가" 임을
    # 분명히 하고 조작이 가능한 자리(브라우저 창)를 안내한다. 인라인 이벤트
    # 핸들러 없이 순수 텍스트 배너.
    if not interactive:
        parts.append(_view_only_banner_html())

    # 판매상태 배너 — 가장 눈에 띄는 최상단 위치. 두 모드 공통.
    parts.append(_status_banner(status))

    # 헤더: 상품명·판매상태·판매가.
    # 조작 모드: 상품명/판매가 칸이 contenteditable. 보기 전용 모드: 순수 텍스트.
    parts.append('<div class="preview-header">')
    name_display = name or "(상품명 없음)"
    if interactive:
        parts.append(
            '<h1 class="preview-title">'
            f'<span class="edit-field" contenteditable="true" '
            f'data-field="상품명" data-original="{html.escape(name, quote=True)}">'
            f"{html.escape(name_display)}</span></h1>"
        )
    else:
        parts.append(f'<h1 class="preview-title">{html.escape(name_display)}</h1>')
    parts.append('<div class="preview-meta">')
    parts.append(f"<strong>카테고리 ID:</strong> {html.escape(category_id or '(미지정)')} ")
    parts.append(_status_badge(status))
    parts.append("<br />")
    if interactive:
        parts.append(
            '<span class="preview-price">'
            f'<span class="edit-field" contenteditable="true" '
            f'data-field="판매가" data-original="{html.escape(price_original, quote=True)}">'
            f"{html.escape(price_display)}</span></span>"
        )
    else:
        parts.append(f'<span class="preview-price">{html.escape(price_display)}</span>')
    parts.append("</div>")
    parts.append("</div>")  # preview-header

    # 이미지 섹션 (읽기 전용 — 사진은 URL 로만 확인).
    parts.append('<div class="preview-section">')
    parts.append("<h2>이미지</h2>")
    parts.append(
        _render_images_readonly(listing_urls) if not interactive else _render_images(listing_urls)
    )
    parts.append("</div>")

    # 상세 페이지 섹션 (iframe srcdoc — 외부 리소스 참조 없음). 두 모드 공통.
    parts.append('<div class="preview-section">')
    parts.append("<h2>상세 페이지 (렌더 결과)</h2>")
    if detail_html:
        parts.append('<div class="detail-frame">')
        parts.append(f'<iframe srcdoc="{srcdoc}"></iframe>')
        parts.append("</div>")
    else:
        parts.append('<p class="preview-meta">(상세 HTML 없음)</p>')
    parts.append("</div>")

    # 고시 정보 섹션. 조작 모드: 값 칸 편집 가능. 보기 전용: 순수 텍스트 칸.
    parts.append('<div class="preview-section">')
    parts.append("<h2>상품정보제공고시</h2>")
    parts.append(
        '<p class="preview-meta"><strong>고시 타입:</strong> '
        f"{html.escape(notice_type_label)}"
        f" ({html.escape(notice_type or '미확정')})</p>"
    )
    parts.append(
        _render_notice_table_readonly(notice_rows)
        if not interactive
        else _render_notice_table(notice_rows)
    )
    parts.append(
        '<p class="preview-meta"><span class="source-config">설정 기본값</span>'
        " 표시가 있는 필드는 판매자가 입력하지 않았지만 config 의 "
        "smartstore_notice_defaults 에서 자동으로 채워진 값입니다. "
        "의도한 값인지 확인하세요. "
        '<span class="source-user-config-overlap">사용자 입력 (설정에도 있음)</span>'
        " 은 고시 본문 값과 설정 값이 겹치는 필드입니다.</p>"
    )
    parts.append("</div>")

    # 상품속성(productAttributes) 섹션 — 보기 전용·조작 모드 공통 (편집 불가).
    # 속성은 ID 참조이므로 contenteditable 으로 열지 않는다. 전송될 값을 행으로
    # 보여준다. 속성이 없으면 "미제공" 한 줄 — 행을 억지로 만들지 마라.
    # 화면과 payload 가 같아야 한다 (오늘 네 번 어긋났던 자리).
    parts.append('<div class="preview-section">')
    parts.append("<h2>상품속성 (productAttributes)</h2>")
    parts.append(_render_attribute_section(attribute_rows))
    parts.append("</div>")

    # 태그 섹션. 조작 모드: 편집 가능(contenteditable). 보기 전용: 텍스트.
    parts.append('<div class="preview-section">')
    parts.append("<h2>검색태그</h2>")
    if interactive:
        parts.append('<p class="preview-meta">쉼표로 구분. (예: 겨울, 후드티, 기모)</p>')
        parts.append(
            '<p class="preview-meta"><span class="edit-field" contenteditable="true" '
            f'data-field="태그" data-original="{html.escape(tags_value, quote=True)}">'
            f"{html.escape(tags_value) or '<em style=\"color:#999\">(태그 없음 — 클릭해 입력)</em>'}"
            "</span></p>"
        )
    else:
        parts.append(f'<p class="preview-meta">{html.escape(tags_value) or "(태그 없음)"}</p>')
    parts.append("</div>")

    # 안내문 (두 모드 공통 — 미리보기의 본 목적 안내).
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

    # 직접 편집 바·승인 바·스크립트 — 조작 모드에서만.
    # 보기 전용 모드는 여기서 아무것도 더 붙이지 않고 문서를 닫는다.
    if interactive:
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

        # 템플릿 저장 안내 바: 조작 모드에서만 나간다(보기 전용 패널은 위
        # 배너 텍스트로만 안내). **새 전송 경로를 만들지 않는다** — fetch 폼 POST
        # 모두 없다. 클립보드 복사 패턴을 **재사용** 한다: 아래 문구를 복사해
        # 채팅에 붙여넣으면 모델이 ``submit_reviews`` 의
        # ``save_prepared_as_template`` 인자로 저장을 호출한다. 이 바 자체는
        # 버튼·폼·스크립트 없이 순수 텍스트 안내다 — 이미 위 [수정사항 복사]
        # 버튼이 클립보드 다리 역할을 하므로, 여기서는 "무엇을 붙여넣을지"만
        # 알려준다. 상품명·가격·이미지·재고는 담기지 않는다는 점도 함께(과장 금지).
        parts.append(
            '<div class="edit-bar" style="background:#eef6ff;border-color:#1a73e8">'
            '<span class="edit-bar-note">'
            "<strong>이 구성을 템플릿으로 저장할 수 있습니다.</strong><br />"
            "규제값(고시 타입·AS·원산지·배송)만 저장되고 상품명·가격·이미지·재고는 "
            "담기지 않습니다. 저장하려면 아래 한 줄을 복사해 채팅에 붙여넣으세요."
            '<br /><em style="color:#1a4d8f">'
            "[클로시파이 템플릿 저장] product_key: "
            f"{html.escape(str(product_key or ''))}, "
            "이름: &lt;템플릿 이름&gt;"
            "</em>"
            "</span>"
            "</div>"
        )

        # 로컬 승인 바: approval_token 과 approval_port 가 모두 있을 때만 렌더.
        # 기본 OFF — 설정이 꺼져 있으면 token/port 가 None 이므로 이 바는 나오지
        # 않는다. 기존 클립보드 경로(수정사항 복사)는 그대로 동작한다.
        if approval_token and approval_port:
            safe_token = html.escape(str(approval_token), quote=True)
            # 순수 HTML 폼 POST. enctype 생략 = application/x-www-form-urlencoded
            # (CORS 프리플라이트를 유발하지 않는 "simple request" 조건). 커스텀
            # 헤더는 일절 없다 — 토큰은 hidden 필드로 본문에 보낸다.
            # [승인] 은 type="submit" 이고 JS 가 없어도 폼이 전송된다. [수정 후 승인]
            # 도 type="submit" 이며 JS 가 hidden edits[...] 필드를 채운 뒤 폼이 전송된다.
            action = f"http://127.0.0.1:{int(approval_port)}/"
            parts.append(
                '<div class="approval-bar">'
                '<span class="approval-bar-note">'
                "<strong>[승인] 버튼을 누르면 이 상품이 등록됩니다.</strong><br />"
                "[수정 후 승인] 은 페이지에서 바꾼 값을 함께 보냅니다. "
                "10분 안에 누르지 않으면 자동 만료됩니다. "
                "결과 페이지가 열리며, 서버가 처리한 실제 결과를 표시합니다."
                "</span>"
                f'<form id="approval-form" method="POST" action="{action}">'
                f'<input type="hidden" name="token" value="{safe_token}" />'
                f'<input type="hidden" name="product_key" value="{safe_pkey}" />'
                '<button type="submit" id="approval-btn" '
                'class="approval-btn approval-btn-approve">'
                "승인</button>"
                '<button type="submit" id="approval-btn-edit" '
                'class="approval-btn approval-btn-approve-edit" '
                'formaction="' + action + '">'
                "수정 후 승인</button>"
                "</form>"
                '<span id="approval-status" class="approval-status"></span>'
                "</div>"
            )

        parts.append("</div>")  # preview-wrap
        parts.append(_PREVIEW_EDIT_SCRIPT)
        # 승인 바가 있을 때만 승인 스크립트를 포함한다(불필요한 코드 노출 금지).
        if approval_token and approval_port:
            parts.append(_PREVIEW_APPROVAL_SCRIPT)
    else:
        parts.append("</div>")  # preview-wrap

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
    mode: PreviewMode = "view_only",
) -> Path:
    """미리보기 HTML 을 디스크에 쓰고 경로를 반환한다.

    prepared payload 디렉터리 규약 하위의 ``preview.html`` 로 쓴다.
    ``product_key`` 는 조작 모드에서 클립보드 수정사항 페이로드에 포함되도록
    페이지에 싣는다(모델이 어느 상품인지 정확히 식별).

    **모드**(``mode`` 인자)는 ``render_preview_html`` 과 동일한 의미다.
    **기본값은 ``"view_only"``** — 안전한 쪽. 호출부 배정:

      - ``register.prepare_listing`` 은 기본값(보기 전용) 그대로 호출 — 최초
        prepared 생성 직후이므로 패널에 바로 표시될 HTML 이 필요하다.
      - ``mcp_server.register_product`` (승인 대기 진입)는 ``mode="interactive"``
        를 **명시적으로** 전달 — 승인 서버 포트가 확정된 뒤 브라우저용으로
        파일을 갱신한다.

    ``approval_token`` 과 ``approval_port`` 가 모두 주어져도 **보기 전용 모드**
    에서는 승인 바가 렌더되지 않는다 — 패널은 폼을 제출할 수 없으므로 승인
    버튼은 죽은 UI 다. 조작 모드에서만 의미 있다.

    Args:
        product_key: prepared payload 의 product_key.
        payload: prepared payload dict.
        api_payload: 등록 단계 페이로드(선택). 고시 출처 표시에 쓴다.
        approval_token: 로컬 승인 다리 토큰(선택). 조작 모드에서만 승인 바 렌더.
        approval_port: 로컬 승인 서버 포트(선택). 조작 모드에서만 승인 바 렌더.
        mode: ``"view_only"`` (기본) 또는 ``"interactive"``.

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
        mode=mode,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_doc, encoding="utf-8")
    return path


__all__ = [
    "PreviewMode",
    "render_preview_html",
    "write_preview_html",
]
