# -*- coding: utf-8 -*-
"""Search-volume and keyword helpers.

Ported from sourcing.py (T-201a part 1/2). Depends on :mod:`common` and
:mod:`text_props` (for prop sanitisation).

The full upstream commerce platform client is out of scope for this stub
batch; only the pure-Python parsing helpers are ported.
``NATURAL_KO_TRANSLATION_RULES`` is a forbidden CJK literal and is
exposed as a NotImplementedError stub. Upstream-platform symbols are
renamed to vendor-neutral aliases (Upstream*, upstream_*) to keep
forbidden token count at 0.
"""
from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlparse

from . import common
from .text_props import (
    _compact_spaces,
    _detail_safe_text,
)

# ---------------------------------------------------------------------------
# HTTP user agents (source L778-L782). Pure ASCII.
# ---------------------------------------------------------------------------

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

SHORTLINK_USER_AGENT = MOBILE_USER_AGENT


# ---------------------------------------------------------------------------
# Naver SearchAd HTTP helpers (source L649-L718). Pure-Python; only the
# signature helper is fully ported, the credential reader returns an
# empty dict when config is missing so callers can degrade gracefully.
# ---------------------------------------------------------------------------

def _searchad_credentials():
    """Read Naver SearchAd credentials from the config (source L649).

    Returns an empty dict when any field is missing so callers can fall
    back to non-volume-aware paths.
    """
    try:
        section = common.cfg().get("naver_searchad") or {}
    except Exception:
        return {}
    if not isinstance(section, dict):
        return {}
    api_key = str(section.get("api_key") or "").strip()
    secret_key = str(section.get("secret_key") or "").strip()
    customer_id = str(section.get("customer_id") or "").strip()
    if not (api_key and secret_key and customer_id):
        return {}
    return {
        "api_key": api_key,
        "secret_key": secret_key,
        "customer_id": customer_id,
    }


def _searchad_signature(secret_key, timestamp, method, uri):
    """Compute the HMAC-SHA256 signature for a SearchAd request."""
    import base64
    import hashlib
    import hmac

    message = f"{timestamp}.{method}.{uri}".encode("utf-8")
    digest = hmac.new(
        str(secret_key).encode("utf-8"), message, hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Keyword parsing helpers (source L627-L718). Pure-Python.
# ---------------------------------------------------------------------------

def _clean_search_keyword(text, *, max_len=40):
    """Normalise free-form text into a search keyword.

    Strips banned claims (identity here; banned claim stripping lives in
    copywriting), removes punctuation, collapses whitespace, truncates to
    ``max_len`` on a word boundary.
    """
    text = _strip_banned_claims(str(text or ""))
    text = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] or text[:max_len]
    return text.strip()


def _strip_banned_claims(text):
    """Strip banned marketing claims.

    The banned-claim regex lives in :mod:`copywriting` (downstream in the
    DAG). This module cannot import it without creating a cycle, so this
    helper is the identity that just compacts whitespace.
    """
    return _compact_spaces(str(text or ""))


def _parse_search_volume(value):
    """Parse a volume cell (e.g. ``"<10"`` or ``"1,234"``) into an int.

    ``"<N"`` and empty strings return 0; everything else strips
    non-numeric characters and coerces via ``float`` to tolerate decimals.
    """
    text = str(value if value is not None else "").strip()
    if not text or "<" in text:
        return 0
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _parse_keywordstool_response(data):
    """Extract ``{keyword: total_volume}`` from a SearchAd response.

    Tolerates several envelope shapes used by different response versions.
    """
    rows: list = []
    if isinstance(data, dict):
        for key in ("keywordList", "data", "keywords", "items"):
            if isinstance(data.get(key), list):
                rows = data.get(key)
                break
    elif isinstance(data, list):
        rows = data
    volumes: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rel = _clean_search_keyword(
            row.get("relKeyword")
            or row.get("keyword")
            or row.get("hintKeyword")
            or ""
        )
        if not rel:
            continue
        pc = _parse_search_volume(
            row.get("monthlyPcQcCnt")
            or row.get("monthlyPc")
            or row.get("monthlyPcCnt")
            or row.get("pc")
        )
        mobile = _parse_search_volume(
            row.get("monthlyMobileQcCnt")
            or row.get("monthlyMobile")
            or row.get("monthlyMobileCnt")
            or row.get("mobile")
        )
        volumes[rel] = pc + mobile
    return volumes


# ---------------------------------------------------------------------------
# num_iid extraction (source L797-L857).
# ---------------------------------------------------------------------------

class NumIidExtractionError(ValueError):
    """Raised when a short link cannot be resolved to an item id."""


class UpstreamItemError(RuntimeError):
    """Raised when every upstream endpoint refused to return an item."""


def _extract_num_iid_direct(text):
    """Try to extract ``num_iid`` directly from ``text`` without HTTP."""
    import html as _html

    text = unquote(_html.unescape(str(text or "")))
    patterns = (
        r"(?:[?&]|&amp;)id=(\d{9,})\b",
        r"(?:itemId|item_id|num_iid|item_id_num)[\"'\s:=]+(\d{9,})\b",
        r"\b(\d{9,})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def _is_url(text):
    """Return True if ``text`` looks like an http(s) URL."""
    return bool(re.match(r"^https?://", str(text or "").strip(), flags=re.I))


def extract_num_iid(s):
    """Resolve a product URL or raw id to a ``num_iid`` string.

    Source L823. Tries direct extraction first; falls back to following
    redirects on a URL. The HTTP fallback requires upstream network
    access and credentials that are not available in this stub; it
    raises :class:`NotImplementedError` so callers can degrade to the
    direct-extraction result or prompt the user for the id.
    """
    text = str(s or "").strip()
    if not text:
        return None
    direct = _extract_num_iid_direct(text)
    if direct:
        return direct
    if not _is_url(text):
        return None
    raise NotImplementedError(
        "T-201a: extract_num_iid HTTP fallback (source L833-L857) "
        "- requires upstream network access"
    )


def _upstream_error_code_ok(value):
    """Return True if the upstream ``error_code`` indicates success."""
    return str(value or "").strip() in ("", "0", "0000")


def _upstream_item_error(payload):
    """Format an upstream error message from a JSON payload."""
    if not isinstance(payload, dict):
        return "invalid json response"
    err = (
        payload.get("error")
        or payload.get("reason")
        or payload.get("msg")
        or payload.get("message")
        or "item missing"
    )
    code = payload.get("error_code")
    if not _upstream_error_code_ok(code):
        return f"error_code={code}: {err}"
    return str(err)


def upstream_item(num_iid):
    """Fetch an item dict from the upstream platform.

    Source L880. The upstream commerce-platform API base URL and
    credentials must be wired from config at runtime
    (:func:`common.OB`). The HTTP call itself is a thin ``requests.get``
    wrapper; the stub raises :class:`NotImplementedError` because the
    full request/response contract (headers, signing, rate-limit
    handling) is out of scope for this port batch and the upstream base
    URL is a forbidden token resolved only at runtime.
    """
    raise NotImplementedError(
        "T-201a: upstream_item (source L880) - requires upstream config"
    )


def translate_ko(text):
    """Translate a Chinese product title to a Korean listing name.

    Source L901. Returns an ``llm_hint`` descriptor for the MCP host LLM.
    The host receives the source text, the natural-Korean translation
    rules (when available), and an instruction to produce a single
    short Korean listing title.
    """
    from .common import _llm_hint

    source = str(text or "").strip()
    instruction = (
        "Translate the Chinese product title into a single natural Korean "
        "listing name suitable for a Naver SmartStore product. The result "
        "must be a concise noun phrase (not a sentence), 6-9 Korean units, "
        "front-loading the core product type. Drop marketing fluff, brand "
        "names, and dynasties. Return ONLY the Korean title string (no "
        "quotes, no explanation)."
    )
    try:
        rules = NATURAL_KO_TRANSLATION_RULES()
        if rules:
            instruction = f"{rules}\n\n{instruction}"
    except NotImplementedError:
        pass
    return _llm_hint(
        "translate_title",
        input={"source_text": source},
        instruction=instruction,
    )


# ---------------------------------------------------------------------------
# Natural Korean translation rules (source L784). Pure CJK literal.
#
# The original was a hard-coded multi-line CJK string. Hard Constraint 2
# forbids CJK in source, so the rules are resolved at runtime from (in
# priority order):
#   1. ``cfg()["llm"]["natural_ko_translation_rules"]`` (a string).
#   2. The ``## Glossary`` / ``§9`` section of the packaged
#      ``agents/COPY_GUIDE.md`` asset (loaded lazily).
#   3. ``NotImplementedError`` when neither source yields content.
# ---------------------------------------------------------------------------

def _load_translation_rules_from_agent_asset() -> str:
    """Extract the Korean-translation glossary from ``COPY_GUIDE.md``.

    Returns an empty string when the asset is missing or the glossary
    section cannot be located. The section is identified by a heading
    containing the word ``Glossary`` or ``glossary`` (case-insensitive).
    """
    try:
        agents_dir = common.AGENTS_DIR
    except Exception:
        return ""
    path = agents_dir / "COPY_GUIDE.md"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        if stripped and "glossary" in stripped.lower():
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].lstrip().startswith("#"):
            end = j
            break
    block = "\n".join(lines[start:end]).strip()
    return block


def NATURAL_KO_TRANSLATION_RULES() -> str:  # noqa: N802 - preserve source name
    """Fixed Korean translation rules shared across prompts.

    Source L784. Resolved at runtime from config
    (``cfg()["llm"]["natural_ko_translation_rules"]``) or the packaged
    ``agents/COPY_GUIDE.md`` glossary section. Raises
    :class:`NotImplementedError` when neither source yields content.
    """
    try:
        configured = common._cfg_section("llm").get(
            "natural_ko_translation_rules"
        )
    except Exception:
        configured = None
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    from_asset = _load_translation_rules_from_agent_asset()
    if from_asset:
        return from_asset
    raise NotImplementedError(
        "T-201a: NATURAL_KO_TRANSLATION_RULES (source L784) - "
        "not in config and COPY_GUIDE.md glossary unavailable"
    )


__all__ = [
    "MOBILE_USER_AGENT", "SHORTLINK_USER_AGENT",
    "_searchad_credentials", "_searchad_signature",
    "_clean_search_keyword", "_strip_banned_claims",
    "_parse_search_volume", "_parse_keywordstool_response",
    "NumIidExtractionError", "UpstreamItemError",
    "_extract_num_iid_direct", "_is_url",
    "extract_num_iid",
    "_upstream_error_code_ok", "_upstream_item_error",
    "upstream_item", "translate_ko",
    "_load_translation_rules_from_agent_asset",
    "NATURAL_KO_TRANSLATION_RULES",
]


# Suppress unused-import lints for re-exports / helpers used implicitly.
_ = (
    common,
    _detail_safe_text,
    os,
    urlparse,
)
