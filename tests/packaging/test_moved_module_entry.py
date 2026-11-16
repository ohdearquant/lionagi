# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0136 Amendment 5, item G5: a moved module entry warns once from the command line."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_FINDER = Path(__file__).resolve().parents[2] / "lionagi" / "_moved.py"
_INSTALL_FINDER = """
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

spec = spec_from_file_location("_entry_test_moved", {finder!r})
moved = module_from_spec(spec)
spec.loader.exec_module(moved)
moved._LIONAGI_DIR = str(Path(__file__).parent)
moved.MOVED = (moved.Moved("legacy.application", "current_application", "1.0.0", "2.0.0"),)
moved.install()
"""


@pytest.mark.parametrize("parent_layout", ["kept", "aliased", "renamed-kept"])
@pytest.mark.parametrize("entry", ["__main__", "child"])
@pytest.mark.parametrize(
    "warning_args", [(), ("-W", "always::DeprecationWarning")], ids=["default", "always"]
)
def test_moved_module_entry_warns_once(
    tmp_path: Path, parent_layout: str, entry: str, warning_args: tuple[str, ...]
) -> None:
    old_package = tmp_path / "legacy"
    old_package.mkdir()
    (old_package / "__init__.py").write_text(
        _INSTALL_FINDER.format(finder=str(_FINDER)), encoding="utf-8"
    )
    kept_parent = parent_layout != "aliased"
    if kept_parent:
        old_parent = old_package / "application"
        old_parent.mkdir()
        # A matching module name alone does not make this the canonical object.
        source = '__name__ = "current_application"\n' if parent_layout == "renamed-kept" else ""
        (old_parent / "__init__.py").write_text(source, encoding="utf-8")

    new_package = tmp_path / "current_application"
    new_package.mkdir()
    (new_package / "__init__.py").touch()
    (new_package / f"{entry}.py").write_text(
        f'if __name__ == "__main__":\n    print("ran canonical {entry}")\n', encoding="utf-8"
    )

    child_suffix = "" if entry == "__main__" else f".{entry}"
    result = subprocess.run(  # noqa: S603 -- this interpreter and a synthetic module
        [
            sys.executable,
            "-B",
            "-E",
            "-S",
            *warning_args,
            "-m",
            f"legacy.application{child_suffix}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"ran canonical {entry}\n"
    warnings = [line for line in result.stderr.splitlines() if "DeprecationWarning:" in line]
    assert len(warnings) == 1, (len(warnings), result.stderr)
    assert warnings[0].startswith(
        f"<command line>:1: DeprecationWarning: legacy.application.{entry} "
        f"moved to current_application.{entry}; the old import path is removed in lionagi 2.0.0."
    )
