# SPDX-License-Identifier: Apache-2.0

"""The provider compatibility loader caches one catalog, or one typed failure, per process.

The loader remembers its first outcome for the life of the process, and a module that is not
installed has to be simulated before the first call, so every case runs in a fresh
interpreter. A meta-path finder installed ahead of the first call stands in for an absent
module and counts how often the import system asks for it; a replacement for the manifest
function counts how often it is called. No endpoint implementation is loaded and nothing
outside the process is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from lionhub.providers.manifest import get_provider_manifest

ROOT = Path(__file__).resolve().parents[3]
_LIONHUB_SRC = str(ROOT / "lionhub" / "src")
_MANIFEST_MODULE = "lionhub.providers.manifest"
_SENTINEL = "sentinel-7f3a91-never-echoed"

_CHILD = """
import dataclasses
import importlib
import json
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
scenario = sys.argv[2]
sentinel = sys.argv[3]
attempts = {}
manifest_calls = []


class AbsentModule:
    # Stands in for a module that is not installed: the import system asks it first.
    def __init__(self, name):
        self.name = name

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.name:
            attempts[fullname] = attempts.get(fullname, 0) + 1
            raise ModuleNotFoundError("No module named " + repr(fullname), name=fullname)
        return None


if scenario == "root_missing":
    sys.meta_path.insert(0, AbsentModule("lionhub"))
elif scenario == "manifest_missing":
    sys.meta_path.insert(0, AbsentModule("lionhub.providers.manifest"))

from lionagi.service.connections._provider_compat import default_provider_catalog


def hub_modules():
    return sorted(m for m in sys.modules if m == "lionhub" or m.startswith("lionhub."))


def describe(exc):
    cause = exc.__cause__
    fields = dict(vars(exc))
    if isinstance(exc, ImportError):
        fields["name"] = exc.name
    return {
        "ok": False,
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": str(exc),
        "fields": fields,
        "cause_type": None if cause is None else type(cause).__name__,
        "cause_text": None if cause is None else str(cause),
        "leaks_sentinel": sentinel in str(exc) or sentinel in repr(fields),
    }


def call():
    try:
        return default_provider_catalog(), None
    except Exception as exc:
        return None, exc


def snapshot():
    return {"attempts": dict(attempts), "manifest_calls": len(manifest_calls)}


result = {"hub_after_import": hub_modules(), "attempts_after_import": dict(attempts)}

if scenario == "import_only":
    importlib.import_module("lionhub.providers.manifest")
    result["hub_after_control"] = hub_modules()
    print(json.dumps(result))
    sys.exit(0)

if scenario in ("raises", "version_2", "healthy", "concurrent"):
    module = importlib.import_module("lionhub.providers.manifest")
    real = module.get_provider_manifest

    def counted():
        manifest_calls.append(1)
        if scenario == "raises":
            raise RuntimeError(sentinel)
        if scenario == "version_2":
            return dataclasses.replace(real(), contract_version=2)
        if scenario == "concurrent":
            time.sleep(0.3)
        return real()

    module.get_provider_manifest = counted

if scenario == "concurrent":
    barrier = threading.Barrier(8, timeout=30)
    outcomes = [None] * 8

    def work(index):
        try:
            barrier.wait()
            outcomes[index] = ("ok", default_provider_catalog())
        except Exception as exc:
            outcomes[index] = ("error", exc)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    result["completed"] = sum(outcome is not None for outcome in outcomes)
    result["errors"] = [
        type(outcome[1]).__name__ for outcome in outcomes if outcome and outcome[0] == "error"
    ]
    result["distinct_catalogs"] = len(
        {id(outcome[1]) for outcome in outcomes if outcome and outcome[0] == "ok"}
    )
    result["manifest_calls"] = len(manifest_calls)
    print(json.dumps(result))
    sys.exit(0)

first, first_exc = call()
result["after_first"] = snapshot()
second, second_exc = call()
result["after_second"] = snapshot()
result["first"] = {"ok": True} if first_exc is None else describe(first_exc)
result["second"] = {"ok": True} if second_exc is None else describe(second_exc)
result["same_type"] = type(first_exc) is type(second_exc)
result["same_cause"] = (
    first_exc is not None
    and second_exc is not None
    and first_exc.__cause__ is not None
    and first_exc.__cause__ is second_exc.__cause__
)

if scenario == "healthy":
    from lionagi.service.connections.provider_catalog import (
        ProviderCatalog,
        compose_provider_catalog,
    )

    result["is_catalog"] = isinstance(first, ProviderCatalog)
    result["equals_composed_manifest"] = first == compose_provider_catalog(real())
    result["same_catalog"] = first is second

if scenario == "version_2":
    from lionagi.service.connections.provider_catalog import compose_provider_catalog

    try:
        compose_provider_catalog(dataclasses.replace(real(), contract_version=2))
    except Exception as exc:
        result["direct"] = describe(exc)

print(json.dumps(result, default=repr))
"""


def _run_child(scenario: str) -> dict:
    """Run one scenario in a fresh interpreter with this process's environment; parse its JSON line."""
    proc = subprocess.run(  # noqa: S603 -- this interpreter, fixed script
        [sys.executable, "-c", _CHILD, _LIONHUB_SRC, scenario, _SENTINEL],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_replayed(result: dict, type_name: str, *, attempts: dict, manifest_calls: int) -> None:
    """The first call failed with ``type_name``; the second raised the same failure without retrying."""
    first, second = result["first"], result["second"]
    assert first["type"] == second["type"] == type_name
    assert result["same_type"] is True
    assert second["fields"] == first["fields"]
    assert second["message"] == first["message"]
    assert result["after_first"] == {"attempts": attempts, "manifest_calls": manifest_calls}
    assert result["after_second"] == result["after_first"]


def test_absent_hub_root_raises_missing_feature_error_and_is_not_retried():
    result = _run_child("root_missing")

    assert result["attempts_after_import"] == {}
    _assert_replayed(result, "MissingFeatureError", attempts={"lionhub": 1}, manifest_calls=0)
    first = result["first"]
    assert first["module"] == "lionagi._feature_errors"
    fields = first["fields"]
    assert fields["feature"] == "providers"
    assert fields["missing_distribution"] == "lionagi"
    assert fields["extra"] is None
    assert fields["name"] == "lionhub"
    # The guidance points at repairing the bundled installation, not at installing an extra.
    assert "lionagi" in fields["install_hint"]
    assert "reinstall" in fields["install_hint"].lower()
    assert first["cause_type"] == "ModuleNotFoundError"
    assert result["same_cause"] is True


def test_absent_manifest_module_with_a_present_hub_is_an_activation_failure():
    result = _run_child("manifest_missing")

    assert result["attempts_after_import"] == {}
    _assert_replayed(
        result, "ProviderActivationError", attempts={_MANIFEST_MODULE: 1}, manifest_calls=0
    )
    first = result["first"]
    assert first["module"] == "lionagi._provider_errors"
    assert first["fields"]["source"] == _MANIFEST_MODULE
    assert first["fields"]["reason"] == "activation_failed"
    assert first["fields"]["endpoint"] is None
    assert first["cause_type"] == "ModuleNotFoundError"
    assert result["same_cause"] is True


def test_raising_manifest_function_is_an_activation_failure_that_echoes_nothing():
    result = _run_child("raises")

    _assert_replayed(result, "ProviderActivationError", attempts={}, manifest_calls=1)
    for call in (result["first"], result["second"]):
        assert call["fields"]["source"] == _MANIFEST_MODULE
        assert call["fields"]["reason"] == "activation_failed"
        assert call["cause_type"] == "RuntimeError"
        # The original is chained, so its text is reachable there and nowhere else.
        assert _SENTINEL in call["cause_text"]
        assert call["leaks_sentinel"] is False
    assert result["same_cause"] is True


def test_manifest_with_an_unsupported_contract_version_propagates_unchanged():
    result = _run_child("version_2")

    _assert_replayed(result, "ProviderContractVersionError", attempts={}, manifest_calls=1)
    first = result["first"]
    assert first["module"] == "lionagi._provider_errors"
    assert first["fields"]["observed"] == 2
    assert first["fields"]["expected"] == [1]
    assert first["fields"]["source"] == get_provider_manifest().source
    # Same type, fields and message as composing that manifest directly.
    direct = result["direct"]
    assert first["type"] == direct["type"]
    assert first["message"] == direct["message"]
    assert first["fields"] == direct["fields"]


def test_healthy_load_returns_the_catalog_composed_from_the_manifest_once():
    result = _run_child("healthy")

    assert result["first"] == {"ok": True}
    assert result["second"] == {"ok": True}
    assert result["is_catalog"] is True
    assert result["equals_composed_manifest"] is True
    assert result["same_catalog"] is True
    assert result["after_first"] == {"attempts": {}, "manifest_calls": 1}
    assert result["after_second"] == result["after_first"]


def test_importing_the_loader_module_loads_no_hub_module():
    result = _run_child("import_only")

    assert result["hub_after_import"] == []
    # Control: the same check does see the hub once something imports it.
    assert _MANIFEST_MODULE in result["hub_after_control"]


def test_concurrent_first_calls_load_the_manifest_once_and_share_the_catalog():
    result = _run_child("concurrent")

    assert result["completed"] == 8
    assert result["errors"] == []
    assert result["manifest_calls"] == 1
    assert result["distinct_catalogs"] == 1
