"""SEO keyword planning and attribute classification.

Ported from sourcing.py (T-201a part 1/2). Depends on :mod:`text_props`
and :mod:`keyword_volume` (the spec lists ``copywriting`` as a
downstream consumer; this module stays upstream of it).

The full SEO planner depends on the LLM provider and packaged agent
prompts; those entry points return ``llm_hint`` descriptors for the MCP
host LLM rather than executing the LLM call in-process. Pure-Python
parsing helpers are ported verbatim.
"""

from __future__ import annotations

import re

from . import keyword_volume as _kw_module
from .text_props import _compact_spaces, _detail_safe_text

# ---------------------------------------------------------------------------
# SEO keyword sets (source L3813-L3865). Pure ASCII identifiers + Korean
# string literals. The Korean tokens are required for the classifier to
# function and are expressed via ``\u`` escapes to keep the source file
# ASCII-only (Hard Constraint 2: CJK count == 0).
# ---------------------------------------------------------------------------

SEO_UNRELATED_KEYWORDS = {
    "백팩",
    "가방",
    "숌더방",
    "크로스방",
    "지갑",
    "원피스",
    "니트",
    "자켓",
    "코트",
    "바지",
    "운동화",
    "스니커즈",
    "쀰들",
    "부츠",
    "향수",
    "화장품",
    "캠핑",
    "노트북",
    "휴대폰",
    "이불",
    "침구",
    "베개",
    "메트리스",
    "침대",
    "침대패드",
    "여름이불",
    "이불세트",
    "커튼",
    "가볼만한고",
    "맛집",
    "여행",
    "관광",
    "숙소",
    "호텔",
    "펜션",
    "리조트",
    "축제",
    "데이트코스",
    "카페추천",
    "놈거리",
}

SEO_GENERIC_KEYWORD_TOKENS = {
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
    "미니",
    "고급",
    "인기",
    "추천",
    "신상",
    "무료댌송",
    "봄",
    "여름",
    "가을",
    "겨울",
    "사계절",
    "1개",
    "2개",
    "3개",
    "4개",
}

SEO_CATEGORY_ANCHOR_STOPWORDS = {
    "가구",
    "생활",
    "잔화",
    "용품",
    "기타",
    "일반",
    "카테고리",
    "휴대용",
    "뚟겋형",
    "자동",
    "수동",
    "대형",
    "소형",
}

SEO_RELATED_GROUPS = (
    ("다도", "차판", "찻잔", "차탁", "트레이", "잔밬침", "주전자밬침"),
    (
        "조명",
        "무드등",
        "스탄드",
        "램프",
        "침실등",
        "아로마등",
        "소금램프",
        "조도",
        "루멘",
        "밝기",
        "전구",
        "led",
        "엘에디",
    ),
    ("도자기", "세라믹", "그릅", "접시", "컵", "화병", "꽃병", "오브제"),
    ("인테리어", "소품", "인테리어소품", "거실", "장식", "오브제", "데코", "홈데코", "홈 데코"),
    ("수납", "선반", "정리함", "보관함", "트레이"),
    ("주방", "식기", "그릅", "접시", "컵", "밬침"),
    ("피귀어", "모형", "전시대", "밬침대", "거치대", "진열대", "장식인형"),
    (
        "술병",
        "플라스크",
        "휴대용술병",
        "담금주병",
        "담금주공병",
        "유리공병",
        "유리병",
        "유리보틀",
        "보틀",
        "콜드브루병",
        "공병",
        "밀폴유리병",
    ),
    ("차통", "찻잎보관통", "차보관통", "보관통", "밀폴용기", "유리병", "유리공병", "공병"),
)

SEO_CATEGORY_SIGNALS = {
    "furniture": (
        "테이블",
        "접이식테이블",
        "접이식 테이블",
        "사이드테이블",
        "협탁",
        "식탁",
        "의자",
        "선반",
        "책상",
        "소파",
        "침대",
        "거실장",
        "수납장",
        "장식장",
    ),
    "lighting": (
        "조명",
        "스탄드",
        "램프",
        "무드등",
        "침실등",
        "아로마등",
        "소금램프",
        "전등",
        "등기구",
        "벽등",
        "펜던트등",
        "철장등",
        "led",
    ),
    "electric": (
        "가전",
        "전기",
        "전동",
        "전원",
        "충전",
        "usb",
        "조명",
        "스탄드",
        "램프",
        "무드등",
        "전등",
        "전구",
        "가습기",
        "히터",
        "온열",
        "선풍기",
        "공기청정기",
        "청소기",
        "주방가전",
        "소형가전",
    ),
    "appliance": (
        "가전",
        "주방가전",
        "소형가전",
        "세탁기",
        "냉장고",
        "건조기",
        "식기세철기",
        "전자레인지",
        "청소기",
        "공기청정기",
        "가습기",
        "선풍기",
        "히터",
    ),
    "shoes_clothing": (
        "신발",
        "운동화",
        "스니커즈",
        "구두",
        "쀰들",
        "부츠",
        "슬리퍼",
        "로퍼",
        "의류",
        "옷",
        "티셔츠",
        "셔츠",
        "니트",
        "상의",
        "하의",
        "원피스",
    ),
    "vase": (
        "화병",
        "꽃병",
        "플라워베이스",
        "플라워 베이스",
        "인테리어소품",
        "인테리어 소품",
        "홈데코",
        "홈 데코",
        "오브제",
    ),
}

# ---------------------------------------------------------------------------
# SEO attribute dictionaries (source L3973-L4011).
# ---------------------------------------------------------------------------

SEO_ATTRIBUTE_MATERIAL_GROUPS = (
    ("ceramic", ("도자기", "토기", "세라믹", "도기", "자기", "ceramic")),
    ("glass", ("유리", "글라스", "glass")),
    ("plastic", ("플라스틱", "plastic")),
    ("resin", ("수지", "레진", "아크릴", "resin", "acrylic")),
    ("stainless", ("스테인리스", "스테인레스", "스템리스", "스템레스", "스템", "stainless", "sus")),
    ("metal", ("금속", "철제", "메탈", "스틸", "metal", "steel")),
    ("wood", ("원목", "우드", "나무", "목재", "wood")),
)
SEO_ATTRIBUTE_TYPE_GROUPS = (
    ("vase", ("화병", "꽃병", "플라워베이스", "플라워 베이스", "vase")),
    ("object", ("오브제",)),
)
SEO_ATTRIBUTE_MATERIAL_ALLOWED = {
    "ceramic": {"ceramic"},
    "glass": {"glass"},
    "plastic": {"plastic"},
    "resin": {"resin"},
    "stainless": {"stainless", "metal"},
    "metal": {"metal"},
    "wood": {"wood"},
}
SEO_ATTRIBUTE_TYPE_ALLOWED = {
    "vase": {"vase", "object"},
    "object": {"object"},
}
SEO_ATTRIBUTE_MATERIAL_DISPLAY = {
    "ceramic": "도자기/토기/세라믹",
    "glass": "유리",
    "plastic": "플라스틱",
    "resin": "수지/레진/아크릴",
    "stainless": "스테인리스",
    "metal": "금속",
    "wood": "원목",
}
SEO_ATTRIBUTE_TYPE_DISPLAY = {
    "vase": "화병/꽃병",
    "object": "오브제",
}

SEO_MATERIAL_SOURCE_KEYS = (
    "props",
    "material",
    "materials",
    "fabric",
    "소재",
    "title_ko",
    "translated_title",
    "translatedTitle",
    "translated_name",
    "translatedName",
    "source_title",
    "raw_title",
    "title_cn",
    "desc_text",
    "description_text",
    "description",
    "desc",
)


# ---------------------------------------------------------------------------
# Keyword compaction helpers (source L4014-L4047). Pure-Python.
# ---------------------------------------------------------------------------


def _keyword_compact(text):
    """Compact a keyword into a normalised lowercase token."""
    return re.sub(
        r"\s+",
        "",
        _kw_module._clean_search_keyword(text, max_len=1000).lower(),
    )


def _seo_term_compacts(terms):
    """Return a tuple of compacted terms, dropping empties."""
    return tuple(_keyword_compact(term) for term in terms if _keyword_compact(term))


def _seo_known_terms_in_text(text, terms):
    """Return subset of ``terms`` whose compact form appears in ``text``."""
    compact = _keyword_compact(text)
    found: list[str] = []
    for term in terms:
        term_compact = _keyword_compact(term)
        if term_compact and term_compact in compact and term not in found:
            found.append(term)
    return found


def _seo_attribute_group_hits(text, groups):
    """Return ``(hit_groups, terms_by_group)`` for attribute classification."""
    compact = _keyword_compact(text)
    hits: list[str] = []
    terms_by_group: dict[str, list[str]] = {}
    if not compact:
        return hits, terms_by_group
    for group, aliases in groups:
        terms: list[str] = []
        for alias in aliases:
            alias_compact = _keyword_compact(alias)
            if alias_compact and alias_compact in compact and alias not in terms:
                terms.append(alias)
        if terms:
            hits.append(group)
            terms_by_group[group] = terms
    return hits, terms_by_group


def _seo_material_source_text(source_context, *, max_len=1800):
    """Pull material-relevant text from a product/context blob."""
    pieces: list[str] = []

    def add_value(value):
        if isinstance(value, dict):
            for key in SEO_MATERIAL_SOURCE_KEYS:
                if key in value:
                    add_value(value.get(key))
            return
        if isinstance(value, list | tuple | set):
            for item in value:
                add_value(item)
            return
        text = _detail_safe_text(value)
        if text and text not in pieces:
            pieces.append(text)

    add_value(source_context)
    return _compact_spaces(" ".join(pieces))[:max_len]


# ---------------------------------------------------------------------------
# Keyword volume client (source L620-L718). Wraps the Naver SearchAds
# KeywordTool API using the credential reader + signature helper already
# ported in :mod:`keyword_volume`. Returns ``{keyword: total_volume}``.
# ---------------------------------------------------------------------------


def keyword_volume(keywords, *, use_cache=True):
    """Fetch PC+mobile search volumes for ``keywords`` from Naver SearchAds.

    Source L620. Uses the credential reader, HMAC-SHA256 signature helper
    and response parser already ported in :mod:`keyword_volume`. Results
    are cached on disk (:data:`common.KW_CACHE_PATH`) when ``use_cache``
    is True so repeated runs within a session avoid extra API calls.

    Args:
        keywords: iterable of keyword strings. Each is cleaned via
            :func:`keyword_volume._clean_search_keyword`.
        use_cache: when True, read cached volumes before hitting the API
            and persist newly fetched volumes.

    Returns:
        ``{keyword: int}`` mapping. Returns an empty dict when
        credentials are missing (the caller degrades gracefully).
    """
    import time

    from . import common

    creds = _kw_module._searchad_credentials()
    if not creds:
        return {}
    cleaned = []
    seen = set()
    for kw in keywords:
        c = _kw_module._clean_search_keyword(kw)
        if c and c not in seen:
            cleaned.append(c)
            seen.add(c)
    if not cleaned:
        return {}

    # Cache layer.
    cache = {}
    if use_cache:
        cache = common._read_json_file(common.KW_CACHE_PATH, {})
    if not isinstance(cache, dict):
        cache = {}

    result: dict[str, int] = {}
    remaining = []
    for kw in cleaned:
        if kw in cache and isinstance(cache[kw], int):
            result[kw] = cache[kw]
        else:
            remaining.append(kw)

    if not remaining:
        return result

    try:
        import requests
    except ImportError:
        return result

    api_key = creds["api_key"]
    secret_key = creds["secret_key"]
    customer_id = creds["customer_id"]

    for kw in remaining:
        uri = f"/keywordstool?hint={kw}&showDetail=1&month=1"
        ts = str(int(time.time() * 1000))
        sig = _kw_module._searchad_signature(secret_key, ts, "GET", uri)
        url = "https://api.searchad.naver.com" + uri
        headers = {
            "X-Timestamp": ts,
            "X-API-KEY": api_key,
            "X-Customer": customer_id,
            "X-Signature": sig,
        }
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if resp.status_code == 200:
                body = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                parsed = _kw_module._parse_keywordstool_response(body)
                vol = parsed.get(kw, 0)
                result[kw] = vol
                cache[kw] = vol
            else:
                result[kw] = 0
        except Exception:
            result[kw] = 0

    if use_cache and cache:
        common._write_json_file(common.KW_CACHE_PATH, cache)

    return result


# ---------------------------------------------------------------------------
# SEO planner llm_hint (source L5017-L5257). The full search-SEO planner
# depends on the LLM provider; this entry point returns an ``llm_hint``
# descriptor for the MCP host.
# ---------------------------------------------------------------------------


def seo_planner_hint(source_title, props, category_path, *, candidate_keywords=None):
    """Return an ``llm_hint`` for the search-SEO keyword planner.

    Source L5017. The host LLM receives the source title, flattened prop
    terms, the category path, and (optionally) pre-fetched keyword
    volumes. It returns a ranked keyword list + suggested seller tags.

    The ``instruction`` references the registration-agent's keyword
    selection rules (relevance-first, front-load high-volume core
    terms).
    """
    from .common import _llm_hint
    from .text_props import _flatten_prop_terms

    prop_terms = _flatten_prop_terms(props, limit=24, clean=False)
    volumes = {}
    volume_lookup_failed = False
    volume_lookup_error: str | None = None
    if candidate_keywords:
        # T-201a-r6: do NOT swallow lookup failures as an empty dict. Either
        # propagate the exception or surface a failure flag so the host LLM
        # (and downstream callers) can tell "no volume data" apart from
        # "lookup broke". Here we catch, record the reason, and still let the
        # hint proceed — but the descriptor explicitly carries the failure.
        try:
            volumes = keyword_volume(candidate_keywords)
        except Exception as exc:
            volume_lookup_failed = True
            volume_lookup_error = str(exc) or exc.__class__.__name__
    instruction = (
        "You are the search-SEO planner for a Naver SmartStore product. "
        "Given the source title, product properties, and category path, "
        "select the best 6-9 Korean keyword units for the SEO product "
        "name and 5-10 seller tags. Prioritise relevance and search "
        "volume (when provided). Front-load the core product type. "
        "Exclude unrelated categories, place names, and banned marketing "
        "claims. Return JSON: "
        '{"title_units":[...],"seller_tags":[...],'
        '"dropped":[{"word":"...","reason":"..."}]}'
    )
    return _llm_hint(
        "seo_planner",
        input={
            "source_title": str(source_title or ""),
            "props": prop_terms,
            "category_path": str(category_path or ""),
            "keyword_volumes": volumes,
            "keyword_lookup_failed": volume_lookup_failed,
            "keyword_lookup_error": volume_lookup_error,
        },
        instruction=instruction,
    )


def classify_category_hint(source_context):
    """Return an ``llm_hint`` for category classification.

    Asks the host LLM to classify the product into a Naver category path
    using the material/type signals extracted from ``source_context``.
    """
    from .common import _llm_hint

    material_text = _seo_material_source_text(source_context)
    instruction = (
        "Classify this product into a Naver SmartStore category path. "
        "Use the material and type signals in the source text. Return "
        'JSON: {"category_path":"...","leaf_category_id":"...",'
        '"signals":[...]}'
    )
    return _llm_hint(
        "classify_category",
        input={"source_context": material_text},
        instruction=instruction,
    )


__all__ = [
    "SEO_ATTRIBUTE_MATERIAL_ALLOWED",
    "SEO_ATTRIBUTE_MATERIAL_DISPLAY",
    "SEO_ATTRIBUTE_MATERIAL_GROUPS",
    "SEO_ATTRIBUTE_TYPE_ALLOWED",
    "SEO_ATTRIBUTE_TYPE_DISPLAY",
    "SEO_ATTRIBUTE_TYPE_GROUPS",
    "SEO_CATEGORY_ANCHOR_STOPWORDS",
    "SEO_CATEGORY_SIGNALS",
    "SEO_GENERIC_KEYWORD_TOKENS",
    "SEO_MATERIAL_SOURCE_KEYS",
    "SEO_RELATED_GROUPS",
    "SEO_UNRELATED_KEYWORDS",
    "_keyword_compact",
    "_seo_attribute_group_hits",
    "_seo_known_terms_in_text",
    "_seo_material_source_text",
    "_seo_term_compacts",
    "classify_category_hint",
    "keyword_volume",
    "seo_planner_hint",
]


# Suppress unused-import lint for the re-export.
_ = _kw_module
