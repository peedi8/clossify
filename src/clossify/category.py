# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""카테고리 분류 — 토큰 기반 후보 산출 + LLM 확정.

카테고리 분류 파이프라인.
구조:
  1. 입력 텍스트(제목 + 속성 + 상세)에서 토큰 스펙 산출
  2. ``category_meta`` 의 4,999건 카테고리 메타데이터를 후보 검증에 활용
  3. 각 카테고리 행에 대해 점수 계산(정확 일치 > 부분 일치 > 경로 포함)
  4. 강한 단일 후보가 있으면 확정; 분명치 않으면 ``common._llm_hint()`` 로
     MCP 호스트 LLM 에게 상위 후보 중 하나를 선택하도록 위임

과거 버전은 ``.local/naver_categories.json`` 에서만 카테고리를 읽었으나, 본
이식판은 ``category_meta`` 모듈(``data/category_meta.json``, 4,999건)을 우선
사용한다. ``category_meta`` 가 조회 불가능하면 빈 후보로 떨어지며, LLM 위임으로
넘어간다.

의존 방향: ``agent_calls`` (상위) → ``category`` (본 모듈).
``common``, ``seo``, ``text_props``, ``category_meta`` 는 어디서든 import 가능.
"""

from __future__ import annotations

import re

from . import common
from .text_props import _compact_spaces, _props_summary, _strip_banned_claims

# ---------------------------------------------------------------------------
# 카테고리 타입 별칭.
#
# 고가중치 "canonical type" 토큰과 그 별칭들. 한국어 리터럴은 토큰 매칭에 필요.
# ---------------------------------------------------------------------------
_CATEGORY_TYPE_ALIASES = (
    ("휴지통", ("휴지통", "쓰레기통")),
    ("버너", ("버너", "알코올버너", "스테인리스버너")),
    ("우산", ("우산", "접이식우산", "장우산")),
    ("화병", ("화병", "꽃병", "플라워베이스", "플라워베이스", "vase")),
    ("도자기", ("도자기", "도기", "세라믹", "토기")),
    ("접시", ("접시", "디저트접시", "스테이크접시", "원형접시")),
    ("찻잔", ("찻잔", "티컵", "머그잔", "커피잔")),
    ("쟁반", ("쟁반", "트레이", "서빙트레이")),
    ("조명", ("조명", "스탠드", "무드등", "램프", "전등")),
    ("초", ("초", "양초", "캔들")),
    ("시계", ("시계", "벽시계", "탁상시계")),
    ("거울", ("거울", "전신거울", "탁상거울")),
    ("액자", ("액자", "사진액자", "포스터액자")),
    ("식물", ("식물", "화분", "수반", "플랜터")),
    ("수납", ("수납", "정리함", "보관함", "선반")),
)

_CATEGORY_CONTEXT_ALIASES = (
    ("인테리어소품", 45),
    ("도자기", 25),
    ("원목", 15),
    ("빈티지", 10),
    ("앤틱", 10),
    ("미니", 8),
    ("북유럽", 8),
)

_CATEGORY_GENERIC_STOPWORDS = frozenset(
    {
        "상품",
        "제품",
        "옵션",
        "타입",
        "종류",
        "선택",
        "구성",
        "세트",
        "모음",
        "판매",
        "대형",
        "소형",
        "고급",
        "인기",
        "추천",
        "신상",
        "1개",
        "2개",
        "3개",
        "4개",
    }
)


# ---------------------------------------------------------------------------
# 카테고리 메타데이터 행 로더 (``category_meta`` 우선).
#
# 원본은 ``.local/naver_categories.json`` 만 읽었다. 본 이식판은
# ``category_meta.load_category_meta()`` 의 4,999건 verified 카탈로그를
# 사용한다. 조회 실패 시 빈 리스트(조용한 PASS 금지 — 호출자가 처리).
# ---------------------------------------------------------------------------

_CATEGORY_ROWS_CACHE: list[dict] | None = None


def _category_rows():
    """카테고리 메타데이터 행 리스트를 반환.

    ``category_meta.load_category_meta()`` 의 ``categories`` 배열을 사용.
    각 행은 ``{id, name, wholeCategoryName, exceptionalCategories, ...}``.

    캐싱: 한 프로세스 내에서 최초 1회만 로드.

    Raises:
        category_meta.CategoryMetaUnavailableError: 데이터 파일 부재.
    """
    global _CATEGORY_ROWS_CACHE
    if _CATEGORY_ROWS_CACHE is not None:
        return _CATEGORY_ROWS_CACHE
    from . import category_meta

    doc = category_meta.load_category_meta()
    rows = []
    for cat in doc.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cid = str(cat.get("id") or "")
        path = str(cat.get("wholeCategoryName") or "")
        names = [str(cat.get("name") or "")]
        leaf = names[0]
        rows.append(
            {
                "id": cid,
                "path": path,
                "names": names,
                "leaf": leaf,
                "depth": path.count(">") + 1,
                "search": _normalize_category_match_text(path + " " + " ".join(names)),
                "leaf_search": _normalize_category_match_text(leaf),
            }
        )
    _CATEGORY_ROWS_CACHE = rows
    return rows


def _normalize_category_match_text(text):
    """토큰 매칭용 텍스트 정규화.

    금지 표현 제거 → 한국어/라틴 경계에 공백 삽입 → 소문자화 →
    비영숫자 실행을 단일 공백으로 축약.
    """
    text = _strip_banned_claims(str(text or ""))
    text = re.sub(r"([가-힣])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9])([가-힣])", r"\1 \2", text)
    text = text.lower()
    text = re.sub(r"[^0-9a-z가-힣\s]", " ", text)
    return _compact_spaces(text)


def _category_input_has_term(text, term):
    """``text`` 안에 ``term`` 이 단어 단위로 존재하는지.

    음수 lookaround 로 부분문자열 오정합(예: "접시" 가 "디저트접시세트" 에
    걸리는 것)을 방지한다.
    """
    if not term or not text:
        return False
    pattern = r"(?<![0-9a-z가-힣])" + re.escape(term) + r"(?![0-9a-z가-힣])"
    return re.search(pattern, text) is not None


def _category_input_text(title_ko, props, desc_text="", *, include_desc=False):
    """토큰 매칭용 입력 해스택 구성."""
    parts = [str(title_ko or ""), _props_summary(props, max_terms=20)]
    if include_desc:
        parts.append(str(desc_text or "")[:1200])
    return _normalize_category_match_text(" ".join(p for p in parts if p))


def _category_token_specs(title_ko, props, desc_text=""):
    """가중 토큰 스펙 리스트 산출.

    세 패스:
      1. 타입 별칭 (고가중치 130-260)
      2. 컨텍스트 별칭 (저가중치 8-45)
      3. 일반 토큰 (가중치 18, 최대 12개)

    Returns:
        ``[{canonical, terms, weight, kind, matched}, ...]``
    """
    haystack = _category_input_text(title_ko, props, desc_text, include_desc=bool(desc_text))
    specs: list[dict] = []

    # Pass 1: 타입 별칭.
    for canonical, aliases in _CATEGORY_TYPE_ALIASES:
        matched = [
            a
            for a in aliases
            if _category_input_has_term(haystack, _normalize_category_match_text(a))
        ]
        if matched:
            specs.append(
                {
                    "canonical": canonical,
                    "terms": matched,
                    "weight": 260 if len(matched) >= 2 else 180,
                    "kind": "type",
                    "matched": matched,
                }
            )

    # Pass 2: 컨텍스트 별칭.
    for token, weight in _CATEGORY_CONTEXT_ALIASES:
        if _category_input_has_term(haystack, _normalize_category_match_text(token)):
            specs.append(
                {
                    "canonical": token,
                    "terms": [token],
                    "weight": weight,
                    "kind": "context",
                    "matched": [token],
                }
            )

    # Pass 3: 일반 토큰 (2자 이상 영숫자/한글).
    seen = {s["canonical"] for s in specs}
    for token in re.findall(r"[0-9a-z가-힣]{2,}", haystack):
        if token in seen or token in _CATEGORY_GENERIC_STOPWORDS:
            continue
        specs.append(
            {
                "canonical": token,
                "terms": [token],
                "weight": 18,
                "kind": "generic",
                "matched": [token],
            }
        )
        seen.add(token)
        if len(specs) >= 30:  # 최대 스펙 수 제한.
            break
    return specs


def _category_candidates_from_tokens(token_specs, *, limit=20):
    """토큰 스펙 vs 카테고리 행 scoring.

    각 (spec, row) 쌍에 대해:
      - 정확 leaf 일치: +weight*3
      - 부분 leaf 일치: +weight*2
      - path/name 해스택 일치: +weight

    Returns:
        상위 ``limit`` 개 후보. 각 ``{id, path, leaf, score, matched_terms, exact_leaf}``.
    """
    rows = _category_rows()
    scored: list[dict] = []
    for row in rows:
        score = 0
        matched_terms: list[str] = []
        exact_leaf = False
        leaf_search = row.get("leaf_search") or ""
        search = row.get("search") or ""
        for spec in token_specs:
            weight = spec.get("weight") or 0
            for term in spec.get("terms") or []:
                term_norm = _normalize_category_match_text(term)
                if not term_norm:
                    continue
                if term_norm == leaf_search:
                    score += weight * 3
                    matched_terms.append(term)
                    exact_leaf = True
                elif term_norm and leaf_search and term_norm in leaf_search:
                    score += weight * 2
                    matched_terms.append(term)
                elif term_norm and term_norm in search:
                    score += weight
                    if term not in matched_terms:
                        matched_terms.append(term)
        if score > 0:
            scored.append(
                {
                    "id": row.get("id"),
                    "path": row.get("path"),
                    "leaf": row.get("leaf"),
                    "score": score,
                    "matched_terms": matched_terms[:8],
                    "exact_leaf": exact_leaf,
                }
            )
    scored.sort(key=lambda c: (-c["score"], str(c.get("path")), str(c.get("id"))))
    return scored[:limit]


def _strong_category_candidate(candidates):
    """강한 단일 후보 판정.

    조건:
      - 정확 leaf 일치 + score≥500 + margin≥120, 또는
      - score≥700 + margin≥200
    """
    if not candidates:
        return None
    top = candidates[0]
    second_score = candidates[1]["score"] if len(candidates) > 1 else 0
    margin = top["score"] - second_score
    if top.get("exact_leaf") and top["score"] >= 500 and margin >= 120:
        return top
    if top["score"] >= 700 and margin >= 200:
        return top
    return None


# ---------------------------------------------------------------------------
# LLM 위임 — 카테고리 확정.
#
# 이 모듈은 ``common._llm_hint()`` 디스크립터를 반환하여 MCP 호스트가 상위
# 후보 중 하나를 선택하도록 위임한다.
# ---------------------------------------------------------------------------


def llm_select_category_hint(title_ko, props, desc_text, candidates):
    """카테고리 확정을 위한 ``llm_hint`` 디스크립터 반환.

    호스트 LLM 은 상위 후보 리스트 중 하나의 ``category_id`` 를 반환해야 한다.
    """
    from .text_props import _flatten_prop_terms

    payload = {
        "source_title": str(title_ko or ""),
        "props": _flatten_prop_terms(props, limit=20, clean=False),
        "desc_text": str(desc_text or "")[:1200],
        "candidates": [
            {
                "category_id": str(c.get("id") or ""),
                "path": str(c.get("path") or ""),
                "score": c.get("score"),
            }
            for c in (candidates or [])
        ],
    }
    instruction = (
        "당신은 네이버 스마트스토어 카테고리 분류기입니다. 입력 상품 정보와 "
        "후보 카테고리 리스트를 받아, 가장 적합한 카테고리 ID 하나를 선택해 "
        "반환한다. **반드시** candidates 에 있는 category_id 중 하나를 선택해야 "
        '한다. JSON 형태로 반환: {"category_id":"...","reason":"..."}'
    )
    return common._llm_hint(
        "classify_category",
        input=payload,
        instruction=instruction,
    )


def _llm_response_candidate_id(response):
    """LLM 회신 dict 에서 ``category_id`` 를 추출.

    Returns:
        후보 id 문자열, 또는 ``None`` (회신이 없거나 후보에 없는 id).
    """
    if not isinstance(response, dict):
        return None
    for key in ("category_id", "categoryId", "leaf_category_id", "id"):
        value = response.get(key)
        if value:
            return str(value)
    return None


# ---------------------------------------------------------------------------
# 공용 진입점 — classify_category.
# ---------------------------------------------------------------------------


def classify_category(title_ko, props, desc_text="", *, fallback=None):
    """카테고리 분류 공용 진입점.

    알고리즘:
      1. 토큰 스펙 산출 → 후보 scoring (``limit=20``)
      2. 카테고리 메타데이터가 없거나 후보가 없으면 ``fallback`` 반환
      3. 강한 단일 후보가 있으면 그 id 반환 (LLM 위임 없음)
      4. 분명치 않으면 ``llm_select_category_hint`` 디스크립터를 반환 —
         호스트 LLM 이 상위 후보 중 하나를 선택한다.

    Args:
        title_ko: 한국어 상품명.
        props: 상품 속성.
        desc_text: 상세 설명 텍스트 (선택).
        fallback: 분류 불가일 때 반환할 기본값.

    Returns:
        - 카테고리 id (str): 강한 후보가 확정된 경우.
        - ``llm_hint`` dict: LLM 판단이 필요한 경우 (호스트가 회신 후 재호출).
        - ``fallback``: 후보 자체가 없는 경우.
    """
    token_specs = _category_token_specs(title_ko, props, desc_text)
    try:
        candidates = _category_candidates_from_tokens(token_specs, limit=20)
    except Exception:
        # category_meta 조회 실패 — fail-closed: 후보 없음.
        candidates = []
    if not candidates:
        return fallback
    strong = _strong_category_candidate(candidates)
    if strong is not None:
        return str(strong.get("id") or "")
    # 분명치 않음 — LLM 위임.
    return llm_select_category_hint(title_ko, props, desc_text, candidates)


def category_path(category_id, *, raise_if_unknown=False):
    """카테고리 id 의 전체 경로 반환 (``category_meta.category_path`` 얇은 래퍼)."""
    from . import category_meta

    return category_meta.category_path(category_id, raise_if_unknown=raise_if_unknown)


__all__ = [
    "_category_candidates_from_tokens",
    "_category_input_has_term",
    "_category_input_text",
    "_category_rows",
    "_category_token_specs",
    "_llm_response_candidate_id",
    "_normalize_category_match_text",
    "_strong_category_candidate",
    "category_path",
    "classify_category",
    "llm_select_category_hint",
]
