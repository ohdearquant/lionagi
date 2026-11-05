# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The shared mirror settings stay independent of application services."""

from __future__ import annotations


def test_mirror_settings_imports_only_the_standard_library() -> None:
    """The settings module is read by the studio config at import time, so it imports nothing else."""
    import sys
    from pathlib import Path

    import lionhub.serve
    from tests.cli._import_guard import resolved_imports

    source = Path(lionhub.serve.__file__).resolve().parent / "mirror_settings.py"
    imported = resolved_imports(source, "lionhub.serve")
    assert imported
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in imported)
