# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from scripts.build_provider_inventory import HUB, INVENTORY, ROOT, verify_inventory

MANIFEST = f"{HUB}/manifest.py"
EMBEDDINGS = (
    '            _Endpoint("EMBED", "embeddings", ("embed",), '
    '"api", "openai.embed:OpenaiEmbedEndpoint"),\n'
)

_PURITY_CHILD = """
import _thread
import importlib.abc
from pathlib import Path
import runpy
import sys
import threading


class RefuseProductImports(importlib.abc.MetaPathFinder):
    core = False

    def find_spec(self, fullname, path=None, target=None):
        prefixes = ("lionagi", "lionhub") if self.core else ("lionagi.providers", "lionhub")
        if any(fullname == prefix or fullname.startswith(prefix + ".") for prefix in prefixes):
            raise AssertionError(f"collector attempted product import: {fullname}")


def reject_thread(*args, **kwargs):
    raise AssertionError("collector attempted to start a thread")


def registry_state(registry):
    return {
        "entries": tuple(registry._entries),
        "alias_owners": registry._alias_owners.copy(),
        "loaded": registry._loaded,
        "failed": registry._failed,
        "activation_failures": registry._activation_failures.copy(),
        "plugin_registration": vars(registry._plugin_registration).copy(),
    }


blocker = RefuseProductImports()
sys.meta_path.insert(0, blocker)
if sys.argv[1] == "registry-first":
    from lionagi.service.connections.registry import EndpointRegistry

    # This is a private interpreter. Seed the imperative state without activating a provider.
    registry = EndpointRegistry
    registry._entries.append(object())
    registry._alias_owners["inventory-probe"] = "inventoried"
    registry._failed = (object(),)
    declaration = object()
    registry._activation_failures[id(declaration)] = (declaration, RuntimeError("inventory probe"))
    registry._plugin_registration.provenance = ("inventory-probe", "inventory.probe")
    captured = registry_state(registry)
else:
    registry = None

modules_before = {key for key in sys.modules if key.split(".", 1)[0] in {"lionagi", "lionhub"}}
blocker.core = True
threading.Thread.start = reject_thread
_thread.start_new_thread = reject_thread
if hasattr(_thread, "start_joinable_thread"):
    _thread.start_joinable_thread = reject_thread

module = runpy.run_path("scripts/build_provider_inventory.py")
payload = module["collect"](lambda path: Path(path).read_text(encoding="utf-8"))
sys.stdout.write(module["encode"](payload))
modules_after = {key for key in sys.modules if key.split(".", 1)[0] in {"lionagi", "lionhub"}}
assert modules_after == modules_before
if registry is not None:
    assert registry_state(registry) == captured
"""


def read_source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def replaced(path: str, before: str, after: str) -> str:
    original = read_source(path)
    if original.count(before) != 1:
        raise AssertionError(f"Expected one mutation site in {path}: {before}")
    return original.replace(before, after, 1)


class TestProviderAuthorityInventory(unittest.TestCase):
    def setUp(self):
        self.expected = (ROOT / INVENTORY).read_bytes()

    def test_checked_inventory_matches_source(self):
        verify_inventory(read_source, self.expected)

    def assert_stale(self, overrides):
        def with_mutation(path):
            return overrides[path] if path in overrides else read_source(path)

        with self.assertRaisesRegex(ValueError, "differs from the provider source inventory"):
            verify_inventory(with_mutation, self.expected)

    def test_dropping_a_manifest_declaration_is_detected(self):
        self.assert_stale({MANIFEST: replaced(MANIFEST, EMBEDDINGS, "")})

    def test_changing_one_endpoint_alias_is_detected(self):
        config = f"{HUB}/openai/_config.py"
        self.assert_stale(
            {
                MANIFEST: replaced(MANIFEST, EMBEDDINGS, EMBEDDINGS.replace('"embed"', '"vector"')),
                config: replaced(config, '["embed"]', '["vector"]'),
            }
        )

    def test_changing_one_context_window_is_detected(self):
        config = f"{HUB}/google/_config.py"
        self.assert_stale(
            {
                config: replaced(
                    config, '"gemini-2.0-flash": 1_048_576', '"gemini-2.0-flash": 2_048_576'
                ),
            }
        )

    def test_changing_context_window_scan_order_is_detected(self):
        self.assert_stale(
            {
                MANIFEST: replaced(
                    MANIFEST, '    "openai",\n    "anthropic",', '    "anthropic",\n    "openai",'
                ),
            }
        )

    def test_gemini_context_examples(self):
        examples = json.loads(self.expected)["context_examples"]
        assert [row["window"] for row in examples] == [
            1_048_576,
            1_048_576,
            1_048_576,
            200_000,
            1_048_576,
            200_000,
        ]

    def test_deterministic_without_imports_or_registry_publication(self):
        for order in ("isolated", "registry-first"):
            for seed in ("7", "73"):
                with self.subTest(order=order, seed=seed):
                    result = subprocess.run(  # noqa: S603 -- fixed interpreter and child script
                        [sys.executable, "-B", "-c", _PURITY_CHILD, order],
                        cwd=ROOT,
                        env={**os.environ, "PYTHONHASHSEED": seed},
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    assert result.returncode == 0, result.stderr.decode()
                    assert result.stdout == self.expected


if __name__ == "__main__":
    unittest.main()
