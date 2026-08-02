# -*- coding: utf-8 -*-
"""Naming agent and SEO copy normalisation.

Ported from sourcing.py (T-201a part 1/2). Terminal node of the DAG:
depends on :mod:`common`, :mod:`text_props`, :mod:`keyword_volume`
and :mod:`seo`.

Per the spec the signature of :func:`_normalize_naming_result` uses
``source_title`` (renamed from the original ``title_cn``); a falsy
``source_title`` now raises :class:`ValueError` before normalisation.

T-201a-r4: Chinese detection / stripping code has been removed entirely.
This product only ever ingests Korean user input, so there is no path
for Chinese text to enter the pipeline. Korean marketing-claim filters
(``BANNED_CLAIM_RE`` etc.) are preserved and now use literal Korean.
"""
from __future__ import annotations

import re

from . import common, keyword_volume, seo
from .text_props import (
    _detail_safe_text,
    _fallback_seo_title,
    _flatten_prop_terms,
    _props_summary,
    _sanitize_seo_title,
)

# ---------------------------------------------------------------------------
# SEO title targets (source L3905-L3907).
# ---------------------------------------------------------------------------

SEO_TITLE_UNIT_MIN: int = 6
SEO_TITLE_UNIT_MAX: int = 9
SEO_TITLE_TARGET_MAX_LEN: int = 50


# ---------------------------------------------------------------------------
# Banned-claim / editorial-noise regexes (source L3546-L3563).
#
# T-201a-r4: Chinese (Hanja) alternatives removed; only Korean patterns
# remain. Korean is now expressed as literal characters (no \u escapes).
# ---------------------------------------------------------------------------

BANNED_CLAIM_RE = re.compile(
    r"100\s*%|AUTH\s*ENTIC|"
    r"정\s*품|진\s*품|"
    r"최고(?:급)?|최상급|"
    r"완벽(?:한|하게)?|"
    r"프리미엄",
    re.I,
)

EDITORIAL_NOISE_RE = re.compile(
    r"배송|출고|발송|택배|"
    r"판매처|판매자|스토어|"
    r"구매대행|주문\s*확인|"
    r"반품|교환|고객센터|"
    r"무료배송|특가|도매|"
    r"공장직영|쿠폰",
    re.I,
)

EMPTY_MARKETING_COPY_RE = re.compile(
    r"일상에\s*별별|당신만을\s*위한|"
    r"나만을\s*위한|별별한\s*하루|"
    r"삶의\s*격|생활의\s*격|"
    r"공간을\s*완성|물드를\s*완성|"
    r"감성을\s*더하|각을\s*더하|"
    r"완벽한\s*선택|소중한\s*사람을\s*위한",
    re.I,
)

SENSORY_COPY_NOISE_RE = EMPTY_MARKETING_COPY_RE


# ---------------------------------------------------------------------------
# List/string normalisation helpers (source L3712-L3744). Pure-Python.
# ---------------------------------------------------------------------------

def _jsonish_loads(value):
    """Best-effort parse a string into a Python object (dict/list/scalar).

    Returns the input unchanged on any failure.
    """
    import json

    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        return json.loads(text)
    except Exception:
        return value


def _list_strings(value, *, limit=20):
    """Coerce ``value`` into a flat list of unique, sanitised strings."""
    if isinstance(value, str):
        parsed = _jsonish_loads(value)
        items = parsed if isinstance(parsed, list) else [value]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    out: list[str] = []
    for item in items:
        text = _detail_safe_text(item)
        if text and text not in out:
            out.append(text[:80])
        if len(out) >= limit:
            break
    return out


def _normalize_dropped_entries(value, *, limit=20):
    """Normalise a list of "dropped keyword" rows into ``{word, reason}``."""
    rows = value if isinstance(value, (list, tuple)) else []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            word = _detail_safe_text(
                row.get("word") or row.get("term") or row.get("keyword")
            )
            reason = _detail_safe_text(
                row.get("reason") or row.get("why")
            ) or "title-excluded"
        else:
            word = _detail_safe_text(row)
            reason = "title-excluded"
        if word and not any(x.get("word") == word for x in out):
            out.append({"word": word[:40], "reason": reason[:120]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Agent-rule driven term stripping (source L3747-L3759).
# ---------------------------------------------------------------------------

def _agent_title_exclusion_terms():
    """Return the hard-coded title exclusion term list from agent rules.

    Source: read from the packaged ``COMPLIANCE_RULES.md`` asset. Returns
    an empty tuple when the asset is missing or the section is absent.

    T-201a-r4: the Korean-only token scan no longer admits Hanja ranges;
    only Hangul syllables are collected.
    """
    try:
        bundle = _agent_rules_bundle("COMPLIANCE_RULES.md")
    except Exception:
        return ()
    if not bundle:
        return ()
    lines = bundle.splitlines()
    collecting = False
    terms: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            lower = stripped.lower()
            if "banned" in lower or "exclusi" in lower:
                collecting = True
                continue
            if collecting:
                break
        if collecting:
            for token in re.findall(r"[가-힣]+", stripped):
                if token and token not in terms:
                    terms.append(token)
    return tuple(terms)


def _strip_agent_exclusion_terms(title, source_text, dropped):
    """Strip agent-rule exclusion terms from ``title`` and record drops.

    Source L3747. Returns ``(sanitised_title, dropped)``.
    """
    title = str(title or "")
    source_text = str(source_text or "")
    dropped = list(dropped or [])
    for term in _agent_title_exclusion_terms():
        if not term:
            continue
        found = term in source_text or term in title
        if term in title:
            title = re.sub(re.escape(term), " ", title)
        if found and not any(x.get("word") == term for x in dropped):
            dropped.append({"word": term, "reason": "agent-rule exclusion"})
    return _sanitize_seo_title(title, max_len=100), dropped


def _valid_seo_title(text):
    """Return True if ``text`` is a non-trivial usable SEO title.

    Source L3664. A title is valid when it is non-empty after stripping,
    contains at least one Hangul or ASCII letter.
    """
    text = str(text or "").strip()
    if not text:
        return False
    if not re.search(r"[A-Za-z가-힣]", text):
        return False
    return True


def _seo_sanitize_synonym_duplicates(text, *, max_len=SEO_TITLE_TARGET_MAX_LEN):
    """Collapse duplicate interior synonym tokens.

    Source L4297. The full synonym table lives in the agent rules and is
    not yet wired; for now this collapses repeated whitespace-separated
    tokens to keep titles deterministic.
    """
    tokens = str(text or "").split()
    seen: set[str] = set()
    kept: list[str] = []
    for token in tokens:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(token)
    return _sanitize_seo_title(" ".join(kept), max_len=max_len)


# ---------------------------------------------------------------------------
# Naming agent (source L3762-L3810, L5259-L5291).
#
# Per the spec the param ``title_cn`` is renamed ``source_title``; a falsy
# ``source_title`` raises ValueError before normalisation. This blocks
# downstream code from silently passing an empty source title.
# ---------------------------------------------------------------------------

def _fallback_naming_agent(source_title, props, category_path):
    """Build a deterministic fallback naming result.

    Source L3762 (param renamed). Returns a dict with keys
    ``title``, ``dropped``, ``kept_keywords``, ``story_terms``.
    """
    if not str(source_title or "").strip():
        raise ValueError(
            "source_title must be a non-empty string for naming fallback"
        )
    source_text = " ".join([
        str(source_title or ""),
        " ".join(_flatten_prop_terms(props, limit=24, clean=False)),
        str(category_path or ""),
    ])
    fallback = _sanitize_seo_title(str(source_title or ""), max_len=100)
    if not _valid_seo_title(fallback):
        fallback = _fallback_seo_title(
            _detail_safe_text(source_title), props, category_path
        )
    title, dropped = _strip_agent_exclusion_terms(fallback, source_text, [])
    title = _seo_sanitize_synonym_duplicates(title, max_len=100)
    if not title:
        title = _sanitize_seo_title(
            _props_summary(props)
            or str(category_path or "").split(">")[-1],
            max_len=100,
        )
    kept = [w for w in title.split() if w][:10]
    story = [row["word"] for row in dropped]
    return {
        "title": title or "item-detail",
        "dropped": dropped,
        "kept_keywords": kept,
        "story_terms": story,
    }


def _normalize_naming_result(data, source_title, props, category_path):
    """Normalise an LLM naming-agent response.

    Source L3786 (param ``title_cn`` renamed to ``source_title``).
    Falls back to :func:`_fallback_naming_agent` when ``data`` is missing,
    non-dict, or yields an invalid title.

    Raises:
        ValueError: if ``source_title`` is falsy (spec requirement).
    """
    if not str(source_title or "").strip():
        raise ValueError(
            "source_title must be a non-empty string for naming normalisation"
        )
    fallback = _fallback_naming_agent(source_title, props, category_path)
    if not isinstance(data, dict):
        return fallback
    source_text = " ".join([
        str(source_title or ""),
        " ".join(_flatten_prop_terms(props, limit=24, clean=False)),
        str(category_path or ""),
    ])
    title = _sanitize_seo_title(data.get("title"), max_len=100)
    if not _valid_seo_title(title):
        title = fallback["title"]
    dropped = _normalize_dropped_entries(data.get("dropped"))
    title, dropped = _strip_agent_exclusion_terms(title, source_text, dropped)
    title = _seo_sanitize_synonym_duplicates(title, max_len=100)
    kept = (
        _list_strings(data.get("kept_keywords") or data.get("kept"), limit=20)
        or fallback["kept_keywords"]
    )
    story = _list_strings(data.get("story_terms") or data.get("story"), limit=20)
    if not story:
        story = [row["word"] for row in dropped]
    return {
        "title": title or fallback["title"],
        "dropped": dropped,
        "kept_keywords": kept,
        "story_terms": story,
    }


# ---------------------------------------------------------------------------
# SEO search wiring (source L5017-L5257). Stub: the full planner depends
# on the keyword volume client and LLM provider.
# ---------------------------------------------------------------------------

def _apply_search_seo_to_naming(naming_result, source_title, props, category_path, *, source_context=None):
    """Refine a naming result with search-volume-aware SEO adjustments.

    Source L5017. Returns an ``llm_hint`` descriptor that bundles the
    current naming result and asks the host LLM to apply search-volume
    refinements (re-ordering units, swapping low-volume terms). The host
    returns a normalised naming-result dict.
    """
    from .common import _llm_hint

    if not isinstance(naming_result, dict):
        naming_result = _fallback_naming_agent(source_title, props, category_path)
    candidate_keywords = list(naming_result.get("kept_keywords") or [])
    volumes = {}
    try:
        volumes = seo.keyword_volume(candidate_keywords)
    except Exception:
        volumes = {}
    instruction = (
        "Refine the SEO naming result using the provided search volumes. "
        "Re-order title units to front-load higher-volume core terms. "
        "Swap any unit with volume 0 for a better candidate from the "
        "kept_keywords list. Preserve the 6-9 unit count and the "
        "category relevance. Return JSON in the same naming-result shape: "
        '{"title":"...","dropped":[...],"kept_keywords":[...],'
        '"story_terms":[...]}'
    )
    return _llm_hint(
        "apply_search_seo",
        input={
            "naming_result": naming_result,
            "source_title": str(source_title or ""),
            "category_path": str(category_path or ""),
            "keyword_volumes": volumes,
        },
        instruction=instruction,
    )


# ---------------------------------------------------------------------------
# Agent rules bundle (source L3683-L3710).
#
# Loads the packaged ``agents/*.md`` markdown assets. The assets ship
# inside the wheel (see ``pyproject.toml`` force-include) and are also
# present on disk in the source tree under ``<root>/agents/``.
# ---------------------------------------------------------------------------

# Module-level cache: agent_filename -> markdown text. Populated lazily;
# never invalidated (assets are immutable within a process lifetime).
_AGENT_RULES_CACHE: dict[str, str] = {}


def _agent_rules_bundle(agent_filename):
    """Load and cache the packaged agent rule markdown bundle.

    Source L3683. Reads ``<root>/agents/<agent_filename>`` (or the
    wheel-bundled ``clossify/agents/<agent_filename>`` copy) and returns
    the full markdown text. Results are cached per-filename.

    Args:
        agent_filename: basename of the agent markdown file, e.g.
            ``"naming_agent.md"`` or ``"COMPLIANCE_RULES.md"``.

    Returns:
        The markdown text (str).

    Raises:
        FileNotFoundError: when the asset does not exist on disk or in
            the package.
    """
    if agent_filename in _AGENT_RULES_CACHE:
        return _AGENT_RULES_CACHE[agent_filename]
    from . import common
    from importlib import resources

    candidates = []
    # 1. Source tree: <root>/agents/<filename>
    candidates.append(common.AGENTS_DIR / agent_filename)
    # 2. Wheel-bundled copy: clossify/agents/<filename>
    try:
        pkg_path = resources.files("clossify").joinpath(
            "agents", agent_filename
        )
        candidates.append(pkg_path)
    except Exception:
        pass

    for candidate in candidates:
        try:
            if hasattr(candidate, "read_text"):
                text = candidate.read_text(encoding="utf-8")
            else:
                text = str(candidate)
            if text and text.strip():
                _AGENT_RULES_CACHE[agent_filename] = text
                return text
        except Exception:
            continue
    raise FileNotFoundError(
        f"Agent asset not found: agents/{agent_filename} "
        "(T-201a: _agent_rules_bundle)"
    )


def _agent_llm_json(prompt, *, image_path=None, purpose="naming"):
    """Return an ``llm_hint`` for an agent-style LLM call.

    Source L5223. The original shelled out to the LLM provider and
    parsed the JSON response; the ported version returns an
    ``llm_hint`` descriptor so the MCP host executes the call. The host
    is expected to return parsed JSON.

    Args:
        prompt: the assembled prompt body.
        image_path: optional path to an image file (for vision-QA
            agents). Surfaced as ``input.image_path``.
        purpose: short label identifying the agent family
            (``"naming"``, ``"vision_qa"``, ``"copy"`` etc.). Used as
            the ``task`` id.

    Returns:
        ``llm_hint`` dict.
    """
    from .common import _llm_hint

    payload: dict = {"prompt": str(prompt or "")}
    if image_path:
        payload["image_path"] = str(image_path)
    return _llm_hint(
        str(purpose or "agent"),
        input=payload,
        instruction=str(prompt or "") or f"Agent call: {purpose}",
    )


def naming_agent(source_title, props, category_path):
    """Run the naming agent over a Korean source title.

    Source L5259 (param renamed ``title_cn`` -> ``source_title``).
    Returns an ``llm_hint`` descriptor for the MCP host LLM. The host
    receives the source title, flattened prop terms, the category path,
    and the full ``naming_agent.md`` rule bundle as the instruction
    context. It returns a naming-result JSON dict which the caller
    normalises via :func:`_normalize_naming_result`.

    Raises:
        ValueError: if ``source_title`` is falsy.
    """
    if not str(source_title or "").strip():
        raise ValueError(
            "source_title must be a non-empty string for naming_agent"
        )
    from .common import _llm_hint

    prop_terms = _flatten_prop_terms(props, limit=24, clean=False)
    instruction = common._resolve_instruction(
        lambda: _agent_rules_bundle("naming_agent.md"),
        (
            "Build an SEO-optimised Korean product name (6-9 units, "
            "front-loaded core type) from the supplied Korean source "
            "title, product properties and category path. Return JSON: "
            '{"title":"...","dropped":[{"word":"...","reason":"..."}],'
            '"kept_keywords":[...],"story_terms":[...]}'
        ),
    )
    return _llm_hint(
        "naming_agent",
        input={
            "source_title": str(source_title or ""),
            "props": prop_terms,
            "category_path": str(category_path or ""),
        },
        instruction=instruction,
    )


__all__ = [
    "SEO_TITLE_UNIT_MIN", "SEO_TITLE_UNIT_MAX", "SEO_TITLE_TARGET_MAX_LEN",
    "BANNED_CLAIM_RE", "EDITORIAL_NOISE_RE", "EMPTY_MARKETING_COPY_RE",
    "SENSORY_COPY_NOISE_RE",
    "_jsonish_loads", "_list_strings", "_normalize_dropped_entries",
    "_agent_title_exclusion_terms", "_strip_agent_exclusion_terms",
    "_valid_seo_title", "_seo_sanitize_synonym_duplicates",
    "_fallback_naming_agent", "_normalize_naming_result",
    "_apply_search_seo_to_naming", "_agent_rules_bundle",
    "_agent_llm_json", "naming_agent",
]


# Suppress unused-import lints for re-exports.
_ = (keyword_volume, seo)
