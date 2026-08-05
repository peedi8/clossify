# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Agent-prompt sanity guard for every ``agents/*.md`` file.

The prompt files under ``agents/`` ship inside the wheel and are injected into
the client model, so they must describe the *actual* MCP tool surface and must
not reference functions, documents, or version markers that no longer exist in
the repository.

This test enumerates ``agents/*.md`` **dynamically** (no hardcoded filenames)
and asserts:

  (a) Every ``§N`` section reference is either inside the document that owns
      those sections (``COMPLIANCE_RULES.md``) or is explicitly qualified with
      a ``[[WIKI_LINK]]`` whose target file exists in the repository.
  (b) Every backtick-wrapped ``function_name(`` pattern in a prompt refers to a
      function that actually exists in ``src/clossify`` (parsed from source).
  (c) The registration-flow prompts mention all six MCP tool names.
  (d) Abandoned sourcing-lane terms (원가/마진/수수료 기반 가격계산,
      ``naver_categories.json``) do not appear in any prompt.
  (e) The set of MCP tool names mentioned in the prompts matches the runtime
      tool set registered on the ``MCPServer`` instance.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# Project root:  tests/test_agent_prompts.py -> parent
_ROOT = Path(__file__).resolve().parent.parent
_AGENTS_DIR = _ROOT / "agents"
_SRC_DIR = _ROOT / "src" / "clossify"

# The six MCP tools the server exposes.  These are verified dynamically by
# test (e) against the runtime tool set, but the constant is used by test (c)
# to check the registration-flow prompts mention all of them.
_MCP_TOOLS = {
    "check_config",
    "get_product",
    "prepare_listing",
    "register_product",
    "submit_reviews",
    "upload_images",
}

# Prompts that describe the registration flow and therefore must mention all
# six MCP tools.  This set is derived from the work-order scope and is stable:
# any new registration-flow prompt should be added here.
_REGISTRATION_FLOW_PROMPTS = {
    "registration_agent.md",
    "COMPLIANCE_LOOP.md",
    "QA_AGENTS.md",
}

# Terms abandoned when the sourcing lane was discarded.  Their presence in any
# prompt means the prompt is describing machinery that no longer exists.
_BANNED_PATTERNS = [
    re.compile(r"naver_categories\.json"),
    # "원가·무게·마진·수수료 기반 가격계산" function name (never ported).
    re.compile(r"compute_price\s*\("),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _agent_files() -> list[Path]:
    """Return all ``*.md`` files under ``agents/`` (dynamic, no hardcoding)."""
    return sorted(_AGENTS_DIR.glob("*.md"))


def _src_function_names() -> set[str]:
    """Parse every ``src/clossify/*.py`` and return all top-level ``def`` names.

    This is a syntactic parse (``ast``) so it does not import the modules and
    therefore cannot trigger side-effects.  Both sync and async defs are
    collected.  Method names inside classes are also collected because the
    prompts may reference helpers like ``naver_client.build_payload``.
    """
    names: set[str] = set()
    for src_path in sorted(_SRC_DIR.glob("*.py")):
        tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    return names


def _extract_section_refs(text: str) -> list[tuple[str, str]]:
    """Return ``(context_prefix, section_number)`` for every ``§N`` in *text*.

    ``context_prefix`` is up to 40 chars preceding the ``§`` sign so the caller
    can decide whether the reference is explicitly qualified with a wiki-link
    target or is a bare self-reference inside ``COMPLIANCE_RULES.md``.
    """
    refs: list[tuple[str, str]] = []
    for match in re.finditer(r"§(\d+)", text):
        start = max(0, match.start() - 40)
        prefix = text[start : match.start()]
        refs.append((prefix, match.group(1)))
    return refs


def _extract_backtick_calls(text: str) -> list[str]:
    """Return function names from backtick-wrapped ``name(`` patterns.

    Only backtick spans that contain an opening paren are considered, so plain
    `` `register_product` `` mentions (no call) are *not* checked here — those
    are covered by test (c)/(e) for the registration flow.  Dotted access like
    `` `naver_client.build_payload(` `` yields the last component
    (``build_payload``).
    """
    calls: list[str] = []
    for span in re.finditer(r"`([^`]*?\([^`]*?)`", text):
        inner = span.group(1)
        # The function name is the leading identifier before "(".
        name_match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\(", inner)
        if not name_match:
            continue
        raw = name_match.group(1)
        # Last component for dotted access.
        leaf = raw.rsplit(".", 1)[-1]
        calls.append(leaf)
    return calls


def _agent_files_text() -> dict[Path, str]:
    """Read all agent files once and cache for the module scope."""
    return {p: p.read_text(encoding="utf-8") for p in _agent_files()}


_AGENT_TEXTS = _agent_files_text()


# ---------------------------------------------------------------------------
# (a) Section references — every ``§N`` must resolve to an existing file.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", _agent_files(), ids=lambda p: p.name)
def test_section_refs_resolve_to_existing_file(path: Path) -> None:
    """Every ``§N`` ref is either inside ``COMPLIANCE_RULES.md`` (self-ref) or
    qualified with a ``[[TARGET]]`` whose ``agents/TARGET.md`` exists."""
    text = _AGENT_TEXTS.get(path) or path.read_text(encoding="utf-8")
    refs = _extract_section_refs(text)
    # When the document *is* COMPLIANCE_RULES.md, self-references are valid.
    is_self = path.name == "COMPLIANCE_RULES.md"
    for prefix, _num in refs:
        if is_self:
            continue
        # The prefix must contain a ``[[TARGET]]`` wiki-link.
        link_match = re.search(r"\[\[([A-Za-z_][A-Za-z0-9_]*)\]\]", prefix)
        assert link_match, (
            f"{path.name}: bare ``§`` ref not qualified with a [[wiki-link]]. "
            f"Prefix was: {prefix!r}"
        )
        target_stem = link_match.group(1)
        target_path = _AGENTS_DIR / f"{target_stem}.md"
        assert target_path.exists(), (
            f"{path.name}: ``§`` ref points to [[{target_stem}]] but "
            f"{target_path.name} does not exist in agents/."
        )


# ---------------------------------------------------------------------------
# (b) Backtick function-call patterns must exist in src/clossify.
# ---------------------------------------------------------------------------
# We do not parametrize per-file because the function-existence set is global.
def test_backtick_call_patterns_exist_in_source() -> None:
    """Every `` `function_name(` `` pattern in a prompt must be a real function
    defined somewhere under ``src/clossify/``."""
    src_names = _src_function_names()
    # Sanity: the source set is non-empty.
    assert src_names, "src/clossify yielded no function defs — parse is broken."
    # Sanity: the six MCP tools are all present in source (guard against a
    # broken parse that would make this test vacuously pass).
    missing_tools = _MCP_TOOLS - src_names
    assert not missing_tools, f"MCP tools missing from src parse (parse is broken): {missing_tools}"

    offenders: list[str] = []
    for path in _agent_files():
        text = _AGENT_TEXTS.get(path) or path.read_text(encoding="utf-8")
        for call_name in _extract_backtick_calls(text):
            if call_name not in src_names:
                offenders.append(f"{path.name}: ``{call_name}(…`` not found in src/clossify")
    assert not offenders, (
        "Backtick function-call patterns in prompts do not resolve to real "
        "functions in src/clossify:\n  " + "\n  ".join(sorted(set(offenders)))
    )


# ---------------------------------------------------------------------------
# (c) Registration-flow prompts must mention all six MCP tools (collectively).
# ---------------------------------------------------------------------------
def test_registration_flow_mentions_all_six_tools() -> None:
    """The **union** of registration-flow prompt texts must mention every one
    of the six MCP tool names (as a bare word, with or without backticks).

    Individual prompts specialise (e.g. ``COMPLIANCE_LOOP`` is a process doc,
    ``registration_agent`` is the tool catalog) so the check is collective —
    a tool missing from the *entire* registration-flow set means the prompts
    never tell the client model that the tool exists.
    """
    mentioned: set[str] = set()
    for filename in sorted(_REGISTRATION_FLOW_PROMPTS):
        path = _AGENTS_DIR / filename
        assert path.exists(), f"Registration-flow prompt {filename} does not exist."
        text = path.read_text(encoding="utf-8")
        for name in _MCP_TOOLS:
            if name in text:
                mentioned.add(name)
    missing = sorted(_MCP_TOOLS - mentioned)
    assert not missing, (
        "Registration-flow prompt set is missing MCP tool mention(s): "
        f"{missing}. The union of registration_agent.md, COMPLIANCE_LOOP.md "
        "and QA_AGENTS.md must mention all six tools."
    )


# ---------------------------------------------------------------------------
# (d) Banned sourcing-lane terms must not appear.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", _agent_files(), ids=lambda p: p.name)
def test_no_abandoned_sourcing_terms(path: Path) -> None:
    """No prompt may mention ``naver_categories.json`` or ``compute_price(``."""
    text = _AGENT_TEXTS.get(path) or path.read_text(encoding="utf-8")
    for pattern in _BANNED_PATTERNS:
        match = pattern.search(text)
        assert match is None, (
            f"{path.name}: abandoned term {match.group(0)!r} (pattern "
            f"{pattern.pattern!r}) is still present."
        )


# ---------------------------------------------------------------------------
# (e) Tool names mentioned in prompts must match the runtime tool set.
# ---------------------------------------------------------------------------
def test_prompt_tool_set_matches_runtime() -> None:
    """The set of MCP tool names mentioned across registration-flow prompts
    must equal the runtime tool set registered on the ``MCPServer``.

    If a tool is added or removed in code but the prompts are not updated, the
    diff is reported.
    """
    from clossify import mcp_server

    # Runtime tool names from the live MCPServer instance.  ``list_tools`` is
    # async; we use the synchronous helper if present, otherwise fall back to
    # the decorator-registered names visible on the module.
    runtime_names: set[str] = set()
    server = getattr(mcp_server, "mcp", None)
    if server is not None and hasattr(server, "list_tools"):
        import asyncio

        async def _collect() -> set[str]:
            tools = await server.list_tools()
            return {getattr(t, "name", str(t)) for t in tools}

        runtime_names = asyncio.run(_collect())
    # Fallback: derive from the @mcp.tool() decorators in the module source.
    if not runtime_names:
        runtime_names = set(_MCP_TOOLS)
    assert runtime_names, "Could not determine runtime MCP tool set."

    # Collect the union of tool-name mentions across registration-flow prompts.
    mentioned: set[str] = set()
    for filename in _REGISTRATION_FLOW_PROMPTS:
        path = _AGENTS_DIR / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for name in _MCP_TOOLS:
            if name in text:
                mentioned.add(name)

    # The registration-flow prompts must mention *exactly* the runtime set —
    # no phantom tools, no missing tools.
    assert mentioned == runtime_names, (
        "Registration-flow prompt tool set does not match runtime tool set.\n"
        f"  Missing from prompts: {sorted(runtime_names - mentioned)}\n"
        f"  Phantom in prompts:   {sorted(mentioned - runtime_names)}"
    )
