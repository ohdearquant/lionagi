# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression: public import surface of lionagi.cli.orchestrate is preserved
after the spec-validation helper split.

``validate_path_component`` was imported at module level in
``lionagi/cli/orchestrate/__init__.py`` on main, making it importable via
``from lionagi.cli.orchestrate import validate_path_component``.  The
module-split refactor inadvertently moved that import inside
``_resolve_playbook_path`` in ``_spec.py``, dropping it from the public
surface.  This test ensures it stays re-exported at module level.
"""

from __future__ import annotations


def test_validate_path_component_importable_from_orchestrate_package() -> None:
    """``from lionagi.cli.orchestrate import validate_path_component`` must succeed."""
    from lionagi.cli.orchestrate import validate_path_component  # noqa: F401 — import is the test

    assert callable(validate_path_component), "validate_path_component must be a callable function"


def test_validate_path_component_is_the_canonical_function() -> None:
    """The re-export must resolve to the same object as the canonical location."""
    from lionagi.cli.orchestrate import validate_path_component as from_orchestrate
    from lionagi.libs.path_safety import validate_path_component as canonical

    assert from_orchestrate is canonical, (
        "lionagi.cli.orchestrate.validate_path_component must be the same object "
        "as lionagi.libs.path_safety.validate_path_component"
    )
