"""Shared constants and JSON IO helpers.

Ported from sourcing.py (T-201a part 1/2) as the DAG root module.
Symbols whose source values contain forbidden tokens (e.g. the as_tel
literal) are resolved at runtime from config and raise ``ValueError``
when the config key is absent (fail-closed). No ``NotImplementedError``
stubs live in this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root resolution.
#
# Source used ``Path(__file__).resolve().parents[1]`` because sourcing.py
# lived at ``<root>/backend/sourcing.py``. This module lives at
# ``<root>/src/clossify/common.py`` so the project root is two parents up.
# Resolved eagerly and asserted non-empty per the spec note on ROOT_DIR.
# ---------------------------------------------------------------------------
ROOT_DIR: Path = Path(__file__).resolve().parents[2]
assert ROOT_DIR.name, "ROOT_DIR must resolve to a named directory"

AGENTS_DIR: Path = ROOT_DIR / "agents"
AGENT_LOG_PATH: Path = ROOT_DIR / ".local" / "agent_log.json"
AGENT_LOG_LIMIT: int = 200
CATEGORY_TRACE_LOG_PATH: Path = ROOT_DIR / ".local" / "category_trace.jsonl"
TRAIN_LOG_PATH: Path = ROOT_DIR / ".local" / "train_log.jsonl"
PREPARED_DIR: Path = ROOT_DIR / ".local" / "prepared"
PREPARED_PAYLOAD_VERSION: int = 1
LLM_TMP_DIR: Path = ROOT_DIR / ".local" / "llm_tmp"
KW_CACHE_PATH: Path = ROOT_DIR / ".local" / "kw_cache.json"
POSTTEAM_RATES_PATH: Path = ROOT_DIR / ".local" / "postteam_rates.json"

SEO_MIN_SEARCH_VOLUME: int = 10
VISION_QA_MAX_SIDE: int = 1568
NAVER_OPTION_PRICE_DELTA_LIMIT_KRW: int = 500000


def cfg():
    """Return the loaded config dict.

    Delegates to :mod:`clossify.naver_client` to keep a single source of
    truth for the config path resolution.
    """
    from . import naver_client as nc

    return nc.load_config()


# ---------------------------------------------------------------------------
# Config section accessor. Used by live accessors below
# (DEFAULT_AS_TEL). The ported layer that read "upstream" and "llm"
# sections was removed in T-114 — those lanes are not part of this
# product (text inference is owned by the MCP client).
# ---------------------------------------------------------------------------

def _cfg_section(name: str) -> dict:
    """Return ``cfg()[name]`` if it is a dict, else ``{}``."""
    try:
        section = cfg().get(name)
    except Exception:
        return {}
    return section if isinstance(section, dict) else {}


def DEFAULT_AS_TEL() -> str:
    """Default AS telephone literal (source L43).

    Resolved from ``cfg()["brand"]["as_tel"]``; raises ``ValueError`` when
    absent (the literal phone number is a forbidden token).
    """
    tel = str(_cfg_section("brand").get("as_tel") or "").strip()
    if not tel:
        raise ValueError(
            "brand.as_tel is not configured (T-201a: DEFAULT_AS_TEL source L43)"
        )
    return tel


# ---------------------------------------------------------------------------
# JSON IO helpers (source L612-L624). Pure stdlib, no forbidden content.
# ---------------------------------------------------------------------------

def _read_json_file(path, default):
    """Read JSON from ``path``; return ``default`` on any failure."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json_file(path, data):
    """Atomically write ``data`` as UTF-8 JSON to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Config coercion helpers (source L8484-L8502, L9270-L9274).
# ---------------------------------------------------------------------------

def _bool_config(value, default=False):
    """Coerce ``value`` to bool using the sourcing.py truthy/falsy tables."""
    if value is None:
        return default
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off", "none"):
            return False
        return default
    return bool(value)


def _int_config(value, default):
    """Coerce ``value`` to int; return ``default`` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    """Coerce ``value`` to float; return ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# llm_hint descriptor contract (T-201a-r).
#
# The MCP host (the external LLM client driving this MCP server) performs
# all real LLM work. Functions that used to call an LLM CLI now return an
# ``llm_hint`` descriptor dict instead. The host inspects the dict, executes
# the LLM call with its own configured provider/model, and passes the
# parsed result back to the caller (typically via a second MCP tool call).
#
# Descriptor shape:
#     {"needs_llm": True,
#      "task": "<short task id>",
#      "input": {...},             # structured arguments for the host LLM
#      "instruction": "<prompt>"}  # prompt body (from packaged agents/*.md)
# ---------------------------------------------------------------------------

class LLMGenerateError(RuntimeError):
    """Raised when an llm_hint descriptor cannot be constructed.

    This is **not** a provider failure — the host LLM never ran. It
    indicates a programming error (bad input shape, missing required
    field) in the caller.
    """


def _llm_hint(task, *, input, instruction):
    """Build a canonical ``llm_hint`` descriptor for the MCP host.

    Args:
        task: short, stable task id (e.g. ``"translate_title"``,
            ``"naming_agent"``). The MCP host keys on this.
        input: structured arguments the host LLM consumes. Must be a
            JSON-serialisable dict.
        instruction: the prompt body, typically sourced from a packaged
            ``agents/*.md`` asset.

    Returns:
        ``{"needs_llm": True, "task": task, "input": input,
        "instruction": instruction}``.

    Raises:
        LLMGenerateError: when ``task``/``instruction`` are empty or
            ``input`` is not a dict.
    """
    if not str(task or "").strip():
        raise LLMGenerateError("llm_hint.task must be a non-empty string")
    if not isinstance(input, dict):
        raise LLMGenerateError("llm_hint.input must be a dict")
    if not str(instruction or "").strip():
        raise LLMGenerateError("llm_hint.instruction must be non-empty")
    return {
        "needs_llm": True,
        "task": str(task),
        "input": input,
        "instruction": str(instruction),
    }


def _resolve_instruction(*candidates):
    """Return the first non-empty instruction string from ``candidates``.

    Each candidate may be a string, a callable returning a string, or
    ``None``. Callables are invoked lazily so an asset read only happens
    when needed.
    """
    for candidate in candidates:
        if candidate is None:
            continue
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:
                continue
        text = str(candidate or "").strip()
        if text:
            return text
    return ""
