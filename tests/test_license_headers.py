# SPDX-FileCopyrightText: 2026 3rdhand
# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0
# Providing this software to others is permitted only free of charge and for
# non-commercial purposes. See LICENSE.md.
"""SPDX license-header guard for every ``src/clossify/*.py`` file.

This test enumerates the package source files **dynamically** from the
directory (no hardcoded filenames) and asserts that:

  1. The file set is non-empty (empty-yield guard).
  2. Each file contains the ``SPDX-FileCopyrightText:`` line.
  3. Each file contains the
     ``SPDX-License-Identifier: LicenseRef-SustainableUse-1.0`` line.
  4. The 4-line SPDX header block is byte-identical across every file
     (compared against one reference file).

Adding a new ``.py`` file under ``src/clossify/`` without the standard
header will fail this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The package source directory, resolved relative to this test file:
#   tests/test_license_headers.py -> project root -> src/clossify
_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "clossify"

# The exact 4-line SPDX header every source file must carry verbatim.
_EXPECTED_HEADER_LINES = (
    "# SPDX-FileCopyrightText: 2026 3rdhand\n",
    "# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0\n",
    "# Providing this software to others is permitted only free of charge and for\n",
    "# non-commercial purposes. See LICENSE.md.\n",
)


def _source_files() -> list[Path]:
    """Return all ``*.py`` files under ``src/clossify/`` (dynamic, no hardcoding)."""
    return sorted(_SRC_DIR.glob("*.py"))


def test_source_directory_is_non_empty() -> None:
    """Empty-yield guard: the package directory must contain at least one file."""
    files = _source_files()
    assert len(files) >= 1, (
        "src/clossify/ yielded no .py files — the glob is broken or the " "package directory moved."
    )


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_each_file_has_spdx_copyright_line(path: Path) -> None:
    """Every source file must contain the ``SPDX-FileCopyrightText:`` line."""
    text = path.read_text(encoding="utf-8")
    assert any(
        line.startswith("# SPDX-FileCopyrightText:") for line in text.splitlines()
    ), f"{path.name} is missing the 'SPDX-FileCopyrightText:' header line."


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_each_file_has_spdx_license_identifier_line(path: Path) -> None:
    """Every source file must contain the LicenseRef-SustainableUse-1.0 identifier line."""
    expected = "# SPDX-License-Identifier: LicenseRef-SustainableUse-1.0"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert any(
        line.strip() == expected for line in lines
    ), f"{path.name} is missing the '{expected}' header line."


def test_header_block_identical_across_all_files() -> None:
    """The 4-line SPDX header block must be byte-identical across every file."""
    files = _source_files()
    assert len(files) >= 1, "src/clossify/ yielded no .py files."

    reference: tuple[str, ...] | None = None
    for path in files:
        with path.open(encoding="utf-8") as fh:
            header = tuple(fh.readline() for _ in range(4))
        if reference is None:
            reference = header
            continue
        assert header == reference, (
            f"{path.name} header block differs from {files[0].name}.\n"
            f"Expected: {reference!r}\nGot:      {header!r}"
        )
    # Sanity: the reference must equal the expected constant.
    assert reference == _EXPECTED_HEADER_LINES, (
        f"Header block does not match the expected 4-line SPDX constant.\n"
        f"Expected: {_EXPECTED_HEADER_LINES!r}\nGot:      {reference!r}"
    )
