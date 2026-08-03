"""Search-volume and keyword helpers.

Ported from sourcing.py (T-201a part 1/2). Depends on :mod:`common` and
:mod:`text_props` (for prop sanitisation).

T-201a-r6: the external-market residue (num_iid extraction, the upstream
item fetcher, the Chinese-to-Korean title translator, the mobile and
short-link HTTP user-agent constants, the natural-Korean translation
rules table) has been removed entirely — this product only ingests
Korean user-supplied text and never collects from an external market.
Only the Naver SearchAds keyword-volume parsing helpers remain.
"""

from __future__ import annotations

import re

from . import common
from .text_props import _strip_banned_claims

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

    message = f"{timestamp}.{method}.{uri}".encode()
    digest = hmac.new(str(secret_key).encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Keyword parsing helpers (source L627-L718). Pure-Python.
# ---------------------------------------------------------------------------


def _clean_search_keyword(text, *, max_len=40):
    """Normalise free-form text into a search keyword.

    Strips banned claims, removes punctuation, collapses whitespace,
    truncates to ``max_len`` on a word boundary.
    """
    text = _strip_banned_claims(str(text or ""))
    text = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] or text[:max_len]
    return text.strip()


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
            row.get("relKeyword") or row.get("keyword") or row.get("hintKeyword") or ""
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


__all__ = [
    "_clean_search_keyword",
    "_parse_keywordstool_response",
    "_parse_search_volume",
    "_searchad_credentials",
    "_searchad_signature",
]


# Suppress unused-import lints for re-exports / helpers used implicitly.
_ = (common,)
