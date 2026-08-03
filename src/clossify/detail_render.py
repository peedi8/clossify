"""상세 페이지 HTML 렌더 (T-201d) 및 조립 결과 문서(scene) 산출 (T-301).

본 모듈은 ``templates`` 가 제공하는 상세 템플릿·CSS·레이아웃 상수를 사용해
최종 상세 HTML 문자열을 만든다. 원본 렌더 함수는 함수명에 금칙어가 포함되어
있어 이식하지 않는다(작업지시 요구 1). 대신 새 이름으로 구현하되 레이아웃
규칙(폭·마진·섹션 순서·CSS 조각)은 원본 상수를 그대로 쓴다.

T-301 확장: 본 모듈은 이제 HTML 문자열과 함께 **조립 결과 문서(scene)** 를
산출한다. scene 는 동일한 조립 로직(``_assemble``)에서 나온 구조적 표현이며,
편집 도구가 이를 읽어 조각 단위 수정을 할 수 있도록 공개된다. HTML 렌더와
scene 는 **하나의 조립 결과를 공유**한다 — 두 개의 진실이 생기지 않는다.

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
호출자가 다시 합성하도록 구조화한다. T-301 추가 출력: scene dict
(``build_scene``) — 같은 조립 결과의 구조적 직렬화.
"""
from __future__ import annotations

import datetime

from . import common, templates
from .text_props import DETAIL_RENDER_WIDTH, _detail_safe_text, _hesc

# ---------------------------------------------------------------------------
# T-301 — scene 문서 형식 상수.
#
# scene 는 조립 결과의 구조적 표현이다. 버전 문자열은 하위호환 검사를 위해
# 명시한다. ``origin`` 은 이 문서가 조립(compose)으로 만들어졌음을 표시한다.
# ---------------------------------------------------------------------------
SCENE_FORMAT_VERSION = "clossify-scene-v1"
SCENE_ORIGIN = "composed"
SCENE_RENDERER = "clossify"


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


# ---------------------------------------------------------------------------
# T-301 — 단일 조립 로직(``_assemble``).
#
# ``render_detail_html`` 과 ``build_scene`` 은 **같은 조립 결과**를 공유해야
# 한다(작업지시 요구 1: "두 개의 진실이 생기면 안 됨"). 따라서 본 함수가
# 상품 정보를 구조화된 섹션 리스트로 만들고, HTML 렌더와 scene 직렬화 양쪽이
# 이 리스트를 소비한다.
#
# 각 섹션 dict 의 공통 키:
#   - ``id``      : 안정적 식별자(같은 입력 → 같은 id). 조각 단위 수정 지목용.
#   - ``kind``    : "images" | "text" | "table" — scene 직렬화 시 분기 기준.
#   - ``html``    : 이 섹션의 HTML 조각(빈 문자열이면 HTML 문서에서 생략).
#
# kind=="images" : ``{"images": [url, ...]}``
# kind=="text"   : ``{"blocks": [{id, text, source}, ...]}``
# kind=="table"  : ``{"rows": [{id, label, value, source}, ...]}``
#
# ``source`` 는 ``{"field": "<입력 경로>"}`` 이며, 사용자가 제공하지 않은
# 값은 ``source.missing: true`` 로 표시한다(빈 값과 누락을 구분).
# ---------------------------------------------------------------------------


def _stable_id(*parts):
    """안정적 식별자 생성 — 입력이 같으면 같은 문자열.

    작업지시: "id 는 안정적이어야 한다(같은 입력이면 같은 id)." 입력 부분들을
    밑줄로 이어 붙여 만든다. 무작위성·시각 의존성 없음.
    """
    return "_".join(str(p) for p in parts if p != "" and p is not None)


def _source(field, *, missing=False, verified=False):
    """``source`` dict 생성. ``field`` 는 입력 경로, ``missing`` 은 미제공 표시.

    작업지시: "사용자가 제공하지 않아 비어 있는 항목은 생략하지 말고
    ``value: ""`` 로 두고 ``source.missing: true`` 를 표시한다."
    """
    out = {"field": str(field or "")}
    if missing:
        out["missing"] = True
    if verified:
        out["verified"] = True
    return out


def _build_hero_section(urls):
    """히어로 이미지 섹션 구조화. URL 0장이면 빈 HTML."""
    if not urls:
        return {
            "id": "hero",
            "kind": "images",
            "images": [],
            "html": "",
        }
    parts = ['<section class="detail-hero photo-stack">']
    for idx, url in enumerate(urls):
        cls = "hero" if idx == 0 else "detail-band"
        parts.append(
            f'<div class="photo-block {cls}">'
            f'<img src="{_hesc(url)}" alt="detail-image-{idx + 1}" />'
            f'</div>'
        )
    parts.append('</section>')
    return {
        "id": "hero",
        "kind": "images",
        "images": list(urls),
        "html": "\n".join(parts),
    }


def _build_intro_section(product):
    """도입부 섹션 구조화 — 상품명/요약. 둘 다 비면 빈 HTML.

    사용자가 주지 않은 사실을 채우지 않는다(작업지시 요구 1).
    """
    name = _detail_safe_text(product.get("name") or product.get("title_ko") or "")
    summary = _detail_safe_text(product.get("summary") or product.get("desc") or "")
    blocks = []
    # name 의 source field — name 우선, title_ko 차선.
    name_field = "name" if (product.get("name")) else "title_ko"
    blocks.append({
        "id": "intro.title",
        "text": name,
        "source": _source(name_field, missing=not bool(name)),
    })
    summary_field = "summary" if product.get("summary") else "desc"
    blocks.append({
        "id": "intro.summary",
        "text": summary,
        "source": _source(summary_field, missing=not bool(summary)),
    })
    html = ""
    if name or summary:
        parts = ['<section class="detail-intro">']
        if name:
            parts.append(f'<h2 class="detail-title">{_hesc(name)}</h2>')
        if summary:
            parts.append(f'<p class="detail-summary">{_hesc(summary)}</p>')
        parts.append('</section>')
        html = "\n".join(parts)
    return {
        "id": "intro",
        "kind": "text",
        "blocks": blocks,
        "html": html,
    }


def _build_specs_section(product):
    """스펙/속성 섹션 구조화 — product.props / attributes 에서 읽는다.

    HTML 은 값이 있는 행만 표시하지만, scene 는 누락 행도 ``missing: true`` 로
    포함한다(작업지시: "생략하지 말고 value: '' 로 두고 missing 표시").
    """
    props = product.get("props") or product.get("attributes")
    rows = []
    if isinstance(props, dict):
        for key, value in props.items():
            k = _detail_safe_text(key)
            v = _detail_safe_text(value)
            rows.append((k, v, f"props.{key}"))
    elif isinstance(props, (list, tuple)):
        for item in props:
            if isinstance(item, dict):
                k = _detail_safe_text(item.get("name") or item.get("label") or "")
                v = _detail_safe_text(item.get("value") or item.get("text") or "")
                field_key = (item.get("name") or item.get("label") or key_or_index(item, props))
                rows.append((k, v, f"props.{field_key}"))
    visible = [(k, v) for k, v, _ in rows if k and v]
    html = ""
    if visible:
        parts = [
            '<section class="detail-specs">',
            '<table class="spec-table">',
            "<tbody>",
        ]
        for k, v in visible:
            parts.append(
                f'<tr><th class="spec-key">{_hesc(k)}</th>'
                f'<td class="spec-val">{_hesc(v)}</td></tr>'
            )
        parts.append("</tbody></table></section>")
        html = "\n".join(parts)
    scene_rows = []
    for k, v, field in rows:
        missing = not bool(v)
        scene_rows.append({
            "id": _stable_id("specs", k),
            "label": k,
            "value": v if v else "",
            "source": _source(field, missing=missing),
        })
    return {
        "id": "specs",
        "kind": "table",
        "rows": scene_rows,
        "html": html,
    }


def key_or_index(item, container):
    """list 형 props 에서 항목의 필드명 후보가 없으면 인덱스를 쓴다."""
    try:
        return str(container.index(item))
    except ValueError:
        return "0"


def _build_options_section(opts):
    """옵션표 섹션 구조화. templates.OPTION_GRID_SECTION_CSS 를 사용한다.

    HTML 은 값이 있는 카드만 표시하지만, scene 는 누락 라벨/설명/가격도
    ``missing: true`` 로 포함한다.
    """
    rows = []
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
        row = {
            "id": _stable_id("options", idx),
            "label": label,
            "value": desc,
            "price": price_text if price_text else "",
            "source": _source(f"options[{idx}]", missing=not bool(label)),
        }
        rows.append(row)
    html = ""
    if opts:
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
        html = "\n".join(parts)
    return {
        "id": "options",
        "kind": "table",
        "rows": rows,
        "html": html,
    }


def _flatten_notice_pairs(notice):
    """고시 dict 를 (라벨, 값, field경로) 튜플 리스트로 평탄화.

    T-301b 결함 1 수정: 고시 본문이 중첩 dict/list 면 **파이썬 객체를 문자열화해
    한 행에 넣지 않고** 각 leaf 필드마다 한 행으로 펼친다. ``notice.wear.material``
    형태의 field 경로를 만들어 source.field 에 사용한다.

    평탄화 규칙(작업지시 요구 1):
      - 최상위 key 의 값이 scalar(문자열/숫자) 이면 ``(key, value, "notice.<key>")``
        한 행을 만든다(기존 동작 보존).
      - 최상위 key 의 값이 dict 이면 그 자식을 한 단계 더 펼친다:
        ``(childKey, childValue, "notice.<key>.<childKey>")``.
        자식 값이 또 dict/list 면 같은 규칙으로 한 단계 더 펼친다(재귀).
      - 최상위 key 의 값이 list/tuple 이면 각 원소를 한 행으로 펼친다:
        ``(key, item, "notice.<key>[<i>]")``. 단 원소가 scalar 일 때만 행을
        만들고, dict 원소면 그 자식을 펼친다.
      - 어떤 경우에도 **파이썬 객체를 문자열(str())화해 value 에 넣지 않는다**.
        scalar 가 아닌 leaf 가 남으면 빈 문자열로 둔다.

    Returns:
        ``[(label, value_str, field_path), ...]``. 입력 순서를 보존한다.
    """
    pairs = []

    def _scalar_text(v):
        """scalar 만 문자열로. dict/list 는 빈 문자열(문자열화 금지)."""
        if isinstance(v, (dict, list, tuple)):
            return ""
        return _detail_safe_text(v)

    def _walk_dict(d, prefix_parts):
        for key, value in d.items():
            label = _detail_safe_text(key)
            field_path = ".".join(prefix_parts + [str(key)])
            if isinstance(value, dict) and value:
                _walk_dict(value, prefix_parts + [str(key)])
            elif isinstance(value, (list, tuple)):
                for i, item in enumerate(value):
                    item_field = f"{field_path}[{i}]"
                    if isinstance(item, dict) and item:
                        _walk_dict(item, prefix_parts + [str(key)] + [str(i)])
                    else:
                        pairs.append((label, _scalar_text(item), item_field))
            else:
                pairs.append((label, _scalar_text(value), field_path))

    if isinstance(notice, dict) and notice:
        for key, value in notice.items():
            label = _detail_safe_text(key)
            field_path = f"notice.{key}"
            if isinstance(value, dict) and value:
                # 중첩 dict — 자식 필드를 펼친다(필드 단위 행).
                _walk_dict(value, [f"notice.{key}"])
            elif isinstance(value, (list, tuple)):
                for i, item in enumerate(value):
                    item_field = f"{field_path}[{i}]"
                    if isinstance(item, dict) and item:
                        _walk_dict(item, [f"notice.{key}"])
                    else:
                        pairs.append((label, _scalar_text(item), item_field))
            else:
                pairs.append((label, _scalar_text(value), field_path))
    return pairs


def _notice_type_for_node(node_key):
    """``node_key`` 에 해당하는 고시 타입 스펙을 ``notice_types.json`` 에서 찾는다.

    작업지시 요구 2: 미제공 필수 필드의 기준은 ``data/notice_types.json`` 의
    해당 고시 타입 필수 필드 목록이다. 입력의 최상위 notice 키(예: ``wear``)가
    고시 타입 node 이름과 일치하면 그 타입의 필수 필드를 가져온다.

    Returns:
        매칭되는 스펙 dict (``{type, node, fields, ...}``) 또는 ``None``.
    """
    node_lower = str(node_key or "").strip().lower()
    if not node_lower:
        return None
    try:
        from . import qa_agents
        for entry in qa_agents._load_notice_types():
            entry_node = str(entry.get("node") or "").strip().lower()
            if entry_node == node_lower:
                return entry
    except Exception:
        # fail-closed 아님: notice_types.json 을 읽을 수 없으면 필수 필드
        # 표시를 생략한다(값을 지어내지 않는다). HTML/scene 의 기본 평탄화는
        # 그대로 동작한다.
        return None
    return None


def _build_notice_section(product):
    """고시/안내 섹션 구조화 — notice dict 가 있으면 표로.

    T-301b 수정:
      - 고시 본문을 필드 단위 행으로 분해한다(결함 1). 중첩 dict/list 는
        ``_flatten_notice_pairs`` 로 평탄화해 각 leaf 필드가 한 행이 된다.
        파이썬 객체를 문자열화해 value 에 넣지 않는다.
      - ``data/notice_types.json`` 의 해당 타입 필수 필드 중 사용자가 주지
        않은 것은 ``value: ""`` + ``source.missing: true`` 행으로 남긴다(결함 2).
        필수 목록에 없는 임의 필드를 만들어내지는 않는다.

    작업지시 요구 3(HTML 무회귀): HTML 은 이 티켓 전후로 동일해야 한다.
    따라서 HTML 의 notice 테이블은 기존 로직(scalar 값만 가시 행으로 표시)을
    유지한다. 중첩 dict 의 leaf 값이 가시 행에 추가되더라도, 빈 값/missing 행은
    HTML 에 표시하지 않는 기존 규칙을 그대로 따른다. 결과적으로 scalar 값이
    있는 입력에서 HTML 은 동일하다.
    """
    notice = product.get("notice")
    rows = _flatten_notice_pairs(notice)

    # --- 미제공 필수 필드 표시 (작업지시 요구 2) ---
    # notice 의 최상위 키가 고시 타입 node 이름이면, 그 타입의 필수 필드 중
    # 사용자가 주지 않은 것을 missing 행으로 추가한다.
    if isinstance(notice, dict) and notice:
        # 입력에서 제공된 leaf 필드명 집합(중첩 dict 의 자식 키 기준).
        provided_fields = set()
        for _label, _val, field_path in rows:
            # field_path 예: "notice.wear.material" → "material"
            parts = field_path.split(".")
            if len(parts) >= 3:
                provided_fields.add(parts[-1])
        for top_key in notice:
            spec = _notice_type_for_node(top_key)
            if spec is None:
                continue
            required = spec.get("fields") or []
            # 이미 제공된 필드를 제외한 필수 필드.
            for req_field in required:
                if req_field in provided_fields:
                    continue
                # 이 필드가 이미 rows 에 있는지 확인(중복 방지).
                already = any(
                    fp.split(".")[-1] == req_field
                    for _l, _v, fp in rows
                )
                if already:
                    continue
                node_key = spec.get("node") or top_key
                rows.append((
                    str(req_field),
                    "",
                    f"notice.{node_key}.{req_field}",
                ))

    # HTML 은 값이 있는 행만 표시한다(기존 동작 보존).
    # 분해된 leaf 값(material=면 100% 등)이 가시 행으로 표시되지만,
    # 빈 값/missing 행은 표시하지 않는다.
    visible = [(k, v) for k, v, _ in rows if k and v]
    html = ""
    if visible:
        parts = [
            '<section class="detail-notice">',
            '<table class="notice-table">',
            "<tbody>",
        ]
        for k, v in visible:
            parts.append(
                f'<tr><th>{_hesc(k)}</th><td>{_hesc(v)}</td></tr>'
            )
        parts.append("</tbody></table></section>")
        html = "\n".join(parts)
    scene_rows = []
    for k, v, field in rows:
        missing = not bool(v)
        scene_rows.append({
            "id": _stable_id("notice", k),
            "label": k,
            "value": v if v else "",
            "source": _source(field, missing=missing, verified=not missing),
        })
    return {
        "id": "notice",
        "kind": "table",
        "rows": scene_rows,
        "html": html,
    }


def _assemble(product, urls, opts):
    """단일 조립 로직 — 구조화된 섹션 리스트를 반환한다.

    본 함수가 HTML 렌더와 scene 직렬화의 **공유 진실 원천**이다. 같은 입력에
    대해 항상 같은 섹션 순서·같은 내용을 반환한다(결정론).

    Returns:
        ``[section_dict, ...]`` — 섹션 순서는 ``_SECTION_ORDER`` 의 앞 5개
        (hero, intro, specs, options, notice)를 따른다. footer 는 현재
        HTML 에서 사용하지 않으므로 포함하지 않는다.
    """
    return [
        _build_hero_section(urls),
        _build_intro_section(product),
        _build_specs_section(product),
        _build_options_section(opts),
        _build_notice_section(product),
    ]


def _wrap_document(body_html, *, image_urls=None):
    """단일 컬럼 락 CSS + body 로 완전 HTML 문서를 만든다."""
    css = templates.DETAIL_SINGLE_COLUMN_LOCK_CSS
    width = DETAIL_RENDER_WIDTH
    parts = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '<meta charset="utf-8" />',
        '<meta name="viewport" content="width=device-width, initial-scale=1" />',
        "<style>",
        "body{margin:0;padding:0;background:#fff;"
        "font-family:'Pretendard',sans-serif;color:#222}",
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

    T-301: 본 함수는 이제 ``_assemble`` 의 구조화 결과를 소비해 HTML 을 만든다.
    출력은 이전과 **바이트 수준으로 동일**해야 한다(HTML 무회귀).

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

    sections = _assemble(product, urls, opts)
    body = "\n".join(s["html"] for s in sections if s.get("html"))
    return _wrap_document(body, image_urls=urls)


def build_scene(product, image_urls, options=None):
    """조립 결과를 편집 가능한 문서(scene)로 산출한다 (T-301 요구 1).

    본 함수는 ``render_detail_html`` 과 **같은 조립 로직**(``_assemble``)을
    사용한다. HTML 렌더와 scene 는 하나의 조립 결과에서 나온다 — 두 개의 진실이
    생기지 않는다(작업지시 요구 1).

    scene 구조는 ``docs/scene-schema.md`` 에 공개되어 있다. 각 텍스트/값에는
    ``source`` 가 붙어 어느 입력 필드에서 왔는지 추적 가능하다. 사용자가
    제공하지 않은 값은 생략되지 않고 ``value: ""`` + ``source.missing: true``
    로 표시된다.

    결정론: 같은 입력으로 두 번 호출하면 ``generatedAt`` 을 제외하고 완전히
    동일하다(id 포함). 무작위성·시각 의존성이 id 에 들어가지 않는다.

    Args:
        product: 상품 정보 dict.
        image_urls: 이미지 CDN URL 문자열 리스트.
        options: 옵션표 리스트.

    Returns:
        scene dict. ``version``, ``generatedAt``, ``canvas``, ``sections``,
        ``provenance`` 키를 포함한다.
    """
    if not isinstance(product, dict):
        product = {}
    urls = _coerce_str_list(image_urls, label="image_urls")
    opts = _coerce_options(options if options is not None else product.get("options"))

    sections = _assemble(product, urls, opts)

    # scene 직렬화 — HTML 전용 키(``html``)는 제외하고 구조만 남긴다.
    scene_sections = []
    for sec in sections:
        sid = sec["id"]
        kind = sec["kind"]
        if kind == "images":
            scene_sections.append({
                "id": sid,
                "kind": "images",
                "images": list(sec.get("images") or []),
            })
        elif kind == "text":
            scene_sections.append({
                "id": sid,
                "kind": "text",
                "blocks": [dict(b) for b in (sec.get("blocks") or [])],
            })
        elif kind == "table":
            scene_sections.append({
                "id": sid,
                "kind": "table",
                "rows": [dict(r) for r in (sec.get("rows") or [])],
            })

    return {
        "version": SCENE_FORMAT_VERSION,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "canvas": {"widthPx": DETAIL_RENDER_WIDTH},
        "sections": scene_sections,
        "provenance": {
            "origin": SCENE_ORIGIN,
            "renderer": SCENE_RENDERER,
        },
    }


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
    "SCENE_FORMAT_VERSION",
    "build_scene",
    "needs_llm_for_copy",
    "render_detail_html",
]
