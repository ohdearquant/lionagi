# SPDX-License-Identifier: Apache-2.0

"""Compatibility loader that supplies the bundled provider manifest as a catalog.

:func:`default_provider_catalog` is the one automatic source of provider declarations in
1.x. On first use it imports the hub's manifest module, calls its ``get_provider_manifest``,
composes the result with ``compose_provider_catalog`` and keeps the catalog for the life of
the process. This is the only core module that names that manifest module. Importing this
module loads nothing from the hub; the hub is first imported by the first call. A caller
that supplies its own catalog never reaches this module.

The first call has one of these outcomes, and every later call repeats it without trying
again:

* the hub's top-level package is not installed: ``MissingFeatureError`` (feature
  ``providers``, distribution ``lionagi``), because a bundled hub that is missing means the
  installation is incomplete;
* the manifest declares an unsupported contract version: ``ProviderContractVersionError``,
  as the catalog contract raised it;
* any other failure, such as a missing module inside the hub, a syntax error, a manifest
  function that raises or a manifest that cannot be composed: ``ProviderActivationError``
  with the original exception chained as its cause.

The messages are built from fixed fields, so they never carry the text of the original
exception; that text stays reachable through the chained cause. A kept failure is never
raised itself: each call raises a fresh copy of the same type and fields with the same
cause, so repeated calls neither lengthen one traceback nor share one exception object
between threads. An interrupt during the first load (``KeyboardInterrupt``,
``SystemExit``) is not kept, and the next call loads again.
"""

from __future__ import annotations

import copy
import importlib
import threading

from lionagi._feature_errors import MissingFeatureError
from lionagi._provider_errors import ProviderActivationError, ProviderContractVersionError
from lionagi.service.connections.provider_catalog import ProviderCatalog, compose_provider_catalog

__all__ = ("default_provider_catalog",)

_HUB_ROOT = "lionhub"
_MANIFEST_MODULE = "lionhub.providers.manifest"
_FEATURE = "providers"
_REPAIR_HINT = (
    "the bundled provider hub is missing from this installation; "
    "reinstall the matching lionagi release to restore it"
)

_lock = threading.Lock()
_outcome: ProviderCatalog | Exception | None = None


def _activation_failure(cause: Exception) -> ProviderActivationError:
    """Build the activation failure for the whole automatic source, chained to ``cause``.

    No provider is selected at this level, so the provider field carries the feature name.
    """
    error = ProviderActivationError(
        provider=_FEATURE,
        endpoint=None,
        source=_MANIFEST_MODULE,
        reason="activation_failed",
    )
    error.__cause__ = cause
    return error


def _load() -> ProviderCatalog | Exception:
    """Load and compose the manifest once; return the catalog, or the typed failure to keep."""
    try:
        module = importlib.import_module(_MANIFEST_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != _HUB_ROOT:
            # A module missing from inside the hub is a defect, not an absent hub.
            return _activation_failure(exc)
        absent = MissingFeatureError(
            feature=_FEATURE,
            missing_distribution="lionagi",
            install_hint=_REPAIR_HINT,
            source=_MANIFEST_MODULE,
            import_root=_HUB_ROOT,
        )
        absent.__cause__ = exc
        return absent
    except Exception as exc:
        return _activation_failure(exc)
    try:
        return compose_provider_catalog(module.get_provider_manifest())
    except ProviderContractVersionError as exc:
        return exc
    except Exception as exc:
        return _activation_failure(exc)


def default_provider_catalog() -> ProviderCatalog:
    """Return the catalog composed from the bundled provider manifest.

    The first call loads the manifest, thread-safely: concurrent first calls load it once
    and all receive the same catalog object. The catalog, or the failure, is kept, and every
    later call returns that catalog or raises a copy of that failure without importing again.

    Raises:
        MissingFeatureError: The hub's top-level package is not installed.
        ProviderContractVersionError: The manifest declares an unsupported contract version.
        ProviderActivationError: The manifest could not be imported, produced or composed for
            any other reason; the original exception is its ``__cause__``.
    """
    global _outcome
    outcome = _outcome
    if outcome is None:
        with _lock:
            outcome = _outcome
            if outcome is None:
                outcome = _outcome = _load()
    if isinstance(outcome, Exception):
        raise copy.copy(outcome) from outcome.__cause__
    return outcome
