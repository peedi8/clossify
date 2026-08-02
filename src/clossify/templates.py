# -*- coding: utf-8 -*-
"""Detail HTML templates and brand assets.

Ported from sourcing.py (T-201a part 1/2). Depends on :mod:`text_props`
(for ``DETAIL_RENDER_WIDTH`` and ``_hesc``).
"""
from __future__ import annotations

from pathlib import Path

from . import common
from .text_props import DETAIL_RENDER_WIDTH, _hesc

# ---------------------------------------------------------------------------
# Detail rendering limits and asset paths (source L3403-L3458).
#
# The BRAND_* block references brand asset paths. The brand-name token
# itself is forbidden (Hard Constraint 2), so the path-string constants
# are stubbed; the numeric/structural constants are ported verbatim.
# Public accessor names are vendor-neutral (BRAND_*) to keep forbidden
# token count at 0.
# ---------------------------------------------------------------------------

DETAIL_UPLOAD_SEGMENT_MAX_HEIGHT: int = 3000
WHITE_MARGIN_TRIM_THRESHOLD: float = 0.12
WHITE_MARGIN_PIXEL_THRESHOLD: int = 245
WHITE_MARGIN_ROW_RATIO: float = 0.992
WHITE_MARGIN_PAD_RATIO: float = 0.015
WHITE_MARGIN_MAX_PAD: int = 24

# Lazy cache for the Pretendard @font-face CSS block (populated on first
# call to PRETENDARD_FONT_FACE_CSS()). ``None`` = not yet built.
_PRETENDARD_FONT_FACE_CSS_CACHE: str | None = None


def _brand_cfg() -> dict:
    """Return ``cfg()["brand"]`` as a dict, or ``{}`` on any failure."""
    try:
        section = common.cfg().get("brand")
    except Exception:
        return {}
    return section if isinstance(section, dict) else {}


def _brand_asset_path(key: str) -> str:
    """Resolve a brand asset path from ``cfg()["brand"][key]``.

    Raises ``ValueError`` when the key is absent or empty.
    """
    raw = str(_brand_cfg().get(key) or "").strip()
    if not raw:
        raise ValueError(
            f"brand.{key} is not configured (T-201a: brand asset path)"
        )
    return raw


def _font_asset_path(config_key: str, fallback_filename: str) -> str:
    """Resolve a font asset path.

    Priority:
      1. ``cfg()["fonts"][config_key]`` (absolute path).
      2. ``<root>/assets/fonts/<fallback_filename>`` (bundled fallback).
      3. The bare fallback filename (caller handles missing-file).
    """
    try:
        fonts = common.cfg().get("fonts")
    except Exception:
        fonts = None
    if isinstance(fonts, dict):
        raw = str(fonts.get(config_key) or "").strip()
        if raw:
            return raw
    return str(common.ROOT_DIR / "assets" / "fonts" / fallback_filename)


def BRAND_RENDER_WIDTH() -> int:
    """Brand-bound render width (alias for DETAIL_RENDER_WIDTH).

    Source L3403. Returns the shared render width so callers that
    referenced the brand alias keep working.
    """
    return DETAIL_RENDER_WIDTH


def BRAND_DETAIL_HEADER_PATH() -> Path:
    """Brand detail header asset path (source L3404).

    Resolved from ``cfg()["brand"]["detail_header_path"]``. Raises
    ``ValueError`` when unset (the brand name is a forbidden token).
    """
    raw = _brand_asset_path("detail_header_path")
    return Path(raw)


def BRAND_DETAIL_FOOTER_PATH() -> Path:
    """Brand detail footer asset path (source L3405). Forbidden token."""
    raw = _brand_asset_path("detail_footer_path")
    return Path(raw)


def BRAND_DETAIL_HEADER_URI() -> str:
    """Brand detail header ``file://`` URI (source L3406).

    Derived from :func:`BRAND_DETAIL_HEADER_PATH`.
    """
    return BRAND_DETAIL_HEADER_PATH().resolve().as_uri()


def BRAND_DETAIL_FOOTER_URI() -> str:
    """Brand detail footer ``file://`` URI (source L3407).

    Derived from :func:`BRAND_DETAIL_FOOTER_PATH`.
    """
    return BRAND_DETAIL_FOOTER_PATH().resolve().as_uri()


def PRETENDARD_MEDIUM_FONT_PATH() -> Path:
    """Pretendard Medium font asset path (source L3408).

    Resolved from ``cfg()["fonts"]["pretendard_medium"]``; falls back to
    a well-known bundled location under ``<root>/assets/fonts`` when the
    config key is absent.
    """
    raw = _font_asset_path("pretendard_medium", "Pretendard-Medium.otf")
    return Path(raw)


def PRETENDARD_BLACK_FONT_PATH() -> Path:
    """Pretendard Black font asset path (source L3409).

    Resolved from ``cfg()["fonts"]["pretendard_black"]``; falls back to
    a well-known bundled location under ``<root>/assets/fonts``.
    """
    raw = _font_asset_path("pretendard_black", "Pretendard-Black.otf")
    return Path(raw)


def _pretendard_font_face_css() -> str:
    """Build ``@font-face`` CSS for Pretendard (source L3441-L3455).

    Returns an empty string when neither font path resolves to an
    existing file. The generated CSS uses ``file://`` URLs so it renders
    correctly in the headless render pipeline.
    """
    import html as _html

    blocks = []
    for weight, getter in (
        ("500", PRETENDARD_MEDIUM_FONT_PATH),
        ("900", PRETENDARD_BLACK_FONT_PATH),
    ):
        try:
            path = getter()
        except Exception:
            continue
        if not path or not Path(path).is_file():
            continue
        uri = Path(path).resolve().as_uri()
        blocks.append(
            "@font-face{{"
            "font-family:'Pretendard';"
            f"font-weight:{weight};"
            f"src:url('{_html.escape(uri)}') format('opentype');"
            "font-display:swap"
            "}"
        )
    return "\n".join(blocks)


def PRETENDARD_FONT_FACE_CSS() -> str:
    """Module-level ``@font-face`` CSS cache (source L3458).

    Returns the cached CSS produced by :func:`_pretendard_font_face_css`.
    The cache is built lazily on first call and never invalidated
    (font assets are immutable within a process lifetime).
    """
    global _PRETENDARD_FONT_FACE_CSS_CACHE
    if _PRETENDARD_FONT_FACE_CSS_CACHE is None:
        _PRETENDARD_FONT_FACE_CSS_CACHE = _pretendard_font_face_css()
    return _PRETENDARD_FONT_FACE_CSS_CACHE


def BRAND_HEADER_HTML() -> str:
    """Brand header HTML fragment (source L3459).

    Resolved from ``cfg()["brand"]["header_html"]`` (a string) or, when
    that is a path, the file contents. Raises ``ValueError`` when no
    source is configured.
    """
    section = _brand_cfg()
    raw = section.get("header_html")
    if isinstance(raw, str) and raw.strip():
        if raw.lstrip().startswith("<"):
            return raw
        path = Path(raw)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise ValueError(
        "brand.header_html is not configured (T-201a: BRAND_HEADER_HTML L3459)"
    )


# ---------------------------------------------------------------------------
# Static CSS fragments (source L3423-L3440). Pure ASCII, no forbidden tokens.
# ---------------------------------------------------------------------------

DETAIL_SINGLE_COLUMN_LOCK_CSS = (
    ".photo-stack{display:block!important;width:100%;max-width:100%;background:#fff;column-count:1}\n"
    ".photo-stack>.photo-block,.photo-stack>.hero,.photo-stack>.detail-band"
    "{display:block!important;width:100%;max-width:100%;margin:0;"
    "break-inside:avoid;page-break-inside:avoid;clear:both}\n"
    ".photo-stack img{display:block!important;width:100%!important;height:auto;margin:0 auto;border:0;float:none}"
)

OPTION_GRID_SECTION_CSS = (
    "<style>"
    ".options-grid{display:grid;"
    "grid-template-columns:repeat(auto-fit,minmax(var(--option-min,250px),1fr));"
    "gap:26px;align-items:start}"
    ".options-grid.five{grid-template-columns:repeat(auto-fit,minmax(var(--option-min,230px),1fr))}"
    ".options-grid.dense{gap:16px}"
    ".options-grid.dense .option-thumb{min-height:0}"
    ".options-grid.dense .option-card-info{min-height:230px;padding:68px 14px 22px}"
    ".options-grid.dense .option-badge{left:12px;top:12px;min-width:44px;height:44px;"
    "border-radius:22px;font-size:24px;line-height:44px}"
    ".options-grid.dense .option-label{min-height:72px;padding:18px 12px;font-size:26px;line-height:1.3}"
    ".options-grid.dense .option-card-info .option-label{min-height:0;padding:0;"
    "font-size:26px;line-height:1.25}"
    ".options-grid.dense .option-desc{min-height:54px;margin-top:12px;font-size:24px;line-height:1.35}"
    "</style>"
)


def build_korean_detail_html(d, naver_image_urls):
    """Build a Naver-compliant inline detail HTML from a product dict.

    Source L3373. The original hard-coded Korean copy and a fixed brand
    wrapper; both contain CJK which violates Hard Constraint 2 (CJK count
    must stay 0 in source). This implementation therefore returns an
    ``llm_hint`` descriptor asking the MCP host LLM to generate the
    Korean detail HTML body from the product data, using the packaged
    ``COPY_GUIDE.md`` and ``DESIGN_SYSTEM.md`` agent rules as the
    instruction context.

    The host LLM receives the product dict (string fields HTML-escaped),
    the uploaded Naver image URLs, the static CSS fragments this module
    already provides, and the brand wrapper placeholder locations. It
    returns a complete ``<html>...</html>`` document.

    Args:
        d: product dict (``name``, ``options``, ``props``, etc.).
        naver_image_urls: list of uploaded CDN image URLs (first = hero).

    Returns:
        ``llm_hint`` dict for the host LLM.
    """
    from .common import _llm_hint

    # Sanitise product string fields before handing to the host:
    # HTML-escape so the LLM never receives raw upstream HTML entities.
    safe_product = {}
    if isinstance(d, dict):
        for key, value in d.items():
            if isinstance(value, str):
                safe_product[key] = _hesc(value)
            else:
                safe_product[key] = value
    images = [str(u) for u in (naver_image_urls or []) if u]
    instruction = (
        "Build a complete Naver SmartStore detail-page HTML document from "
        "the supplied product dict and image URLs. Follow the COPY_GUIDE "
        "and DESIGN_SYSTEM agent rules: white canvas, single column, "
        "Pretendard font, soft rounds, photo-centric layout, one warm "
        "accent. Hard constraints: zero Chinese characters in rendered "
        "output, mobile-scale font sizes (body 35-40px), split images "
        ">3000px tall, max 5000px. Return a single <html>...</html> "
        "document with inline CSS; do NOT externalise resources."
    )
    return _llm_hint(
        "build_detail_html",
        input={
            "product": safe_product,
            "images": images,
            "render_width": DETAIL_RENDER_WIDTH,
            "css": {
                "single_column_lock": DETAIL_SINGLE_COLUMN_LOCK_CSS,
                "option_grid_section": OPTION_GRID_SECTION_CSS,
            },
        },
        instruction=instruction,
    )


__all__ = [
    "DETAIL_UPLOAD_SEGMENT_MAX_HEIGHT",
    "WHITE_MARGIN_TRIM_THRESHOLD", "WHITE_MARGIN_PIXEL_THRESHOLD",
    "WHITE_MARGIN_ROW_RATIO", "WHITE_MARGIN_PAD_RATIO", "WHITE_MARGIN_MAX_PAD",
    "DETAIL_SINGLE_COLUMN_LOCK_CSS", "OPTION_GRID_SECTION_CSS",
    "BRAND_RENDER_WIDTH", "BRAND_DETAIL_HEADER_PATH",
    "BRAND_DETAIL_FOOTER_PATH", "BRAND_DETAIL_HEADER_URI",
    "BRAND_DETAIL_FOOTER_URI", "PRETENDARD_MEDIUM_FONT_PATH",
    "PRETENDARD_BLACK_FONT_PATH", "_pretendard_font_face_css",
    "PRETENDARD_FONT_FACE_CSS", "BRAND_HEADER_HTML",
    "build_korean_detail_html",
]


# Suppress unused-import lints for re-exports.
_ = common, _hesc
