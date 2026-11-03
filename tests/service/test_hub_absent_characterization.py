# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Provider discovery when the hub package cannot be imported.

Each check runs in a fresh interpreter whose import system refuses ``lionhub``
and every module under it, which is what an incomplete installation of the
framework looks like. The checks record what the code does today, so a later
change to the discovery contract shows up as a deliberate edit to a pinned
value. Nothing here calls a model or a network service: the model constructor
is exercised with a placeholder key that exists only in the child environment.

The comment above each pinned value says what it pins and where ADR-0140
(provider discovery contract) and ADR-0136 Amendment 3 change it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests._core_imports import ROOT

# Installed first on the child's import path: any import of the hub root or of a
# module below it fails the way a missing distribution does.
_HUB_BLOCKED = """
import importlib.abc
import json
import sys

sys.path.insert(0, sys.argv[1])


class _HubBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lionhub" or fullname.startswith("lionhub."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, _HubBlocker())
"""

_REGISTRY_LOGGER = "lionagi.service.connections.registry"


def _run_blocked(body: str, tmp_path: Path) -> dict[str, Any]:
    """Run ``body`` in a fresh interpreter with the hub blocked; return its JSON report."""
    env = {k: v for k, v in os.environ.items() if k != "LIONAGI_CHAT_PROVIDER"}
    # The registry-miss path consults plugins under the user home; keep it empty.
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env["OPENAI_API_KEY"] = "placeholder-value"
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script
        [sys.executable, "-c", _HUB_BLOCKED + body, str(ROOT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_import_lionagi_without_hub_loads_no_hub_module(tmp_path):
    report = _run_blocked(
        """
import lionagi

print(json.dumps({
    "core_loaded": "lionagi" in sys.modules,
    "hub_modules": sorted(
        name for name in sys.modules if name == "lionhub" or name.startswith("lionhub.")
    ),
}))
""",
        tmp_path,
    )

    # Pins: the package imports with the hub blocked and loads no hub module.
    # ADR-0140 D1 keeps this: ordinary ``import lionagi`` must still not load
    # the hub once discovery reads a manifest.
    assert report == {"core_loaded": True, "hub_modules": []}


def test_cold_registry_load_without_hub_logs_one_catalog_failure(tmp_path):
    report = _run_blocked(
        """
import logging

from lionagi.service.connections.registry import EndpointRegistry

records = []


class _Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


logger = logging.getLogger("lionagi.service.connections.registry")
logger.setLevel(logging.DEBUG)
logger.addHandler(_Capture(level=logging.WARNING))

cold_before = EndpointRegistry._loaded
raised = None
try:
    EndpointRegistry._ensure_loaded()
except BaseException as exc:
    raised = type(exc).__name__
providers = EndpointRegistry.list_providers()
print(json.dumps({
    "cold_before": cold_before,
    "raised": raised,
    "logger_names": sorted({r.name for r in records}),
    "levels": sorted({r.levelname for r in records}),
    "messages": [r.getMessage() for r in records],
    "providers": [[p["provider"], p["endpoint"]] for p in providers],
}))
""",
        tmp_path,
    )

    # Pins: a fresh process has not loaded providers yet. ADR-0140 D1 keeps
    # loading lazy (first provider or context-window request); only the flag
    # that records it can change.
    assert report["cold_before"] is False
    # Pins: an absent hub does not make the loader raise. This loader serves
    # the full listing; the typed error (MissingFeatureError, feature=providers)
    # is raised when a provider is selected, as the next test shows.
    assert report["raised"] is None
    # Pins: the warning comes from the registry module's logger; its name does
    # not change.
    assert report["logger_names"] == [_REGISTRY_LOGGER]
    assert report["levels"] == ["WARNING"]
    # Pins: the catalog failure is logged once, naming the error, instead of one
    # warning per provider module.
    assert len(report["messages"]) == 1
    assert report["messages"][0].startswith(
        "built-in providers were not registered: "
        "the provider catalog could not load: MissingFeatureError"
    )
    # Pins: after the load, only the scripted test provider is registered. It
    # stays a core module that the loader imports directly; a later change
    # turns it into an explicitly selected test fragment.
    assert report["providers"] == [["scripted", "chat/completions"]]


def test_openai_model_without_hub_raises_missing_feature_error(tmp_path):
    report = _run_blocked(
        """
from lionagi.service.imodel import iModel

try:
    model = iModel(provider="openai", model="gpt-4o-mini")
except BaseException as exc:
    out = {
        "constructed": False,
        "error_type": type(exc).__name__,
        "is_import_error": isinstance(exc, ImportError),
        "feature": getattr(exc, "feature", None),
        "provider": getattr(exc, "provider", None),
        "distribution": getattr(exc, "missing_distribution", None),
        "hint": getattr(exc, "install_hint", None),
        "name": getattr(exc, "name", None),
    }
else:
    out = {"constructed": True, "endpoint_class": type(model.endpoint).__name__}
print(json.dumps(out))
""",
        tmp_path,
    )

    # Pins the ADR-0140 D4 row "Hub physically absent from a 1.x installation": with
    # the hub absent the model is not built, and the failure is a MissingFeatureError
    # (an ImportError) for feature ``providers``, naming the requested provider and
    # the missing distribution ``lionagi``, with guidance to repair that
    # installation. It used to be the ProviderNotFoundError an unknown provider name
    # gets, which a missing hub could not be told apart from.
    assert report["constructed"] is False, report
    assert report["error_type"] == "MissingFeatureError", report
    assert report["is_import_error"] is True, report
    assert report["feature"] == "providers", report
    assert report["provider"] == "openai", report
    assert report["distribution"] == "lionagi", report
    assert report["name"] == "lionhub", report
    assert "reinstall the matching lionagi release" in report["hint"], report


def test_selection_without_hub_is_not_masked_and_core_providers_still_resolve(tmp_path):
    report = _run_blocked(
        """
import logging
import warnings

from lionagi._feature_errors import MissingFeatureError
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import ProviderNotFoundError

records = []


class _Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


logging.getLogger("lionagi").addHandler(_Capture(level=logging.WARNING))

out = {"attempts": []}
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    for provider, extra in (
        ("openai", {}),
        ("some-other-host", {}),
        ("some-other-host", {"openai_compatible": True}),
        ("some-other-host", {"base_url": "https://llm.example.test/v1"}),
        ("openai", {}),
    ):
        try:
            endpoint = match_endpoint(provider=provider, endpoint="chat/completions", **extra)
        except MissingFeatureError as exc:
            out["attempts"].append(
                [
                    type(exc).__name__,
                    exc.provider,
                    exc.missing_distribution,
                    exc.feature,
                    exc.install_hint,
                ]
            )
        except ProviderNotFoundError as exc:
            out["attempts"].append(["ProviderNotFoundError", provider, None])
        else:
            out["attempts"].append(["selected", provider, type(endpoint).__name__])
out["deprecations"] = [warning.category.__name__ for warning in caught]
out["warnings"] = [record.getMessage() for record in records]
scripted = match_endpoint(provider="scripted", endpoint="")
out["scripted"] = type(scripted).__name__
print(json.dumps(out))
""",
        tmp_path,
    )

    # Pins the same D4 row for the generic routes: neither openai_compatible= nor
    # base_url= turns the missing hub into a generic endpoint, because a 1.x installation
    # without the hub is incomplete. A provider the catalog cannot be consulted about gets
    # the same error, with its feature, the missing distribution and the guidance to
    # reinstall, and no routing warning is issued for a request that fails. Repeated
    # selection raises again without logging a warning. The core scripted provider still
    # resolves.
    assert [row[:4] for row in report["attempts"]] == [
        ["MissingFeatureError", "openai", "lionagi", "providers"],
        ["MissingFeatureError", "some-other-host", "lionagi", "providers"],
        ["MissingFeatureError", "some-other-host", "lionagi", "providers"],
        ["MissingFeatureError", "some-other-host", "lionagi", "providers"],
        ["MissingFeatureError", "openai", "lionagi", "providers"],
    ], report
    assert all("reinstall the matching lionagi release" in row[4] for row in report["attempts"]), (
        report
    )
    assert report["deprecations"] == [], report
    assert report["warnings"] == [], report
    assert report["scripted"] == "ScriptedEndpoint", report


def test_context_window_without_hub_raises_missing_feature_error(tmp_path):
    report = _run_blocked(
        """
from lionagi.service.token_budget import lookup_context_window

out = {}
for model in ("gpt-4o-mini", "gpt-4.1"):
    try:
        out[model] = lookup_context_window(model, "openai")
    except Exception as exc:
        out[model] = type(exc).__name__
print(json.dumps(out))
""",
        tmp_path,
    )

    # With the provider hub unreachable the lookup raises the loader's typed error rather
    # than answering with the 128,000 estimate. The first model's table value is also the
    # estimate, so it could not tell a table hit from the fallback; the second model has a
    # larger window in the hub's table, so a returned number would show a fallback.
    assert report == {"gpt-4o-mini": "MissingFeatureError", "gpt-4.1": "MissingFeatureError"}


def test_old_provider_import_path_without_hub_raises_module_not_found(tmp_path):
    report = _run_blocked(
        """
import importlib

try:
    importlib.import_module("lionagi.providers.openai")
except BaseException as exc:
    out = {"error_type": type(exc).__name__, "missing_name": getattr(exc, "name", None)}
else:
    out = {"error_type": None, "missing_name": None}
print(json.dumps(out))
""",
        tmp_path,
    )

    # Pins: the old import path is served by forwarding to the hub, so with the
    # hub blocked the import fails with the missing-module error of the hub
    # root. ADR-0140 D4 puts its typed error on provider selection and gives
    # module imports no row, so this result is not expected to change with it.
    assert report == {"error_type": "ModuleNotFoundError", "missing_name": "lionhub"}
