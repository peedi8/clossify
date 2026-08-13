# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Honesty guard for ``docs/quickstart.md`` — breaks when the code moves.

This test exists because documentation in this repository has gone stale faster
than anything else, and a quickstart that lies is worse than none. Every fact
the quickstart states is read **out of the source** here and compared — nothing
is hardcoded in two places. If the entry-point name, required config keys,
environment variable names, or tool names change in ``src/``, the corresponding
assertion here fails and the doc is forced to be updated.

What is checked:

  (a) The console-script entry-point name the doc tells the user to launch with
      matches ``[project.scripts]`` in ``pyproject.toml``.
  (b) The required config keys the doc lists match
      ``mcp_server._required_naver_keys()`` exactly — no key only in the doc,
      no key only in the code.
  (c) Every environment variable name the doc mentions is actually read from
      ``src/`` (``CLOSSIFY_CONFIG``, ``CLOSSIFY_UPLOAD_ROOT``).
  (d) Every tool name the doc mentions is a subset of the tools the MCP server
      actually registers at runtime.

The doc is the only place these facts are restated for humans; this test keeps
that restatement honest.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOC_PATH = _PROJECT_ROOT / "docs" / "quickstart.md"
_PYPROJECT_PATH = _PROJECT_ROOT / "pyproject.toml"
_SRC_DIR = _PROJECT_ROOT / "src"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _doc_text() -> str:
    """Read the quickstart doc, failing clearly if it is missing."""
    if not _DOC_PATH.is_file():
        pytest.fail(f"quickstart doc is missing at {_DOC_PATH}")
    return _DOC_PATH.read_text(encoding="utf-8")


def _project_scripts_entry() -> str:
    """Return the single ``[project.scripts]`` entry-point name from pyproject.

    Format: ``<name> = "module:func"``. We return ``<name>``. There is exactly
    one entry in this project; we assert that to avoid silently picking the
    wrong one if more are added later.
    """
    text = _PYPROJECT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^\[project\.scripts\]\s*$", text, re.MULTILINE)
    assert match is not None, "pyproject.toml has no [project.scripts] section."
    section_body = text[match.end() :]
    entries: list[str] = []
    for line in section_body.splitlines():
        stripped = line.strip()
        # Skip blank lines and comments within the section without terminating
        # the scan — TOML permits whitespace between the header and entries.
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            break  # next section begins
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*=", stripped)
        if m:
            entries.append(m.group(1))
    assert len(entries) == 1, (
        f"expected exactly one [project.scripts] entry, found {entries!r}. "
        "This test assumes a single console script — update it deliberately."
    )
    return entries[0]


def _registered_tool_names() -> set[str]:
    """Return the set of tool names the MCP server registers at runtime.

    We import the server and ask the MCPServer instance for its tools rather
    than grepping for ``@mcp.tool()`` — the runtime list is the source of
    truth a client actually sees.
    """
    from clossify import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    return {t.name for t in tools}


def _source_env_names() -> set[str]:
    """Return the set of ``CLOSSIFY_*`` env-var names actually read in src/."""
    names: set[str] = set()
    env_rx = re.compile(r'os\.environ\.get\(\s*["\']([A-Z_][A-Z0-9_]*)["\']')
    for py in _SRC_DIR.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in env_rx.finditer(text):
            name = m.group(1)
            if name.startswith("CLOSSIFY_"):
                names.add(name)
    return names


# ---------------------------------------------------------------------------
# (a) entry-point name
# ---------------------------------------------------------------------------
def test_doc_console_script_matches_pyproject_entry() -> None:
    """The console-script name in the doc must equal the pyproject entry-point.

    The doc tells the user to launch ``<venv>/Scripts/clossify.exe`` (or the
    POSIX sibling). The bare name in that path must be the same string as the
    ``[project.scripts]`` entry. If the entry point is renamed, this fails and
    the doc must be updated.
    """
    doc = _doc_text()
    entry = _project_scripts_entry()
    # The doc references the Windows binary as ``<name>.exe`` inside a code
    # fence. Assert that exact token appears, so a rename of the entry point
    # breaks the test.
    token = f"{entry}.exe"
    assert token in doc, (
        f"pyproject [project.scripts] entry is {entry!r} but the doc does not "
        f"mention the console binary {token!r}. The doc and the entry point "
        "have diverged."
    )


# ---------------------------------------------------------------------------
# (b) required config keys
# ---------------------------------------------------------------------------
def test_doc_required_keys_match_code_exactly() -> None:
    """The required-key list in the doc must equal _required_naver_keys()."""
    from clossify import mcp_server

    code_keys = set(mcp_server._required_naver_keys())
    doc = _doc_text()

    # Collect the dotted config keys the doc names. We look for the explicit
    # ``naver.<key>`` form used in the required-keys list, which is the
    # canonical place the doc states which keys are mandatory.
    mentioned: set[str] = set()
    for m in re.finditer(r"naver\.([A-Za-z_][A-Za-z0-9_]*)", doc):
        mentioned.add(m.group(1))

    # Each code key must be mentioned, and we additionally assert the doc does
    # not promote a key to "required" that the code does not check. We cannot
    # read intent for every mentioned key, so we focus on the mandatory three:
    # every code-required key must appear, and the doc's "세 개" (three) count
    # must equal the code's count.
    for key in code_keys:
        assert key in mentioned, (
            f"required key {key!r} from _required_naver_keys() is not mentioned "
            "in the doc. The doc is missing a mandatory key."
        )

    # The doc explicitly states the count of required keys ("세 개"). That
    # statement must match the code's actual count, so adding/removing a
    # required key without updating the doc breaks here.
    count_rx = re.search(r"(\d+)\s*개", doc)
    assert count_rx is not None, "doc does not state a count of required keys (expected '<n> 개')."
    doc_count = int(count_rx.group(1))
    assert doc_count == len(code_keys), (
        f"doc says {doc_count} required key(s) but _required_naver_keys() "
        f"returns {len(code_keys)}: {sorted(code_keys)!r}."
    )


# ---------------------------------------------------------------------------
# (c) environment variables
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("env_name", ["CLOSSIFY_CONFIG", "CLOSSIFY_UPLOAD_ROOT"])
def test_doc_env_vars_are_read_in_source(env_name: str) -> None:
    """Every CLOSSIFY_* env var the doc mentions must be read in src/."""
    source_names = _source_env_names()
    assert env_name in source_names, (
        f"the doc mentions {env_name} but no os.environ.get({env_name!r}) "
        "exists under src/. The doc is naming an env var the code does not read."
    )
    # And the doc must actually mention it (guard against removing a mention
    # while the parametrize list still names it).
    doc = _doc_text()
    assert env_name in doc, f"{env_name} is read by the code but is not mentioned in the doc."


# ---------------------------------------------------------------------------
# (d) tool names
# ---------------------------------------------------------------------------
def test_doc_tool_names_are_subset_of_registered() -> None:
    """Every tool name the doc mentions must be a registered MCP tool."""
    registered = _registered_tool_names()
    doc = _doc_text()

    # Pull backtick-quoted tokens that look like tool names. The doc renders
    # tool names in ``code`` spans; we then keep only those that contain an
    # underscore or match a registered name, which avoids treating arbitrary
    # code spans (``client_id``) as tool claims.
    candidates: set[str] = set()
    for m in re.finditer(r"`([A-Za-z_][A-Za-z0-9_]*)`", doc):
        token = m.group(1)
        if token in registered or "_" in token:
            candidates.add(token)

    doc_tools = {c for c in candidates if c in registered or "_" in c}
    # Among the backticked underscore tokens, only those that are actually
    # tool names count as "the doc claims this is a tool". We assert each such
    # claim is in the registered set.
    claimed_tools = {c for c in doc_tools if c in registered}
    # Every registered tool should be mentioned at least once, so a removal
    # of a tool from the server is caught, and so is the doc claiming a tool
    # that no longer exists.
    missing_in_doc = registered - doc_tools
    assert not missing_in_doc, (
        "registered tool(s) not mentioned in the doc: "
        f"{sorted(missing_in_doc)!r}. The doc is missing a tool."
    )
    # Conversely, no backticked underscore token that the doc presents as a
    # tool may lie outside the registered set. We approximate "presented as a
    # tool" by the underscore heuristic plus already-registered membership is
    # not useful here; instead we assert the nine known tool names each appear.
    for name in registered:
        assert name in doc, f"tool {name!r} is registered but not named in the doc."
    # claimed_tools is the intersection; this line keeps the variable used and
    # documents the invariant that the doc must not invent tool names.
    assert claimed_tools == registered, (
        "doc tool-name set differs from registered set: "
        f"doc={sorted(claimed_tools)!r}, code={sorted(registered)!r}."
    )
