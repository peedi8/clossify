# Clossify — Naver SmartStore listing automation.
# Copyright (c) 2026 3rdhand. Licensed under the Sustainable Use License v1.0.
# You may use and modify this software for your own internal business or personal
# purposes. Providing it to others — including as a hosted or paid service — is
# permitted only free of charge and for non-commercial purposes. See LICENSE.md.
"""Naming agent and SEO copy normalisation.

Ported from the original sourcing pipeline. Terminal node of the DAG:
depends on :mod:`common`, :mod:`text_props`, :mod:`keyword_volume`
and :mod:`seo`.

The signature of :func:`_normalize_naming_result` uses
``source_title`` (renamed from the original ``title_cn``); a falsy
``source_title`` now raises :class:`ValueError` before normalisation.

Chinese detection / stripping code has been removed entirely.
This product only ever ingests Korean user input, so there is no path
for Chinese text to enter the pipeline. Korean marketing-claim filters
(``BANNED_CLAIM_RE`` etc.) are preserved and now use literal Korean.

The text-filter regexes (``BANNED_CLAIM_RE``,
``EDITORIAL_NOISE_RE``, ...) are imported from :mod:`text_props`
(their canonical home) rather than redefined here — this resolves the
circular dependency that previously forced a lazy-import workaround.
The full synonym-dedup machinery (``_seo_sanitize_synonym_duplicates``
and its helpers) is preserved. Search-volume lookup failures in
``_apply_search_seo_to_naming`` are not silently swallowed.
"""

from __future__ import annotations

import re

from . import common, keyword_volume, seo
from .text_props import (
    BANNED_CLAIM_RE,
    EDITORIAL_NOISE_RE,
    EMPTY_MARKETING_COPY_RE,
    SENSORY_COPY_NOISE_RE,
    _detail_safe_text,
    _fallback_seo_title,
    _flatten_prop_terms,
    _props_summary,
    _sanitize_seo_title,
)

# ---------------------------------------------------------------------------
# SEO title targets.
# ---------------------------------------------------------------------------

SEO_TITLE_UNIT_MIN: int = 6
SEO_TITLE_UNIT_MAX: int = 9
SEO_TITLE_TARGET_MAX_LEN: int = 50


# ---------------------------------------------------------------------------
# Synonym / semantic duplicate groups.
#
# Korean literals are required for the dedup logic to function.
# ---------------------------------------------------------------------------

SEO_SYNONYM_DEDUP_GROUPS = (("화병", "꽃병", "플라워베이스", "플라워 베이스", "vase", "베이스"),)

SEO_SEMANTIC_DUPLICATE_GROUPS = (
    ("화병", "꽃병", "플라워베이스", "플라워 베이스", "vase", "베이스"),
    (
        "인테리어",
        "거실인테리어",
        "사무실인테리어",
        "홈데코",
        "집꾸미기",
        "소품샵",
        "데코",
        "장식소품",
        "인테리어소품",
    ),
    ("도자기", "토기", "세라믹"),
    ("유리", "글라스"),
    ("미니", "작은", "소형"),
    ("화이트", "아이보리", "크림"),
    ("그레이", "회색"),
)


# ---------------------------------------------------------------------------
# List/string normalisation helpers. Pure-Python.
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
    elif isinstance(value, list | tuple):
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
    rows = value if isinstance(value, list | tuple) else []
    out: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            word = _detail_safe_text(row.get("word") or row.get("term") or row.get("keyword"))
            reason = _detail_safe_text(row.get("reason") or row.get("why")) or "title-excluded"
        else:
            word = _detail_safe_text(row)
            reason = "title-excluded"
        if word and not any(x.get("word") == word for x in out):
            out.append({"word": word[:40], "reason": reason[:120]})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Agent-rule driven term stripping.
# ---------------------------------------------------------------------------


def _agent_title_exclusion_terms():
    """Return the hard-coded title exclusion term list from agent rules.

    Source: read from the packaged ``COMPLIANCE_RULES.md`` asset. Returns
    an empty tuple when the asset is missing or the section is absent.

    The Korean-only token scan does not admit Hanja ranges; only Hangul
    syllables are collected.
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

    Returns ``(sanitised_title, dropped)``.
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

    A title is valid when it is non-empty after stripping and
    contains at least one Hangul or ASCII letter.
    """
    text = str(text or "").strip()
    if not text:
        return False
    if not re.search(r"[A-Za-z가-힣]", text):
        return False
    return True


# ---------------------------------------------------------------------------
# Synonym dedup machinery.
#
# The full implementation collapses synonym groups (e.g. 화병/꽃병/플라워베이스)
# rather than only exact-duplicate whitespace tokens.
# ---------------------------------------------------------------------------


def _seo_title_units(title):
    """Split a title string into normalised keyword units."""
    return keyword_volume._clean_search_keyword(title, max_len=120).split()


def _seo_semantic_group_key(term):
    """Return a semantic group key for ``term``.

    Units that share a semantic group (e.g. colour variants, material
    variants) are treated as duplicates by ``_seo_add_title_unit``.
    """
    compact = seo._keyword_compact(term)
    if not compact:
        return ""
    for idx, group in enumerate(SEO_SEMANTIC_DUPLICATE_GROUPS):
        group_compacts = seo._seo_term_compacts(group)
        if any(
            item and (item == compact or item in compact or compact in item)
            for item in group_compacts
        ):
            return f"group:{idx}"
    return compact


def _seo_add_title_unit(units, term, *, allow_group_duplicate=False):
    """Append ``term`` to ``units`` with dedup.

    Returns True when at least one unit was added.
    """
    added = False
    for unit in _seo_title_units(term):
        compact = seo._keyword_compact(unit)
        if not compact:
            continue
        if any(compact == seo._keyword_compact(existing) for existing in units):
            continue
        group = _seo_semantic_group_key(unit)
        if (
            not allow_group_duplicate
            and group
            and any(_seo_semantic_group_key(existing) == group for existing in units)
        ):
            continue
        units.append(unit)
        added = True
        if len(units) >= SEO_TITLE_UNIT_MAX:
            break
    return added


def _seo_synonym_normalized_compact(text):
    """Return a synonym-normalised compact form of ``text``.

    Aliases within a synonym group are rewritten to the group's canonical
    (first) member so that ``화병`` and ``꽃병`` collapse to the same key.
    """
    compact = seo._keyword_compact(text)
    if not compact:
        return ""
    normalized = compact
    for group in SEO_SYNONYM_DEDUP_GROUPS:
        raw_compacts = seo._seo_term_compacts(group)
        group_compacts = sorted(raw_compacts, key=len, reverse=True)
        if not group_compacts:
            continue
        canonical = raw_compacts[0]
        for alias in group_compacts:
            if alias and alias in normalized:
                normalized = normalized.replace(alias, canonical)
    return normalized


def _seo_synonym_hits(text, group):
    """Return the subset of ``group`` whose compact form appears in ``text``."""
    compact = seo._keyword_compact(text)
    hits = []
    for term in sorted(group, key=lambda item: len(seo._keyword_compact(item)), reverse=True):
        term_compact = seo._keyword_compact(term)
        if not term_compact or term_compact not in compact:
            continue
        if any(term_compact in seo._keyword_compact(existing) for existing in hits):
            continue
        hits.append(term)
    return hits


def _seo_sanitize_synonym_duplicates(text, *, max_len=SEO_TITLE_TARGET_MAX_LEN):
    """Collapse duplicate interior synonym tokens.

    Walks the title units; for each unit, compute its synonym-normalised
    key. If a unit is synonymous with one already kept, drop it. This
    properly handles groups like ``화병/꽃병/플라워베이스/vase`` rather
    than only exact whitespace duplicates.
    """
    units = []
    seen_synonyms: set[str] = set()
    for unit in _seo_title_units(text):
        norm = _seo_synonym_normalized_compact(unit)
        synonym_group = ""
        if norm != seo._keyword_compact(unit):
            synonym_group = norm
        else:
            for group in SEO_SYNONYM_DEDUP_GROUPS:
                hits = _seo_synonym_hits(unit, group)
                if hits:
                    synonym_group = _seo_synonym_normalized_compact(hits[0])
                    break
        if synonym_group:
            if synonym_group in seen_synonyms:
                continue
            seen_synonyms.add(synonym_group)
        _seo_add_title_unit(units, unit)
    return _sanitize_seo_title(" ".join(units), max_len=max_len)


# ---------------------------------------------------------------------------
# Naming agent.
#
# The param ``title_cn`` from the original pipeline is renamed
# ``source_title`` here; a falsy ``source_title`` raises ValueError before
# normalisation. This blocks downstream code from silently passing an empty
# source title.
# ---------------------------------------------------------------------------


def _fallback_naming_agent(source_title, props, category_path):
    """Build a deterministic fallback naming result.

    Returns a dict with keys ``title``, ``dropped``, ``kept_keywords``,
    ``story_terms``.
    """
    if not str(source_title or "").strip():
        raise ValueError("source_title must be a non-empty string for naming fallback")
    source_text = " ".join(
        [
            str(source_title or ""),
            " ".join(_flatten_prop_terms(props, limit=24, clean=False)),
            str(category_path or ""),
        ]
    )
    fallback = _sanitize_seo_title(str(source_title or ""), max_len=100)
    if not _valid_seo_title(fallback):
        fallback = _fallback_seo_title(_detail_safe_text(source_title), props, category_path)
    title, dropped = _strip_agent_exclusion_terms(fallback, source_text, [])
    title = _seo_sanitize_synonym_duplicates(title, max_len=100)
    if not title:
        title = _sanitize_seo_title(
            _props_summary(props) or str(category_path or "").split(">")[-1],
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

    Falls back to :func:`_fallback_naming_agent` when ``data`` is missing,
    non-dict, or yields an invalid title.

    Raises:
        ValueError: if ``source_title`` is falsy.
    """
    if not str(source_title or "").strip():
        raise ValueError("source_title must be a non-empty string for naming normalisation")
    fallback = _fallback_naming_agent(source_title, props, category_path)
    if not isinstance(data, dict):
        return fallback
    source_text = " ".join(
        [
            str(source_title or ""),
            " ".join(_flatten_prop_terms(props, limit=24, clean=False)),
            str(category_path or ""),
        ]
    )
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
# SEO search wiring. Stub: the full planner depends on the keyword volume
# client and LLM provider.
# ---------------------------------------------------------------------------


def _apply_search_seo_to_naming(
    naming_result, source_title, props, category_path, *, source_context=None
):
    """Refine a naming result with search-volume-aware SEO adjustments.

    Returns an ``llm_hint`` descriptor that bundles the current naming
    result and asks the host LLM to apply search-volume refinements
    (re-ordering units, swapping low-volume terms). The host returns a
    normalised naming-result dict.

    Search-volume lookup failures are not silently swallowed
    (``except Exception: volumes = {}``). When the lookup raises, the
    exception propagates to the caller. A caller that wants graceful
    degradation must catch it explicitly and decide.
    """
    from .common import _llm_hint

    if not isinstance(naming_result, dict):
        naming_result = _fallback_naming_agent(source_title, props, category_path)
    candidate_keywords = list(naming_result.get("kept_keywords") or [])
    volumes = seo.keyword_volume(candidate_keywords)
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
# Agent rules bundle.
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

    Reads ``<root>/agents/<agent_filename>`` (or the wheel-bundled
    ``clossify/agents/<agent_filename>`` copy) and returns the full
    markdown text. Results are cached per-filename.

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
    from importlib import resources

    from . import common

    candidates = []
    # 1. Source tree: <root>/agents/<filename>
    candidates.append(common.AGENTS_DIR / agent_filename)
    # 2. Wheel-bundled copy: clossify/agents/<filename>
    try:
        pkg_path = resources.files("clossify").joinpath("agents", agent_filename)
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
    raise FileNotFoundError(f"Agent asset not found: agents/{agent_filename}")


def _agent_llm_json(prompt, *, image_path=None, purpose="naming"):
    """Return an ``llm_hint`` for an agent-style LLM call.

    The original shelled out to the LLM provider and parsed the JSON
    response; the ported version returns an ``llm_hint`` descriptor so the
    MCP host executes the call. The host is expected to return parsed
    JSON.

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

    Returns an ``llm_hint`` descriptor for the MCP host LLM. The host
    receives the source title, flattened prop terms, the category path,
    and the full ``naming_agent.md`` rule bundle as the instruction
    context. It returns a naming-result JSON dict which the caller
    normalises via :func:`_normalize_naming_result`.

    Raises:
        ValueError: if ``source_title`` is falsy.
    """
    if not str(source_title or "").strip():
        raise ValueError("source_title must be a non-empty string for naming_agent")
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


def build_seo_title(title_ko, props, category_path):
    """Build an SEO product title via the naming agent.

    Delegates to :func:`naming_agent`, which returns either a normalised
    result dict or an ``llm_hint`` descriptor (when the host LLM must
    run). On invalid output the deterministic
    :func:`_fallback_seo_title` is used.

    This helper lives here rather than in :mod:`text_props` so that
    ``text_props`` does not need to import :mod:`copywriting` (which
    would create an import cycle).
    """
    try:
        result = naming_agent(title_ko, props, category_path)
    except ValueError:
        raise
    except Exception:
        return _fallback_seo_title(title_ko, props, category_path)
    if isinstance(result, dict) and "title" in result:
        title = _sanitize_seo_title(result.get("title"), max_len=100)
        if title:
            return title
    return _fallback_seo_title(title_ko, props, category_path)


__all__ = [
    "BANNED_CLAIM_RE",
    "EDITORIAL_NOISE_RE",
    "EMPTY_MARKETING_COPY_RE",
    "SENSORY_COPY_NOISE_RE",
    "SEO_SEMANTIC_DUPLICATE_GROUPS",
    "SEO_SYNONYM_DEDUP_GROUPS",
    "SEO_TITLE_TARGET_MAX_LEN",
    "SEO_TITLE_UNIT_MAX",
    "SEO_TITLE_UNIT_MIN",
    "_agent_llm_json",
    "_agent_rules_bundle",
    "_agent_title_exclusion_terms",
    "_apply_search_seo_to_naming",
    "_fallback_naming_agent",
    "_jsonish_loads",
    "_list_strings",
    "_normalize_dropped_entries",
    "_normalize_naming_result",
    "_seo_add_title_unit",
    "_seo_sanitize_synonym_duplicates",
    "_seo_semantic_group_key",
    "_seo_synonym_hits",
    "_seo_synonym_normalized_compact",
    "_seo_title_units",
    "_strip_agent_exclusion_terms",
    "_valid_seo_title",
    "build_seo_title",
    "naming_agent",
]


# Suppress unused-import lints for re-exports.
_ = (keyword_volume, seo)
