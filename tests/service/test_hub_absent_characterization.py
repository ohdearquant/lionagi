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


def test_cold_registry_load_without_hub_logs_each_failed_module(tmp_path):
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
    "warned_modules": [r.args[0] for r in records],
    "failure_texts": sorted({str(r.args[1]) for r in records}),
    "providers": [[p["provider"], p["endpoint"]] for p in providers],
}))
""",
        tmp_path,
    )

    # Pins: a fresh process has not loaded providers yet. ADR-0140 D1 keeps
    # loading lazy (first provider or context-window request); only the flag
    # that records it can change.
    assert report["cold_before"] is False
    # Pins: an absent hub does not make the loader raise; each failure is logged
    # and skipped. ADR-0140 D4 and ADR-0136 Amendment 3 C3 turn this into a typed
    # error at provider selection (MissingFeatureError, feature=providers), and
    # D1 removes this loader.
    assert report["raised"] is None
    # Pins: the warnings come from the registry module's logger. ADR-0136
    # Amendment 3 C3 forbids changing logger names in the package move; the
    # warnings themselves go with the loader (ADR-0140 D1).
    assert report["logger_names"] == [_REGISTRY_LOGGER]
    assert report["levels"] == ["WARNING"]
    # Pins: every hub module in the loader's fixed list is named once, in list
    # order, and the one core module in that list is not named. ADR-0140 D1
    # deletes the fixed list and with it this per-module warning pile.
    assert report["warned_modules"] == [
        "lionhub.providers.openai.chat",
        "lionhub.providers.openai.codex",
        "lionhub.providers.openai.audio",
        "lionhub.providers.openai.batch",
        "lionhub.providers.openai.images",
        "lionhub.providers.openai.embed",
        "lionhub.providers.openai.response",
        "lionhub.providers.anthropic.messages",
        "lionhub.providers.anthropic.claude_code",
        "lionhub.providers.ollama.chat",
        "lionhub.providers.ollama.embed",
        "lionhub.providers.ollama.generate",
        "lionhub.providers.lattice.chat",
        "lionhub.providers.lattice.embed",
        "lionhub.providers.tavily.search",
        "lionhub.providers.exa.search",
        "lionhub.providers.exa.contents",
        "lionhub.providers.exa.find_similar",
        "lionhub.providers.firecrawl.scrape",
        "lionhub.providers.firecrawl.map",
        "lionhub.providers.firecrawl.crawl",
        "lionhub.providers.outcrawl.scrape",
        "lionhub.providers.outcrawl.search",
        "lionhub.providers.perplexity.chat",
        "lionhub.providers.nvidia_nim.chat",
        "lionhub.providers.nvidia_nim.embed",
        "lionhub.providers.deepseek.chat",
        "lionhub.providers.google.chat",
        "lionhub.providers.google.gemini_code",
        "lionhub.providers.groq.chat",
        "lionhub.providers.groq.audio_transcription",
        "lionhub.providers.pi.cli",
        "lionhub.providers.openrouter.chat",
        "lionhub.providers.ag2.groupchat",
        "lionhub.providers.ag2.agent",
        "lionhub.providers.ag2.nlip",
    ]
    # Pins: each failure carries the missing-module text of the blocked hub root.
    # Removed together with the loader under ADR-0140 D1.
    assert report["failure_texts"] == ["No module named 'lionhub'"]
    # Pins: after the load, only the scripted test provider is registered.
    # ADR-0140 D1 replaces this loader and turns the scripted provider's
    # registration into an explicitly selected core test fragment, so how this
    # list is produced changes with it.
    assert report["providers"] == [["scripted", "chat/completions"]]


def test_openai_model_without_hub_fails_as_unknown_provider(tmp_path):
    report = _run_blocked(
        """
from lionagi.service.imodel import iModel

try:
    model = iModel(provider="openai", model="gpt-4o-mini")
except BaseException as exc:
    out = {"constructed": False, "error_type": type(exc).__name__, "error_text": str(exc)}
else:
    out = {"constructed": True, "endpoint_class": type(model.endpoint).__name__}
print(json.dumps(out))
""",
        tmp_path,
    )

    # Pins: with the hub absent the model is not built, and the failure is the
    # same ProviderNotFoundError (a ValueError) an unknown provider name gets,
    # so a missing hub cannot be told apart from a misspelled provider. ADR-0140
    # D4 and ADR-0136 Amendment 3 C3 change this on purpose: in a 1.x
    # installation the hub is bundled, so its absence is reported as
    # MissingFeatureError (an ImportError) for feature ``providers``, naming the
    # requested provider and asking to repair the installation of ``lionagi``.
    # ProviderNotFoundError stays for names the catalog really lacks.
    assert report["constructed"] is False, report
    assert report["error_type"] == "ProviderNotFoundError", report
    assert report["error_text"].startswith("no endpoint registered for provider 'openai'"), report


def test_context_window_without_hub_falls_back_to_default(tmp_path):
    report = _run_blocked(
        """
from lionagi.service.token_budget import lookup_context_window

print(json.dumps({
    "gpt-4o-mini": lookup_context_window("gpt-4o-mini", "openai"),
    "gpt-4.1": lookup_context_window("gpt-4.1", "openai"),
}))
""",
        tmp_path,
    )

    # Pins: with the provider tables unreachable the lookup swallows the import
    # failure and returns the default estimate. The first model's table value is
    # also the default, so it cannot tell a table hit from the fallback; the
    # second model has a larger window in the hub's table, so its value shows
    # the fallback. ADR-0140 D5 changes both: a missing manifest or failed
    # metadata load raises a typed error instead of returning 128,000.
    assert report == {"gpt-4o-mini": 128000, "gpt-4.1": 128000}


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
