# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The compatibility check an attached application passes before the daemon builds it.

An application attached to the daemon carries a compatibility manifest, ``compatibility.json``,
in the package that defines its attachment. The manifest names the attachment protocol the
application implements and, for each library, the versions it supports::

    {"attachment_protocol": 1, "requires": {"lionagi": ">=1.0.0,<2.0"}}

The daemon factory reads it before the attachment adds any route and before any process resource
starts, and refuses a missing, malformed or incompatible manifest (ADR-0138 F7). Within one wheel
the versions match by construction, so the check catches a stale or partial installation; it
works the same way once the application is installed separately.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from importlib import resources
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

__all__ = (
    "ATTACHMENT_PROTOCOL",
    "MANIFEST_NAME",
    "IncompatibleApplicationError",
    "check_attachment_compatibility",
)

#: The attachment protocol this daemon implements: the ``Attach`` callable and the
#: ``Attachment`` it returns. A manifest must name exactly this version.
ATTACHMENT_PROTOCOL = 1

#: The manifest's file name, in the package that defines the attachment.
MANIFEST_NAME = "compatibility.json"


def _lionagi_version() -> str:
    from lionagi.version import __version__

    return __version__


#: The libraries a manifest may name, each read from the code this process imported, so a
#: version recorded by an installer that the files on disk no longer match is not consulted.
_LIBRARY_VERSIONS: dict[str, Callable[[], str]] = {"lionagi": _lionagi_version}

_REINSTALL = (
    "Install the application and the libraries from one release (for Studio, reinstall "
    "lionagi with its studio extra) so they match."
)


class IncompatibleApplicationError(RuntimeError):
    """The attached application has no valid compatibility manifest, or it does not match."""


def _defining_package(attach: Any) -> str:
    module_name = getattr(attach, "__module__", None)
    module = sys.modules.get(module_name) if isinstance(module_name, str) else None
    if module is None:
        raise IncompatibleApplicationError(
            f"Cannot tell which package defines the attachment {attach!r}, so its compatibility "
            "manifest cannot be read. Attach a function defined at module level in the "
            "application's package."
        )
    package = module.__package__ or ""
    if not package:
        raise IncompatibleApplicationError(
            f"The attachment {attach!r} is defined in the top-level module {module_name!r}, "
            "which has no package to carry a compatibility manifest. Define it in the "
            "application's package."
        )
    return package


def _malformed(where: str, detail: str) -> IncompatibleApplicationError:
    return IncompatibleApplicationError(
        f"The application's compatibility manifest {where} is malformed: {detail}. {_REINSTALL}"
    )


def check_attachment_compatibility(attach: Any) -> None:
    """Refuse ``attach`` unless its package's manifest is present, valid and compatible.

    Reads ``compatibility.json`` from the package whose module defines ``attach``, so the daemon
    names no application package. Raises :class:`IncompatibleApplicationError` with what to do.
    """
    package = _defining_package(attach)
    where = f"{package}/{MANIFEST_NAME}"
    try:
        manifest_file = resources.files(package).joinpath(MANIFEST_NAME)
        present = manifest_file.is_file()
    except (ModuleNotFoundError, TypeError, OSError) as exc:
        raise IncompatibleApplicationError(
            f"The application's compatibility manifest {where} cannot be located ({exc!r}). "
            f"{_REINSTALL}"
        ) from exc
    if not present:
        raise IncompatibleApplicationError(
            f"The application in {package!r} has no compatibility manifest ({where}), so the "
            f"daemon cannot tell whether it supports these libraries. {_REINSTALL}"
        )
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _malformed(where, f"it is not readable JSON ({exc})") from exc
    if not isinstance(manifest, dict):
        raise _malformed(where, "the document is not an object")

    protocol = manifest.get("attachment_protocol")
    # bool is an int subclass; true is not a protocol version.
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise _malformed(where, "attachment_protocol is missing or not an integer")
    requires = manifest.get("requires")
    if not isinstance(requires, dict) or not requires:
        raise _malformed(where, "requires is missing, empty or not an object")

    supported: list[tuple[str, SpecifierSet]] = []
    for library, specifier in requires.items():
        if library not in _LIBRARY_VERSIONS:
            known = ", ".join(sorted(_LIBRARY_VERSIONS))
            raise _malformed(where, f"it names the unknown library {library!r} (known: {known})")
        if not isinstance(specifier, str):
            raise _malformed(where, f"the versions for {library!r} are not a string")
        try:
            supported.append((library, SpecifierSet(specifier)))
        except InvalidSpecifier as exc:
            raise _malformed(
                where, f"the versions for {library!r}, {specifier!r}, do not parse"
            ) from exc

    if protocol != ATTACHMENT_PROTOCOL:
        raise IncompatibleApplicationError(
            f"The application in {package!r} implements attachment protocol {protocol}, and this "
            f"daemon implements protocol {ATTACHMENT_PROTOCOL}. {_REINSTALL}"
        )
    for library, specifier in supported:
        installed = _LIBRARY_VERSIONS[library]()
        try:
            admitted = specifier.contains(Version(installed), prereleases=True)
        except InvalidVersion:
            admitted = False
        if not admitted:
            raise IncompatibleApplicationError(
                f"The application in {package!r} supports {library} {specifier}, and {library} "
                f"{installed} is installed. {_REINSTALL}"
            )
