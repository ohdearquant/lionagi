# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Kept at its old path so the old Operator daemon path imports without the Studio application.

``lionagi.studio.operator.redact`` moved to the hub and is imported through this package, so the
package stays here and imports nothing. The application's Operator package moved with the rest of
the application; the three names it exports are forwarded from it, with one moved-package
deprecation warning, until the move's removal release.
"""

_FORWARDED = frozenset(
    {"OperatorCoordinator", "get_operator_coordinator", "reset_operator_coordinator_for_testing"}
)


def __getattr__(name: str):
    if name not in _FORWARDED:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    import warnings

    from lionagi._moved import _row, _warning_origin

    row = _row(__name__)
    new_name = row.new + __name__[len(row.old) :]
    # Imported first, so an application that is not installed raises without a warning.
    value = getattr(importlib.import_module(new_name), name)
    # One moved package, so one warning: only the first name read warns. Each name is kept
    # here once read, because ``from ... import`` reads a name twice.
    if _FORWARDED.isdisjoint(globals()):
        stacklevel, _ = _warning_origin()
        warnings.warn(
            f"{__name__} moved to {new_name}; the old import path is removed in lionagi "
            f"{row.removed_in}. Import from {new_name} instead.",
            DeprecationWarning,
            stacklevel=stacklevel,
        )
    globals()[name] = value
    return value
