# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Text and property extraction helpers.

Ported from the original sourcing pipeline. Depends on :mod:`common`.

All Chinese (Hanja) detection and stripping code has been
removed entirely. This product only ingests Korean user-supplied text,
so there is no input path that could carry Chinese ideographs. The
Korean marketing-claim filters are preserved and use literal Korean
characters.

This module is now the canonical home of the text-filter
regexes (``BANNED_CLAIM_RE``, ``EDITORIAL_NOISE_RE``, ...). Downstream
modules (``copywriting``) import them from here. This module no longer
imports any other ``clossify`` submodule — the previous lazy import of
``BANNED_CLAIM_RE`` from :mod:`copywriting` (which created a cycle) is
gone. The translation / external-market prop helpers have been removed
(this product only ingests Korean user-supplied text).
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# This module must not import any other ``clossify`` submodule
# (top-level or lazy). It is the upstream node of the DAG; ``copywriting``
# and ``seo`` import from here, never the reverse. ``_safe_float`` is
# available from :mod:`common` directly — do not re-export it here.

# ---------------------------------------------------------------------------
# Image / detail rendering limits. Pure literals.
# ---------------------------------------------------------------------------

MAIN_IMAGE_LIMIT = None
LISTING_IMAGE_LIMIT = None
DESC_IMAGE_SCAN_LIMIT: int = 32
OPTION_GRID_LIMIT = None
DETAIL_RENDER_WIDTH: int = 1000
DETAIL_CONTENT_TARGET: int = 860
DETAIL_ASPECT_TALL: float = 1.3
DETAIL_IMAGES_MIN: int = 5
DETAIL_IMAGES_MAX: int = 10
DETAIL_TILE_MIN_CONTENT: int = 760
DETAIL_TILE_CONTENT_MAX: int = DETAIL_CONTENT_TARGET
DETAIL_TILE_MAX_UPSCALE: float = 1.8
DETAIL_TILE_SKIP_MIN: int = 0
DETAIL_RENDER_CAPTURE_SCALE: int = 2
DETAIL_RENDER_SEGMENT_MAX_DEVICE_PX: int = 12000
DETAIL_RENDER_FINAL_JPEG_QUALITY: int = 95
DETAIL_HERO_IMAGE_COUNT: int = 2
DETAIL_MERGE_COLUMNS: int = 2
DETAIL_MERGE_ROWS: int = 2
DETAIL_MERGE_CELL: int = DETAIL_RENDER_WIDTH // DETAIL_MERGE_COLUMNS
RETOUCH_SHEET_MAX_PX: int = 2048
RETOUCH_GRID_MAX_DEFAULT: int = 5
RETOUCH_GRID_MAX_LIMIT: int = 5
RETOUCH_GRID_MIN_CONTENT: int = 400
RETOUCH_GRID_PADDING: int = 12

# ---------------------------------------------------------------------------
# Regexes. Korean patterns are expressed as literal characters (no
# \u escapes).
# ---------------------------------------------------------------------------

OPTION_LABEL_TEXT_RE = re.compile(
    r"(?:\bSTY(?:LE|IE|1E)\b|\bTYPE\b|\bMODEL\b|\bCOLOR\b|"
    r"(?<![A-Za-z0-9])[A-Z]\s*\d{1,3}(?![A-Za-z0-9]))",
    re.IGNORECASE,
)

SELLER_SIZE_TEXT_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:cm|mm|m|in|inch)" r"\s*(?:[*xX\u00d7]\s*)?){2,3}",
    re.IGNORECASE,
)

DETAIL_GARBAGE_TEXT_RE = re.compile(
    r"watermark|logo|coupon|free\s*shipping|sale",
    re.IGNORECASE,
)

DETAIL_INFOGRAPHIC_TEXT_RE = re.compile(
    r"our\s*product\s*advantages|product\s*advantages|"
    r"A5\s*melamine|melamine\s*material|"
    r"utensils?",
    re.IGNORECASE,
)

STRONG_GARBAGE_TEXT_RE = re.compile(r"(?!x)x", re.IGNORECASE)

SELLER_NOTICE_HEADING_RE = re.compile(r"(?!x)x", re.IGNORECASE)

OPTION_CARD_TONES = {"brown", "orange", "pink", "green", "neutral"}
OPTION_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,12}\d[A-Za-z0-9_-]*|\d+[A-Za-z][A-Za-z0-9_-]*)(?![A-Za-z0-9])"
)

PROPERTY_FIELD_SPLIT_RE = re.compile(r"[:\uff1a]")

# ---------------------------------------------------------------------------
# Text-filter regexes — canonical home.
#
# These were previously defined in ``copywriting`` and imported
# lazily from here, creating a hidden circular dependency. The canonical
# definitions now live in this module (the upstream DAG node).
# Korean patterns are literal characters.
# ---------------------------------------------------------------------------

BANNED_CLAIM_RE = re.compile(
    r"100\s*%|AUTH\s*ENTIC|"
    r"정\s*품|진\s*품|"
    r"최고(?:급)?|최상급|"
    r"완벽(?:한|하게)?|"
    r"프리미엄",
    re.IGNORECASE,
)

EDITORIAL_NOISE_RE = re.compile(
    r"배송|출고|발송|택배|"
    r"판매처|판매자|스토어|"
    r"구매대행|주문\s*확인|"
    r"반품|교환|고객센터|"
    r"무료배송|특가|도매|"
    r"공장직영|쿠폰",
    re.IGNORECASE,
)

EMPTY_MARKETING_COPY_RE = re.compile(
    r"일상에\s*별별|당신만을\s*위한|"
    r"나만을\s*위한|별별한\s*하루|"
    r"삶의\s*격|생활의\s*격|"
    r"공간을\s*완성|물드를\s*완성|"
    r"감성을\s*더하|각을\s*더하|"
    r"완벽한\s*선택|소중한\s*사람을\s*위한",
    re.IGNORECASE,
)

SENSORY_COPY_NOISE_RE = EMPTY_MARKETING_COPY_RE

# ---------------------------------------------------------------------------
# Category-path -> notice-type heuristic table (canonical, single source).
#
# Both ``qa_agents._infer_notice_type`` (prepare step) and
# ``naver_client._resolve_notice_type`` (register step) must infer the same
# notice type from the same category path. Previously this table existed as
# two literal copies with a comment admitting the duplication; the copies
# inevitably diverged. It now lives once here, and both modules import this
# symbol. This module is the upstream DAG node (no clossify imports), so it
# is the safe shared home for both consumers.
#
# The single source of truth for notice *types/fields* remains
# ``data/notice_types.json``; this tuple is only the path-keyword heuristic
# that picks a candidate type before the data file is consulted.
# ---------------------------------------------------------------------------
CATEGORY_PATH_NOTICE_HINTS = (
    ("가구", "FURNITURE"),
    ("의류", "WEAR"),
    ("신발", "SHOES"),
    ("구두", "SHOES"),
    ("가방", "BAG"),
    ("침구", "SLEEPING_GEAR"),
    ("커튼", "SLEEPING_GEAR"),
    ("가전", "HOME_APPLIANCES"),
    ("영상가전", "IMAGE_APPLIANCES"),
    ("계절가전", "SEASON_APPLIANCES"),
    ("사무용기기", "OFFICE_APPLIANCES"),
    ("휴대폰", "CELLPHONE"),
    ("광학기기", "OPTICS_APPLIANCES"),
    ("귀금속", "JEWELLERY"),
    ("보석", "JEWELLERY"),
    ("시계", "JEWELLERY"),
    ("서적", "BOOKS"),
    ("어린이", "KIDS"),
    ("생활화학", "BIOCHEMISTRY"),
    ("살생물", "BIOCIDAL"),
    ("패션잡화", "FASHION_ITEMS"),
    ("주방", "KITCHEN_UTENSILS"),
    ("식기", "KITCHEN_UTENSILS"),
    ("화장품", "COSMETIC"),
    ("식품", "FOOD"),
    ("스포츠", "SPORTS_EQUIPMENT"),
    ("악기", "MUSICAL_INSTRUMENT"),
    ("자동차", "CAR_ARTICLES"),
    ("의료기기", "MEDICAL_APPLIANCES"),
    ("네비게이션", "NAVIGATION"),
)

# SEO-title specific banned patterns. These are a
# superset of the marketing-claim regex aimed at title copy.
SEO_TITLE_BANNED_RE = re.compile(
    r"정\s*품|최\s*고|1\s*위|공\s*식|100\s*%|정\s*식|명\s*품|고\s*급|"
    r"주문\s*폭주|즉시\s*할인|재입고|한정|첫구매|공짜|품절|MD\s*추천|"
    r"선착순|임박|인기|가성비|저렴|추천|신상품|이벤트|무료\s*배송",
    re.IGNORECASE,
)

SEO_STOPWORDS = {
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
    "로",
    "으로",
    "에",
    "에서",
    "및",
    "또는",
    "그리고",
    "상품",
    "제품",
}


# ---------------------------------------------------------------------------
# Description HTML -> text helpers.
# ---------------------------------------------------------------------------


class _DescTextExtractor(HTMLParser):
    """Extract visible text from upstream description HTML.

    Drops ``script``/``style``/``noscript``/``svg`` subtrees and emits a
    newline at each block-level boundary so callers can re-flow lines.
    """

    BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "td",
            "th",
            "section",
            "article",
            "header",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ul",
            "ol",
            "table",
            "tbody",
            "thead",
            "figcaption",
            "figure",
            "blockquote",
        }
    )
    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if not self._skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data:
            self.parts.append(data)


def _normalize_desc_text(text, limit=6000):
    """Collapse whitespace and trim a raw description string.

    Drops zero-width/nbsp characters and bare URL lines.
    """
    text = html.unescape(str(text or ""))
    text = re.sub(r"[\u00a0\u200b\ufeff]+", " ", text)
    lines = []
    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or re.fullmatch(r"https?://\S+", line, flags=re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines)[:limit].strip()


def desc_html_to_text(desc_html):
    """Return visible text from upstream desc HTML.

    Image-only desc returns empty string.
    """
    raw = str(desc_html or "").strip()
    if not raw:
        return ""
    raw = html.unescape(raw)
    parser = _DescTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<(script|style|noscript|svg)\b[\s\S]*?</\1>", " ", raw, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
    return _normalize_desc_text(text)


def _hesc(value, default=""):
    """HTML-escape ``value`` for safe inline interpolation."""
    text = str(value if value not in (None, "") else default)
    return html.escape(text, quote=True)


# ---------------------------------------------------------------------------
# Property flatten / summarise.
# ---------------------------------------------------------------------------


def _first_text(*values, default=""):
    """Return the first non-empty stringified value, else ``default``."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def _compact_spaces(text):
    """Collapse runs of whitespace into single spaces and trim."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _strip_banned_claims(text):
    """Strip banned Korean marketing claims and collapse whitespace.

    ``BANNED_CLAIM_RE`` lives in this module, so this helper performs
    the real removal rather than being an identity.
    """
    text = BANNED_CLAIM_RE.sub(" ", str(text or ""))
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _detail_safe_text(text, default=""):
    """Sanitise free-form text for detail rendering.

    Strips banned Korean marketing claims (e.g. ``"100%"``, ``"정품"`` /
    ``"진품"`` = "genuine/authentic", ``"최고급"`` = "top-grade",
    ``"프리미엄"`` = "premium") then collapses whitespace.
    """
    text = _strip_banned_claims(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or default


def _sanitize_seo_title(text, *, max_len=100):
    """Sanitise a candidate SEO title.

    Restored to the original pipeline:
      1. strip banned marketing claims (``BANNED_CLAIM_RE``)
      2. strip SEO-title-specific banned patterns (``SEO_TITLE_BANNED_RE``)
      3. drop non-Korean/non-ASCII-alnum/non-space characters
      4. drop SEO stopwords and duplicate words (case-insensitive)
      5. truncate to ``max_len`` on a word boundary
    """
    text = _strip_banned_claims(text)
    text = SEO_TITLE_BANNED_RE.sub(" ", text)
    text = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", text)
    words, seen = [], set()
    for word in _compact_spaces(text).split():
        key = word.lower()
        if key in SEO_STOPWORDS or key in seen:
            continue
        seen.add(key)
        words.append(word)
    title = " ".join(words)
    if len(title) > max_len:
        cut = title[:max_len].rstrip()
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        title = cut or title[:max_len]
    return title.strip()


def _flatten_prop_terms(value, *, limit=30, clean=True):
    """Flatten nested prop structures into a flat list of short phrases.

    Walks dicts/lists/tuples; for each leaf emits ``"<label> <value>"``
    when a label/value pair is detectable, otherwise the raw string.
    """
    terms: list[str] = []

    def add(text):
        text = _compact_spaces(text)
        fields = [x.strip() for x in PROPERTY_FIELD_SPLIT_RE.split(text) if x.strip()]
        if len(fields) >= 4:
            text = f"{fields[-2]} {fields[-1]}"
        elif len(fields) == 2:
            text = f"{fields[0]} {fields[1]}"
        if clean:
            text = _detail_safe_text(text)
        if text and text not in terms:
            terms.append(text)

    def walk(v):
        if len(terms) >= limit:
            return
        if v in (None, ""):
            return
        if isinstance(v, dict):
            label = _first_text(
                v.get("name"),
                v.get("label"),
                v.get("key"),
                v.get("title"),
                v.get("prop_name"),
                v.get("attr_name"),
                default="",
            )
            val = _first_text(
                v.get("value"),
                v.get("values"),
                v.get("text"),
                v.get("desc"),
                v.get("prop_value"),
                v.get("attr_value"),
                default="",
            )
            if label and val:
                add(f"{label} {val}")
                return
            if val:
                add(val)
                return
            for nested in v.values():
                walk(nested)
            return
        if isinstance(v, list | tuple | set):
            for item in v:
                walk(item)
            return
        for part in re.split(r"[;\n\r,|/]+", str(v)):
            add(part)

    walk(value)
    return terms[:limit]


def _props_summary(props, *, max_terms=10):
    """Return a single space-joined summary of the flattened prop terms."""
    return " ".join(_flatten_prop_terms(props, limit=max_terms))


def _fallback_seo_title(title_ko, props, category_path):
    """Build a deterministic fallback SEO title.

    Concatenates the Korean title, the leaf category, and a prop summary,
    then sanitises to ``max_len=100``.
    """
    leaf = str(category_path or "").split(">")[-1].strip()
    pieces = [title_ko, leaf, _props_summary(props, max_terms=12)]
    return _sanitize_seo_title(" ".join(p for p in pieces if p), max_len=100) or "item-detail"


__all__ = [
    "BANNED_CLAIM_RE",
    "CATEGORY_PATH_NOTICE_HINTS",
    "DESC_IMAGE_SCAN_LIMIT",
    "DETAIL_ASPECT_TALL",
    "DETAIL_CONTENT_TARGET",
    "DETAIL_GARBAGE_TEXT_RE",
    "DETAIL_HERO_IMAGE_COUNT",
    "DETAIL_IMAGES_MAX",
    "DETAIL_IMAGES_MIN",
    "DETAIL_INFOGRAPHIC_TEXT_RE",
    "DETAIL_MERGE_CELL",
    "DETAIL_MERGE_COLUMNS",
    "DETAIL_MERGE_ROWS",
    "DETAIL_RENDER_CAPTURE_SCALE",
    "DETAIL_RENDER_FINAL_JPEG_QUALITY",
    "DETAIL_RENDER_SEGMENT_MAX_DEVICE_PX",
    "DETAIL_RENDER_WIDTH",
    "DETAIL_TILE_CONTENT_MAX",
    "DETAIL_TILE_MAX_UPSCALE",
    "DETAIL_TILE_MIN_CONTENT",
    "DETAIL_TILE_SKIP_MIN",
    "EDITORIAL_NOISE_RE",
    "EMPTY_MARKETING_COPY_RE",
    "LISTING_IMAGE_LIMIT",
    "MAIN_IMAGE_LIMIT",
    "OPTION_CARD_TONES",
    "OPTION_CODE_RE",
    "OPTION_GRID_LIMIT",
    "OPTION_LABEL_TEXT_RE",
    "PROPERTY_FIELD_SPLIT_RE",
    "RETOUCH_GRID_MAX_DEFAULT",
    "RETOUCH_GRID_MAX_LIMIT",
    "RETOUCH_GRID_MIN_CONTENT",
    "RETOUCH_GRID_PADDING",
    "RETOUCH_SHEET_MAX_PX",
    "SELLER_NOTICE_HEADING_RE",
    "SELLER_SIZE_TEXT_RE",
    "SENSORY_COPY_NOISE_RE",
    "SEO_STOPWORDS",
    "SEO_TITLE_BANNED_RE",
    "STRONG_GARBAGE_TEXT_RE",
    "_DescTextExtractor",
    "_compact_spaces",
    "_detail_safe_text",
    "_fallback_seo_title",
    "_first_text",
    "_flatten_prop_terms",
    "_hesc",
    "_normalize_desc_text",
    "_props_summary",
    "_sanitize_seo_title",
    "_strip_banned_claims",
    "desc_html_to_text",
]
