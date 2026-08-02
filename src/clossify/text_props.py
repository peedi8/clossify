# -*- coding: utf-8 -*-
"""Text and property extraction helpers.

Ported from sourcing.py (T-201a part 1/2). Depends on :mod:`common`.

T-201a-r5: all Chinese (Hanja) detection and stripping code has been
removed entirely. This product only ingests Korean user-supplied text,
so there is no input path that could carry Chinese ideographs. The
Korean marketing-claim filters are preserved and use literal Korean
characters.
"""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

from .common import _safe_float  # noqa: F401 - re-exported for downstream modules
from . import common

# ---------------------------------------------------------------------------
# Image / detail rendering limits (source L2360-L2387). Pure literals.
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
    re.I,
)

SELLER_SIZE_TEXT_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:cm|mm|m|in|inch)"
    r"\s*(?:[*xX\u00d7]\s*)?){2,3}",
    re.I,
)

DETAIL_GARBAGE_TEXT_RE = re.compile(
    r"watermark|logo|coupon|free\s*shipping|sale",
    re.I,
)

DETAIL_INFOGRAPHIC_TEXT_RE = re.compile(
    r"our\s*product\s*advantages|product\s*advantages|"
    r"A5\s*melamine|melamine\s*material|"
    r"utensils?",
    re.I,
)

STRONG_GARBAGE_TEXT_RE = re.compile(r"(?!x)x", re.I)

SELLER_NOTICE_HEADING_RE = re.compile(r"(?!x)x", re.I)

OPTION_CARD_TONES = {"brown", "orange", "pink", "green", "neutral"}
OPTION_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,12}\d[A-Za-z0-9_-]*|\d+[A-Za-z][A-Za-z0-9_-]*)(?![A-Za-z0-9])"
)

PROPERTY_FIELD_SPLIT_RE = re.compile(r"[:\uff1a]")


# ---------------------------------------------------------------------------
# Description HTML -> text helpers (source L3258-L3370).
# ---------------------------------------------------------------------------

class _DescTextExtractor(HTMLParser):
    """Extract visible text from upstream description HTML.

    Drops ``script``/``style``/``noscript``/``svg`` subtrees and emits a
    newline at each block-level boundary so callers can re-flow lines.
    """

    BLOCK_TAGS = frozenset({
        "p", "div", "br", "li", "tr", "td", "th", "section", "article",
        "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
        "table", "tbody", "thead", "figcaption", "figure", "blockquote",
    })
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
        if not line or re.fullmatch(r"https?://\S+", line, flags=re.I):
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
        text = re.sub(r"<(script|style|noscript|svg)\b[\s\S]*?</\1>", " ", raw, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return _normalize_desc_text(text)


def _hesc(value, default=""):
    """HTML-escape ``value`` for safe inline interpolation."""
    text = str(value if value not in (None, "") else default)
    return html.escape(text, quote=True)


# ---------------------------------------------------------------------------
# Property flatten / summarise / translate (source L7074-L7172).
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


def _detail_safe_text(text, default=""):
    """Sanitise free-form text for detail rendering.

    Strips banned Korean marketing claims (e.g. ``"100%"``, ``"정품"`` /
    ``"진품"`` = "genuine/authentic", ``"최고급"`` = "top-grade",
    ``"프리미엄"`` = "premium") then collapses whitespace.
    ``BANNED_CLAIM_RE`` is defined in :mod:`copywriting` (the terminal
    DAG node); it is imported lazily on first call and cached in
    :data:`globals` so the import graph stays acyclic (copywriting ->
    text_props, never the reverse at load time).
    """
    banned = globals().get("BANNED_CLAIM_RE")
    if banned is None:
        from .copywriting import BANNED_CLAIM_RE as banned
        globals()["BANNED_CLAIM_RE"] = banned
    text = banned.sub(" ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text or default


def _sanitize_seo_title(text, *, max_len=100):
    """Sanitise a candidate SEO title.

    Source L3644. The full sanitisation pipeline depends on copywriting
    rules that are out of scope for this stub batch; the fallback path
    (trim + collapse whitespace) is enough for the property helpers below.
    """
    text = _compact_spaces(text)
    if not text:
        return ""
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] or text[:max_len]
    return text.strip()


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
                v.get("name"), v.get("label"), v.get("key"), v.get("title"),
                v.get("prop_name"), v.get("attr_name"), default=""
            )
            val = _first_text(
                v.get("value"), v.get("values"), v.get("text"), v.get("desc"),
                v.get("prop_value"), v.get("attr_value"), default=""
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
        if isinstance(v, (list, tuple, set)):
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


def _extract_upstream_props(item):
    """Return the first present prop-list field on ``item``.

    Looks up by a series of candidate keys used across upstream payloads.
    """
    for key in (
        "props", "item_props", "props_list", "props_name", "props_names",
        "attributes", "attributes_list", "params", "props_str",
    ):
        if isinstance(item, dict) and item.get(key):
            return item.get(key)
    return []


def translate_props_ko(props):
    """Return sanitised Korean listing keywords for ``props``.

    Source L7137. T-201a-r4: the Chinese-detection branch has been
    removed (no Chinese input path exists). The flattened terms are
    sanitised through :func:`_detail_safe_text` and returned directly;
    no host-LLM translation hint is produced.
    """
    terms = _flatten_prop_terms(props, clean=False)
    if not terms:
        return []
    return [_detail_safe_text(t) for t in terms if _detail_safe_text(t)]


def _fallback_seo_title(title_ko, props, category_path):
    """Build a deterministic fallback SEO title.

    Concatenates the Korean title, the leaf category, and a prop summary,
    then sanitises to ``max_len=100``.
    """
    leaf = str(category_path or "").split(">")[-1].strip()
    pieces = [title_ko, leaf, _props_summary(props, max_terms=12)]
    return _sanitize_seo_title(" ".join(p for p in pieces if p), max_len=100) or "item-detail"


def build_seo_title(title_ko, props, category_path):
    """Build an SEO product title via the naming agent.

    Source L7181. Delegates to :func:`copywriting.naming_agent`, which
    returns either a normalised result dict or an ``llm_hint`` descriptor
    (when the host LLM must run). On invalid output the deterministic
    :func:`_fallback_seo_title` is used.
    """
    from . import copywriting

    try:
        result = copywriting.naming_agent(title_ko, props, category_path)
    except ValueError:
        raise
    except Exception:
        return _fallback_seo_title(title_ko, props, category_path)
    if isinstance(result, dict) and "title" in result:
        title = _sanitize_seo_title(result.get("title"), max_len=100)
        if title:
            return title
    return _fallback_seo_title(title_ko, props, category_path)


# ---------------------------------------------------------------------------
# Lazy re-export of ``BANNED_CLAIM_RE`` from :mod:`copywriting`.
#
# ``copywriting`` imports ``text_props`` at module level (DAG edge), so
# ``text_props`` cannot import ``copywriting`` at module level without
# creating a cycle. PEP 562 module ``__getattr__`` lets us resolve
# ``text_props.BANNED_CLAIM_RE`` lazily on first access — by which point
# both modules are fully initialised.
# ---------------------------------------------------------------------------

def __getattr__(name):
    if name == "BANNED_CLAIM_RE":
        from .copywriting import BANNED_CLAIM_RE as _bcr
        globals()["BANNED_CLAIM_RE"] = _bcr
        return _bcr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Convenience: keep ``common`` accessible as ``text_props.common`` for
# downstream modules that used ``sourcing.common.<x>`` style access.
# ---------------------------------------------------------------------------

__all__ = [
    "MAIN_IMAGE_LIMIT", "LISTING_IMAGE_LIMIT", "DESC_IMAGE_SCAN_LIMIT",
    "OPTION_GRID_LIMIT", "DETAIL_RENDER_WIDTH", "DETAIL_CONTENT_TARGET",
    "DETAIL_ASPECT_TALL", "DETAIL_IMAGES_MIN", "DETAIL_IMAGES_MAX",
    "DETAIL_TILE_MIN_CONTENT", "DETAIL_TILE_CONTENT_MAX",
    "DETAIL_TILE_MAX_UPSCALE", "DETAIL_TILE_SKIP_MIN",
    "DETAIL_RENDER_CAPTURE_SCALE", "DETAIL_RENDER_SEGMENT_MAX_DEVICE_PX",
    "DETAIL_RENDER_FINAL_JPEG_QUALITY", "DETAIL_HERO_IMAGE_COUNT",
    "DETAIL_MERGE_COLUMNS", "DETAIL_MERGE_ROWS", "DETAIL_MERGE_CELL",
    "RETOUCH_SHEET_MAX_PX", "RETOUCH_GRID_MAX_DEFAULT",
    "RETOUCH_GRID_MAX_LIMIT", "RETOUCH_GRID_MIN_CONTENT",
    "RETOUCH_GRID_PADDING",
    "OPTION_LABEL_TEXT_RE", "SELLER_SIZE_TEXT_RE",
    "DETAIL_GARBAGE_TEXT_RE", "DETAIL_INFOGRAPHIC_TEXT_RE",
    "STRONG_GARBAGE_TEXT_RE", "SELLER_NOTICE_HEADING_RE",
    "OPTION_CARD_TONES", "OPTION_CODE_RE", "PROPERTY_FIELD_SPLIT_RE",
    "BANNED_CLAIM_RE",  # noqa: F822 - resolved lazily via module __getattr__
    "desc_html_to_text", "_normalize_desc_text", "_hesc",
    "_first_text", "_compact_spaces", "_detail_safe_text",
    "_sanitize_seo_title",
    "_flatten_prop_terms", "_props_summary",
    "_extract_upstream_props", "translate_props_ko",
    "_fallback_seo_title", "build_seo_title", "_DescTextExtractor",
]


# Suppress unused-import lint for the re-export.
_ = (common, json)
