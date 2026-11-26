# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Without the hub the core keeps its routing seam and has no account engine."""

from tests.service.test_hub_absent_characterization import _run_blocked


def test_the_seam_imports_and_the_engine_is_absent_without_lionhub(tmp_path):
    report = _run_blocked(
        """
import importlib
import warnings

from lionagi.service.accounts import _request_guard, _scope, _types, _usage_shape

engine = {}
for name in ("_credentials", "_ledger", "_pricing", "_router"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            importlib.import_module(f"lionagi.service.accounts.{name}")
        except ModuleNotFoundError as exc:
            engine[name] = exc.name
        else:
            engine[name] = "imported"

print(json.dumps({
    "seam": sorted(m.__name__ for m in (_request_guard, _scope, _types, _usage_shape)),
    "engine": engine,
    "hub_modules": sorted(
        name for name in sys.modules if name == "lionhub" or name.startswith("lionhub.")
    ),
}))
""",
        tmp_path,
    )

    assert report == {
        "seam": [
            "lionagi.service.accounts._request_guard",
            "lionagi.service.accounts._scope",
            "lionagi.service.accounts._types",
            "lionagi.service.accounts._usage_shape",
        ],
        # Each old engine name reaches for the hub and fails the way a missing package does.
        "engine": {
            "_credentials": "lionhub",
            "_ledger": "lionhub",
            "_pricing": "lionhub",
            "_router": "lionhub",
        },
        "hub_modules": [],
    }
