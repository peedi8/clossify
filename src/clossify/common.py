# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Shared constants and JSON IO helpers.

Ported from the original sourcing pipeline as the DAG root module.
Symbols whose source values contain forbidden tokens (e.g. the as_tel
literal) are resolved at runtime from config and raise ``ValueError``
when the config key is absent (fail-closed). No ``NotImplementedError``
stubs live in this module.
"""

from __future__ import annotations

import json
import os
from importlib.resources import files as _ir_files
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution (FIX-P1-install-paths).
#
# Two distinct categories of files exist and must not be conflated:
#
#   (1) Package data — read-only assets shipped inside the wheel
#       (``data/*.json``, ``agents/*.md``). These are resolved through
#       ``importlib.resources`` so the same code works in the source tree,
#       an editable install, and a wheel install. No ``__file__``-based
#       "repo root" estimation is used.
#
#   (2) User-space configuration and working state — files the operator
#       owns and mutates (``config.json``, ``prepared/``, logs, caches).
#       These live under ``CLOSSIFY_STATE_DIR`` if set, otherwise
#       ``<cwd>/.local/``. ``CLOSSIFY_CONFIG`` overrides the config file
#       location specifically (see ``naver_client.resolve_config_path``).
# ---------------------------------------------------------------------------
_PKG_DIR: Path = Path(str(_ir_files("clossify")))
DATA_DIR: Path = _PKG_DIR / "data"


def package_data_path(filename: str) -> Path:
    """Return the path to a packaged data file under ``data/``.

    Uses ``importlib.resources`` so the path resolves correctly in the
    source tree, in editable installs, and in wheel installs. Callers
    that read this path should still handle the file-not-found case by
    raising a clear error rather than silently returning empty data.
    """
    return DATA_DIR / str(filename)


def _state_dir() -> Path:
    """Return the working-state directory root.

    Honours ``CLOSSIFY_STATE_DIR`` (absolute path override). Defaults to
    ``<cwd>/.local`` so each working directory has its own state, which
    matches how operators actually run the server (one store per folder).
    """
    override = os.environ.get("CLOSSIFY_STATE_DIR")
    if override and override.strip():
        return Path(override.strip()).expanduser().resolve()
    return Path.cwd() / ".local"


# Package data locations (read-only).
AGENTS_DIR: Path = _PKG_DIR / "agents"

# User-space state locations (read/write, operator-owned).
STATE_DIR: Path = _state_dir()
# Back-compat alias: legacy code referred to the project-root local dir.
# LOCAL_DIR is the modern name for the same directory. Do not introduce new
# uses of LOCAL_DIR — prefer STATE_DIR.
LOCAL_DIR: Path = STATE_DIR
AGENT_LOG_PATH: Path = STATE_DIR / "agent_log.json"
AGENT_LOG_LIMIT: int = 200
CATEGORY_TRACE_LOG_PATH: Path = STATE_DIR / "category_trace.jsonl"
TRAIN_LOG_PATH: Path = STATE_DIR / "train_log.jsonl"
PREPARED_DIR: Path = STATE_DIR / "prepared"
PREPARED_PAYLOAD_VERSION: int = 1
LLM_TMP_DIR: Path = STATE_DIR / "llm_tmp"
KW_CACHE_PATH: Path = STATE_DIR / "kw_cache.json"
POSTTEAM_RATES_PATH: Path = STATE_DIR / "postteam_rates.json"

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
# sections was removed — those lanes are not part of this
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
    """Default AS telephone literal.

    Resolved from ``cfg()["brand"]["as_tel"]``; raises ``ValueError`` when
    absent (the literal phone number is a forbidden token).
    """
    tel = str(_cfg_section("brand").get("as_tel") or "").strip()
    if not tel:
        raise ValueError("brand.as_tel is not configured")
    return tel


# ---------------------------------------------------------------------------
# JSON IO helpers. Pure stdlib, no forbidden content.
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
# Config coercion helpers.
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
# llm_hint descriptor contract.
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
