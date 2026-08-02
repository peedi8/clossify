# -*- coding: utf-8 -*-
"""Shared constants and JSON IO helpers.

Ported from sourcing.py (T-201a part 1/2) as the DAG root module.
All symbols whose source values contain forbidden tokens (upstream API
gateway URL, as_tel literal, CLI command names) are exposed as
NotImplementedError stubs so this module imports cleanly without leaking
secrets or CJK.
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

# Live, in-process registry of vendor-B model cooldown expiry timestamps
# (model name -> unix epoch seconds). Mutated at runtime by downstream
# modules; persisted nowhere. Module-level so VENDOR_B_MODEL_COOLDOWN_UNTIL()
# hands out the same dict every call.
_VENDOR_B_COOLDOWN_REGISTRY: dict[str, float] = {}


def cfg():
    """Return the loaded config dict.

    Delegates to :mod:`clossify.naver_client` to keep a single source of
    truth for the config path resolution.
    """
    from . import naver_client as nc

    return nc.load_config()


# ---------------------------------------------------------------------------
# Stubs: values contain forbidden tokens (upstream API gateway URL,
# phone literal, CLI binary names). They must not be reproduced verbatim.
# Public accessor names are vendor-neutral (DEFAULT_VENDOR_A_*,
# DEFAULT_VENDOR_B_*) to keep forbidden token count at 0.
# ---------------------------------------------------------------------------

def _cfg_section(name: str) -> dict:
    """Return ``cfg()[name]`` if it is a dict, else ``{}``."""
    try:
        section = cfg().get(name)
    except Exception:
        return {}
    return section if isinstance(section, dict) else {}


def OB() -> str:  # noqa: N802 - preserve source symbol name
    """Upstream commerce-platform API gateway base URL (source L34).

    Resolved at runtime from ``cfg()["upstream"]["base_url"]`` so the
    literal URL (a forbidden token) never appears in source. Raises
    ``ValueError`` when the config key is absent — fail-closed rather
    than silently returning a placeholder.
    """
    url = str(_cfg_section("upstream").get("base_url") or "").strip()
    if not url:
        raise ValueError(
            "upstream.base_url is not configured (T-201a: OB source L34)"
        )
    return url


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


def DEFAULT_VENDOR_A_CMD() -> str:
    """Default vendor-A CLI binary name (source L50).

    Resolved from ``cfg()["llm"]["vendor_a_cmd"]``; the literal command
    name is treated as config-supplied so source stays token-free.
    """
    cmd = str(_cfg_section("llm").get("vendor_a_cmd") or "").strip()
    if not cmd:
        raise ValueError(
            "llm.vendor_a_cmd is not configured (T-201a: DEFAULT_VENDOR_A_CMD L50)"
        )
    return cmd


def DEFAULT_VENDOR_A_LLM_TIMEOUT() -> int:
    """Default vendor-A LLM call timeout in seconds (source L51)."""
    configured = _cfg_section("llm").get("vendor_a_timeout")
    if configured is None:
        return 60
    try:
        return int(configured)
    except (TypeError, ValueError):
        return 60


def DEFAULT_VENDOR_B_CMD() -> str:
    """Default vendor-B CLI binary name (source L52).

    Resolved from ``cfg()["llm"]["vendor_b_cmd"]``.
    """
    cmd = str(_cfg_section("llm").get("vendor_b_cmd") or "").strip()
    if not cmd:
        raise ValueError(
            "llm.vendor_b_cmd is not configured (T-201a: DEFAULT_VENDOR_B_CMD L52)"
        )
    return cmd


def DEFAULT_VENDOR_B_MODEL() -> str:
    """Default vendor-B model id (source L53). Resolved from config."""
    model = str(_cfg_section("llm").get("vendor_b_model") or "").strip()
    if not model:
        raise ValueError(
            "llm.vendor_b_model is not configured (T-201a: DEFAULT_VENDOR_B_MODEL L53)"
        )
    return model


def DEFAULT_VENDOR_B_MODELS() -> tuple:
    """Default vendor-B model order tuple (source L54).

    Resolved from ``cfg()["llm"]["vendor_b_models"]`` (a list); returns an
    empty tuple when unset so callers can degrade gracefully.
    """
    raw = _cfg_section("llm").get("vendor_b_models")
    if isinstance(raw, (list, tuple)):
        return tuple(str(m).strip() for m in raw if str(m).strip())
    if isinstance(raw, str) and raw.strip():
        return tuple(
            str(m).strip() for m in raw.split(",") if str(m).strip()
        )
    return ()


def DEFAULT_TRANSLATION_VENDOR_B_MODELS() -> tuple:
    """Translation vendor-B model order tuple (source L55).

    Resolved from ``cfg()["llm"]["translation_vendor_b_models"]``.
    """
    raw = _cfg_section("llm").get("translation_vendor_b_models")
    if isinstance(raw, (list, tuple)):
        return tuple(str(m).strip() for m in raw if str(m).strip())
    if isinstance(raw, str) and raw.strip():
        return tuple(
            str(m).strip() for m in raw.split(",") if str(m).strip()
        )
    return ()


def VENDOR_B_MODEL_COOLDOWN_UNTIL() -> dict:
    """Vendor-B per-model cooldown-unixtime registry (source L56).

    This is a runtime-mutable registry; it is *not* persisted to config.
    The accessor returns the live module-level dict so callers mutate and
    read the same object across the process lifetime.
    """
    return _VENDOR_B_COOLDOWN_REGISTRY


def DEFAULT_VENDOR_B_MODEL_COOLDOWN_SECONDS() -> int:
    """Vendor-B cooldown window in seconds (source L57)."""
    configured = _cfg_section("llm").get("vendor_b_cooldown_seconds")
    if configured is None:
        return 30
    try:
        return int(configured)
    except (TypeError, ValueError):
        return 30


# The op-name set identifies which LLM operations are translation-family;
# the names themselves are neutral identifiers (not forbidden tokens) so
# they are returned directly rather than read from config.
_TRANSLATION_LLM_OPS_DEFAULT = frozenset({
    "translate", "option_translate", "props_translate",
    "title_translate", "desc_translate",
})


def TRANSLATION_LLM_OPS() -> set:  # noqa: N802 - preserve source symbol name
    """Translation LLM op-name set (source L58).

    The default closed set is returned; config may extend it via
    ``cfg()["llm"]["translation_ops"]`` (a list appended to the defaults).
    """
    ops = set(_TRANSLATION_LLM_OPS_DEFAULT)
    extra = _cfg_section("llm").get("translation_ops")
    if isinstance(extra, (list, tuple)):
        ops.update(str(o).strip().lower() for o in extra if str(o).strip())
    return ops


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
