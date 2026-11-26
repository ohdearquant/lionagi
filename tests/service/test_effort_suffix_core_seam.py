# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``iModel`` routes a model name's effort suffix by the selected endpoint class alone.

The class names the request field (``effort_kwarg``) and may clamp the effort
(``clamp_effort``). A class that names no field keeps the model name whole. Every model here is
built from a supplied provider catalog of core endpoint classes, so none needs the hub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.provider_catalog import (
    EndpointDeclaration,
    ProviderFragment,
    compose_provider_catalog,
)
from lionagi.service.connections.registry import EndpointRegistry
from lionagi.service.imodel import iModel
from tests._core_imports import ROOT


class _Plain(Endpoint):
    def __init__(self, config=None, **kwargs):
        super().__init__(
            config
            or EndpointConfig(
                name="effort-seam-fixture", provider="plain", endpoint="chat", auth_type="none"
            ),
            **kwargs,
        )


class _Declares(_Plain):
    effort_kwarg = "effort"


class _Clamps(_Plain):
    effort_kwarg = "effort"
    calls: list[tuple[str, str]] = []

    @classmethod
    def clamp_effort(cls, effort, model):
        cls.calls.append((effort, model))
        return "low"


def _fragment(key, factory):
    declaration = EndpointDeclaration(
        key="chat",
        aliases=(),
        kind="api",
        factory=factory,
        requirements=(),
        settings={},
        diagnostic_id=f"test.{key}.chat",
    )
    return ProviderFragment(
        contract_version=1,
        key=key,
        aliases=(f"{key}-alias",),
        endpoints=(declaration,),
        default_endpoint="chat",
        context_windows=None,
    )


@pytest.fixture
def catalog():
    _Clamps.calls.clear()
    return compose_provider_catalog(
        (
            _fragment("plain", lambda: _Plain),
            _fragment("declares", lambda: _Declares),
            _fragment("clamps", lambda: _Clamps),
        ),
        source="effort-seam",
    )


def _kwargs(catalog, provider, model, **extra):
    return iModel(
        provider=provider, model=model, provider_catalog=catalog, **extra
    ).endpoint.config.kwargs


def test_a_class_that_names_no_effort_field_keeps_the_name_whole(catalog):
    kwargs = _kwargs(catalog, "plain", "x-high")
    assert kwargs["model"] == "x-high"
    assert "effort" not in kwargs


@pytest.mark.parametrize("provider", ["declares", "DECLARES-alias"])
def test_a_named_field_takes_the_folded_effort(catalog, provider):
    kwargs = _kwargs(catalog, provider, "x-HIGH")
    assert (kwargs["model"], kwargs["effort"]) == ("x", "high")


def test_an_explicit_effort_wins_and_the_suffix_still_leaves_the_name(catalog):
    kwargs = _kwargs(catalog, "declares", "x-high", effort="minimal")
    assert (kwargs["model"], kwargs["effort"]) == ("x", "minimal")


def test_a_name_without_a_suffix_sets_no_effort(catalog):
    kwargs = _kwargs(catalog, "declares", "x")
    assert kwargs["model"] == "x"
    assert "effort" not in kwargs


def test_the_class_clamp_sees_the_folded_effort_and_the_bare_name(catalog):
    kwargs = _kwargs(catalog, "clamps", "x-MAX")
    assert (kwargs["model"], kwargs["effort"]) == ("x", "low")
    assert _Clamps.calls == [("max", "x")]
    _kwargs(catalog, "clamps", "x-max", effort="high")
    assert _Clamps.calls == [("max", "x")]


def test_the_class_lookup_answers_only_a_declared_endpoint(catalog):
    assert (
        EndpointRegistry.declared_endpoint_class("declares", provider_catalog=catalog) is _Declares
    )
    assert (
        EndpointRegistry.declared_endpoint_class("declares-alias", "chat", provider_catalog=catalog)
        is _Declares
    )
    assert EndpointRegistry.declared_endpoint_class("undeclared", provider_catalog=catalog) is None


def test_a_declaration_that_cannot_activate_is_tried_once_and_matching_still_raises():
    calls = []

    def broken():
        calls.append(1)
        raise ImportError("declared implementation is not importable")

    catalog = compose_provider_catalog((_fragment("broken", broken),), source="effort-seam-broken")
    assert EndpointRegistry.declared_endpoint_class("broken", provider_catalog=catalog) is None
    assert EndpointRegistry.declared_endpoint_class("broken", provider_catalog=catalog) is None
    # The lookup keeps the failure of its one attempt instead of building the class again.
    assert calls == [1]
    # The lookup swallows nothing for the caller: matching runs the factory itself and its
    # error reaches the caller unchanged, as it does for a model name with no suffix.
    with pytest.raises(ImportError, match="declared implementation is not importable"):
        iModel(provider="broken", model="x-high", provider_catalog=catalog)
    with pytest.raises(ImportError, match="declared implementation is not importable"):
        iModel(provider="broken", model="x", provider_catalog=catalog)


def test_with_only_the_core_on_the_path_the_seam_imports_no_hub(tmp_path):
    site = tmp_path / "site"
    shutil.copytree(
        ROOT / "lionagi", site / "lionagi", ignore=shutil.ignore_patterns("__pycache__")
    )
    code = """
import importlib.util
import json
import sys
from pathlib import Path

site = Path(sys.argv[1]).resolve()
sys.path[:] = [str(site)] + [
    entry
    for entry in sys.path
    if entry and not (Path(entry) / "lionagi").exists() and not (Path(entry) / "lionhub").exists()
]
sys.meta_path[:] = [f for f in sys.meta_path if "editable" not in type(f).__module__]
absent = importlib.util.find_spec("lionhub") is None

import lionagi
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig
from lionagi.service.connections.provider_catalog import (
    EndpointDeclaration,
    ProviderFragment,
    compose_provider_catalog,
)
from lionagi.service.imodel import iModel


class Plain(Endpoint):
    def __init__(self, config=None, **kwargs):
        super().__init__(
            config
            or EndpointConfig(name="seam", provider="plain", endpoint="chat", auth_type="none"),
            **kwargs,
        )


class Declares(Plain):
    effort_kwarg = "effort"


def fragment(key, cls):
    declaration = EndpointDeclaration(
        key="chat", aliases=(), kind="api", factory=lambda: cls, requirements=(), settings={},
        diagnostic_id=key,
    )
    return ProviderFragment(
        contract_version=1, key=key, aliases=(), endpoints=(declaration,),
        default_endpoint="chat", context_windows=None,
    )


catalog = compose_provider_catalog((fragment("plain", Plain), fragment("declares", Declares)), source="seam")
plain = iModel(provider="plain", model="x-high", provider_catalog=catalog).endpoint.config.kwargs
declares = iModel(provider="declares", model="x-high", provider_catalog=catalog).endpoint.config.kwargs
print(json.dumps({
    "hub_absent": absent,
    "core_from_site": Path(lionagi.__file__).resolve().is_relative_to(site),
    "plain": [plain["model"], plain.get("effort")],
    "declares": [declares["model"], declares.get("effort")],
    "hub_modules": sorted(n for n in sys.modules if n == "lionhub" or n.startswith("lionhub.")),
}))
"""
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script
        [sys.executable, "-c", code, str(site)],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": os.environ.get("TMPDIR", str(tmp_path)),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "hub_absent": True,
        "core_from_site": True,
        "plain": ["x-high", None],
        "declares": ["x", "high"],
        "hub_modules": [],
    }
