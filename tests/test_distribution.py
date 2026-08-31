# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""Distribution integrity test — proves the install-path relocation fix.

The core problem this test guards against: modules computed "repo root" as
``os.path.dirname(__file__)/../..``, which works in the source tree but
breaks when installed via wheel (points inside ``site-packages/Lib/``).
The fix separates package data (``importlib.resources``) from user-space
state (``CLOSSIFY_STATE_DIR`` or ``<cwd>/.local``).

This test inspects **real built artifacts** (a wheel built in a temp dir)
rather than grepping packaging config strings. It asserts:

  (a) Building a wheel succeeds (hatchling can package the project).
  (b) Every ``data/*.json`` data asset is present inside the wheel under
      ``clossify/data/``.
  (c) Every ``agents/*.md`` prompt asset is present inside the wheel under
      ``clossify/agents/``.
  (d) ``config.example.json`` is present inside the wheel.
  (e) No ``__file__``-based repo-root computation (``parents[2]`` or
      ``os.path.join(os.path.dirname(__file__), "..", "..")``) remains in
      the shipped ``*.py`` files inside the wheel.
  (f) No ``ROOT_DIR`` symbol remains in the shipped source.
  (g) At runtime, ``common.package_data_path`` resolves data files via
      ``importlib.resources`` and the files are readable.
  (h) At runtime, ``common.STATE_DIR`` derives from ``CLOSSIFY_STATE_DIR``
      or cwd, not from a ``__file__``-based root.
  (i) every packaged ``agents/*.md`` prompt asset is **actually
      loadable** at runtime via ``copywriting._agent_rules_bundle`` (no
      mocks — the real file is read). This catches the regression where
      the loader path pointed inside the package but the assets lived at
      repo root, so 912 passing tests (which mock the LLM path) never
      exercised the real prompt read.
  (j) no ``agents/`` directory remains at the repository root
      (single source of truth — the package-internal copy is the only one).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

#: Data assets that must ship inside the wheel under ``clossify/data/``.
_REQUIRED_DATA_FILES = {
    "category_meta.json",
    "category_requirements.json",
    "certification_types.json",
    "notice_field_labels.json",
    "notice_field_relations.json",
    "notice_field_types.json",
    "notice_types.json",
}


def _build_wheel(tmp_path: Path) -> Path:
    """Build a wheel into *tmp_path* and return the wheel file path.

    Uses build isolation (default pip behaviour) so the build backend
    (hatchling) is installed into a throwaway environment — this mirrors
    what real users experience when they ``pip install`` the project.
    """
    # Use the project root (tests/ -> parent).
    project_root = Path(__file__).resolve().parent.parent
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        "--no-deps",
        "-w",
        str(tmp_path),
        str(project_root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, (
        f"pip wheel failed (exit {result.returncode}):\n"
        f"STDOUT:\n{result.stdout[-2000:]}\nSTDERR:\n{result.stderr[-2000:]}"
    )
    wheels = list(tmp_path.glob("clossify-*.whl"))
    assert wheels, f"No wheel produced in {tmp_path}"
    assert len(wheels) == 1, f"Multiple wheels found: {wheels}"
    return wheels[0]


def _can_build_wheel() -> bool:
    """Quick check whether wheel building is available in this environment."""
    try:
        import importlib.util

        # Check if the ``build`` package or ``hatchling`` is available, or
        # if pip can do build isolation (network access). We do a lightweight
        # check: if hatchling is importable or if we're in a CI environment
        # (which has network), allow the build.
        if importlib.util.find_spec("hatchling") is not None:
            return True
        # In environments without hatchling, pip will use build isolation
        # (downloading hatchling into a temp env). Allow this in CI or when
        # network is available, but skip in offline test runs to avoid
        # hanging on network timeouts.
        return bool(os.environ.get("CI") or os.environ.get("CLOSSIFY_RUN_WHEEL_TESTS"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# (a) Building a wheel succeeds.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    """Build the wheel once for the module; reuse across tests.

    Skipped when hatchling is not available and the environment is offline
    (no ``CI`` or ``CLOSSIFY_RUN_WHEEL_TESTS`` env var). The runtime tests
    (g, h) always run regardless.
    """
    if not _can_build_wheel():
        pytest.skip(
            "Wheel build tests require hatchling (installed or network-available). "
            "Set CLOSSIFY_RUN_WHEEL_TESTS=1 or run in CI to enable."
        )
    tmp = tmp_path_factory.mktemp("wheel_build")
    return _build_wheel(tmp)


def test_a_wheel_builds_successfully(built_wheel: Path) -> None:
    """The project builds into a wheel without errors."""
    assert built_wheel.exists(), f"Wheel file not found: {built_wheel}"
    assert built_wheel.suffix == ".whl"


# ---------------------------------------------------------------------------
# (b) Every data/*.json asset is inside the wheel under clossify/data/.
# ---------------------------------------------------------------------------


def test_b_data_assets_in_wheel(built_wheel: Path) -> None:
    """All required ``data/*.json`` files are packaged under ``clossify/data/``."""
    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    for filename in sorted(_REQUIRED_DATA_FILES):
        wheel_path = f"clossify/data/{filename}"
        assert wheel_path in names, (
            f"Data asset {filename!r} is missing from the wheel "
            f"(expected at {wheel_path!r}). Wheel contents sample: "
            f"{[n for n in names if 'data' in n][:10]}"
        )


def test_b_dummy_image_asset_in_wheel(built_wheel: Path) -> None:
    """``dummy_main_image.png`` is packaged under ``clossify/data/`` in the wheel.

    This guards against the external-service dependency regression: the dummy
    main image was previously a ``placehold.co`` URL. It is now a packaged PNG
    asset that must ship inside the wheel.
    """
    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    wheel_path = "clossify/data/dummy_main_image.png"
    assert wheel_path in names, (
        f"dummy_main_image.png is missing from the wheel (expected at {wheel_path!r}). "
        f"Wheel data contents: {sorted(n for n in names if 'data/' in n)}"
    )


# ---------------------------------------------------------------------------
# (c) Every agents/*.md prompt asset is inside the wheel under clossify/agents/.
# ---------------------------------------------------------------------------


def test_c_agents_assets_in_wheel(built_wheel: Path) -> None:
    """All ``agents/*.md`` prompt files are packaged under ``clossify/agents/``.

    Source enumeration happens against ``common.AGENTS_DIR`` (the package
    asset directory inside ``src/clossify/agents/``), not against the repo
    root. The asset relocation moved the assets there; the loader reads them from there
    too, so the source-of-truth on disk must match what the wheel contains.
    """
    from clossify import common

    source_agents = sorted(common.AGENTS_DIR.glob("*.md"))
    assert source_agents, (
        f"No agents/*.md found in package asset dir {common.AGENTS_DIR!r} — "
        "test is broken or assets are not in place."
    )
    source_names = {p.name for p in source_agents}

    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    wheel_agent_names = {
        Path(n).name for n in names if n.startswith("clossify/agents/") and n.endswith(".md")
    }
    missing = source_names - wheel_agent_names
    assert not missing, (
        f"Agent prompt files missing from wheel: {sorted(missing)}. "
        f"Wheel agent files: {sorted(wheel_agent_names)}"
    )


# ---------------------------------------------------------------------------
# (d) config.example.json is inside the wheel.
# ---------------------------------------------------------------------------


def test_d_config_example_in_wheel(built_wheel: Path) -> None:
    """``config.example.json`` is packaged at ``clossify/config.example.json``."""
    with zipfile.ZipFile(built_wheel) as zf:
        names = zf.namelist()
    assert "clossify/config.example.json" in names, (
        "config.example.json is missing from the wheel "
        "(expected at clossify/config.example.json)."
    )


# ---------------------------------------------------------------------------
# (e) No __file__-based repo-root computation in shipped source.
# ---------------------------------------------------------------------------

#: Patterns that indicate ``__file__``-based repo-root estimation.
_FILE_ROOT_PATTERNS = [
    # Path(__file__).resolve().parents[2] (or parents[1] in some layouts).
    re.compile(r"parents\[\s*2\s*\]"),
    re.compile(r"parents\[\s*1\s*\]"),
    # os.path.join(os.path.dirname(__file__), "..", "..")
    re.compile(r"os\.path\.dirname\s*\(\s*__file__\s*\)"),
    # os.path.normpath(os.path.join(os.path.dirname(__file__)))
    re.compile(r"os\.path\.normpath\s*\(\s*os\.path\.join\s*\(\s*os\.path\.dirname"),
]


def test_e_no_file_based_root_in_shipped_source(built_wheel: Path) -> None:
    """No shipped ``*.py`` file uses ``__file__``-based repo-root computation."""
    offenders: list[str] = []
    with zipfile.ZipFile(built_wheel) as zf:
        py_names = [n for n in zf.namelist() if n.startswith("clossify/") and n.endswith(".py")]
        for name in py_names:
            source = zf.read(name).decode("utf-8", errors="replace")
            for pattern in _FILE_ROOT_PATTERNS:
                if pattern.search(source):
                    offenders.append(f"{name}: pattern {pattern.pattern!r}")
    assert not offenders, (
        "__file__-based repo-root computation found in shipped source "
        "(must use importlib.resources instead):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# (f) No ROOT_DIR symbol in shipped source.
# ---------------------------------------------------------------------------


def test_f_no_root_dir_symbol_in_shipped_source(built_wheel: Path) -> None:
    """No shipped ``*.py`` file defines or references ``ROOT_DIR``."""
    offenders: list[str] = []
    with zipfile.ZipFile(built_wheel) as zf:
        py_names = [n for n in zf.namelist() if n.startswith("clossify/") and n.endswith(".py")]
        for name in py_names:
            source = zf.read(name).decode("utf-8", errors="replace")
            # Match ROOT_DIR as a whole word (not ROOT_DIRECTORY or similar).
            if re.search(r"\bROOT_DIR\b", source):
                offenders.append(name)
    assert not offenders, (
        "ROOT_DIR symbol found in shipped source (should be removed):\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# (g) Runtime: package_data_path resolves data files via importlib.resources.
# ---------------------------------------------------------------------------


def test_g_package_data_path_resolves_at_runtime() -> None:
    """``common.package_data_path`` resolves data files and they are readable."""
    from clossify import common

    for filename in sorted(_REQUIRED_DATA_FILES):
        path = common.package_data_path(filename)
        assert path.exists(), (
            f"package_data_path({filename!r}) = {path} does not exist at runtime. "
            f"DATA_DIR = {common.DATA_DIR}"
        )
        # File must be readable and non-empty.
        size = path.stat().st_size
        assert size > 0, f"package_data_path({filename!r}) resolved to an empty file: {path}"


# ---------------------------------------------------------------------------
# (h) Runtime: STATE_DIR derives from CLOSSIFY_STATE_DIR or cwd.
# ---------------------------------------------------------------------------


def test_h_state_dir_respects_env(monkeypatch, tmp_path) -> None:
    """``_state_dir`` honours ``CLOSSIFY_STATE_DIR`` and defaults to cwd."""
    from clossify import common

    # Override path.
    custom = tmp_path / "custom_state"
    custom.mkdir()
    monkeypatch.setenv("CLOSSIFY_STATE_DIR", str(custom))
    result = common._state_dir()
    assert result == custom.resolve(), (
        f"_state_dir() with CLOSSIFY_STATE_DIR={custom!r} returned {result!r}, "
        f"expected {custom.resolve()!r}"
    )

    # Default path (cwd-based) — env unset.
    monkeypatch.delenv("CLOSSIFY_STATE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    result = common._state_dir()
    expected = tmp_path / ".local"
    assert result == expected, (
        f"_state_dir() without override returned {result!r}, " f"expected {expected!r} (cwd/.local)"
    )


def test_h_state_dir_not_file_based() -> None:
    """STATE_DIR is a Path under cwd/.local (or CLOSSIFY_STATE_DIR), not under site-packages."""
    from clossify import common

    # STATE_DIR must not point inside site-packages or the package directory.
    state_str = str(common.STATE_DIR)
    pkg_str = str(common._PKG_DIR)
    assert not state_str.startswith(pkg_str), (
        f"STATE_DIR ({state_str!r}) points inside the package directory "
        f"({pkg_str!r}) — it should be in user space (cwd/.local)."
    )
    # Must contain ".local" (the default state directory name).
    assert ".local" in state_str, (
        f"STATE_DIR ({state_str!r}) does not contain '.local' — "
        f"the default state directory name is missing."
    )


# ---------------------------------------------------------------------------
# (i) every agents/*.md prompt is actually loadable (NO MOCKS).
#
# This is the test that would have caught the install-paths regression: the loader
# pointed inside the package, but the assets lived at repo root. Every other
# test mocked the LLM path so the real prompt read never happened. Here we
# really read every packaged agent markdown file through the loader and
# assert it is non-empty. File list is enumerated from the package asset
# directory (NOT hardcoded), so any asset that fails to load fails the test.
# ---------------------------------------------------------------------------


def test_i_agent_rules_bundles_load_for_real() -> None:
    """Every packaged ``agents/*.md`` is loadable via ``_agent_rules_bundle``.

    No mocks: the real file is read on disk. Catches the regression where
    the loader resolves to the package-internal path but the file is
    physically elsewhere (which all 912 mock-based tests missed).
    """
    from clossify import common
    from clossify.copywriting import _agent_rules_bundle

    agent_files = sorted(p.name for p in common.AGENTS_DIR.glob("*.md"))
    assert agent_files, (
        f"No agents/*.md found in package asset dir {common.AGENTS_DIR!r} — "
        "the directory is empty or missing. This is the install-paths regression: "
        "loader resolves inside the package but the assets are not there."
    )

    failures: list[str] = []
    loaded: dict[str, int] = {}
    for filename in agent_files:
        try:
            text = _agent_rules_bundle(filename)
        except Exception as exc:  # collect every failure, report at end
            failures.append(f"{filename}: {type(exc).__name__}: {exc}")
            continue
        if not text or not str(text).strip():
            failures.append(f"{filename}: loaded but empty")
            continue
        loaded[filename] = len(text)

    # Sanity: we expect the canonical set of 9 prompt files. We do not
    # hardcode the names in the assertion (the test enumerates the dir),
    # but if the count drifts we want to know — that signals someone added
    # or removed a prompt without updating loaders/consumers.
    assert len(agent_files) >= 9, (
        f"Expected at least 9 agent prompt files, found {len(agent_files)}: " f"{agent_files}"
    )

    assert not failures, (
        "Some agents/*.md failed to load through the real loader (no mocks). "
        "This is the install-paths regression signature.\n  " + "\n  ".join(failures)
    )
    # Every loaded bundle must be non-trivial (>100 chars — these are prompt
    # documents, not stubs). Guards against accidental truncation.
    stubs = [name for name, size in loaded.items() if size <= 100]
    assert not stubs, f"These agent bundles loaded but look like stubs (<=100 chars): {stubs}"


# ---------------------------------------------------------------------------
# (j) no agents/ directory remains at the repository root.
# ---------------------------------------------------------------------------


def test_j_no_root_agents_directory() -> None:
    """The repo root must NOT contain an ``agents/`` directory.

    The asset relocation moved the assets to ``src/clossify/agents/``. Leaving a stale
    copy at the root would create two sources of truth (the package copy
    the loader reads, and the root copy nothing reads) and silently mask
    future regressions in the package-internal location.
    """
    project_root = Path(__file__).resolve().parent.parent
    root_agents = project_root / "agents"
    assert not root_agents.is_dir(), (
        f"Root {root_agents!r} still exists. agents/ must live only inside "
        "src/clossify/agents/ (single source of truth). Remove the root copy."
    )
