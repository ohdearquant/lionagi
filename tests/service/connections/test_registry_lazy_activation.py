# SPDX-License-Identifier: Apache-2.0

"""Automatic provider selection activates only the endpoint it selects.

With no supplied catalog, ``EndpointRegistry.match`` reads the bundled catalog's declarations
(the manifest imports no endpoint implementation) and calls the factory of the one declaration
the request selects. Nothing else is imported: not the other endpoints of that provider and not
the other providers. A selection fails at selection, with the typed error the failure matrix
names, and a failure is remembered per declaration.

Three groups of tests live here:

* fresh interpreters show which modules a selection imports and that every built-in key, alias,
  default and unlisted endpoint still selects the class the literal table in
  ``test_registry_match_pins`` names, whatever order the requests arrive in;
* injected catalogs show the failure rows (internal import failure, syntax error, factory
  defect, absent declared dependency, unsupported catalog) with their error types and fields,
  that one failure leaves other providers usable, and that a failure is cached per declaration;
* a fresh interpreter whose ``ollama`` package is absent shows the absent-dependency row for a
  real built-in provider.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import warnings

import pytest

from lionagi._feature_errors import MissingFeatureError
from lionagi._provider_errors import ProviderActivationError, ProviderContractVersionError
from lionagi.service.connections import _provider_compat
from lionagi.service.connections import registry as registry_module
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.provider_catalog import (
    DependencyRequirement,
    EndpointDeclaration,
    ProviderFragment,
    ProviderManifest,
    compose_provider_catalog,
)
from lionagi.service.connections.registry import EndpointRegistry, ProviderNotFoundError
from tests._core_imports import ROOT
from tests.service.connections.test_registry_match_pins import (
    BY_PROVIDER,
    DEFAULT_CLASS,
    ROWS,
    UNLISTED_ENDPOINT,
    _mixed_case,
    _spellings,
)

_GENERIC = "lionagi.service.connections.endpoint:Endpoint"
_CUSTOM_BASE_URL = "https://llm.example.test/v1"

# This checkout and its lionhub package, put first on each child's path so a fresh interpreter
# reads the providers in this tree and not an installed copy.
_CHILD_PATH = (str(ROOT), str(ROOT / "lionhub" / "src"))


def _run_child(code, tmp_path, *args):
    """Run ``code`` in a fresh interpreter and return the JSON object it printed last."""
    env = dict(os.environ)
    env.pop("LIONAGI_CHAT_PROVIDER", None)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*_CHILD_PATH, *([inherited] if inherited else [])])
    # The registry-miss path consults plugins under the home directory; keep it empty.
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed script
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=240,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Modules of a hub provider package other than its ``_config`` module, which the manifest reads.
_IMPLEMENTATION_MODULES = """
def implementation_modules(selected_packages=None):
    parts = [m.split(".") for m in sys.modules if m.startswith("lionhub.providers.")]
    return sorted(
        ".".join(p)
        for p in parts
        if len(p) > 3
        and p[3] != "_config"
        and (selected_packages is None or p[2] not in selected_packages)
    )
"""

_SELECT_CHILD = (
    r"""
import json
import sys

from lionagi.service.connections.registry import EndpointRegistry
from lionagi.service.imodel import iModel
"""
    + _IMPLEMENTATION_MODULES
    + r"""
selected = []
for name in sys.argv[1:]:
    model = iModel(provider=name, model="a-model", api_key="placeholder-value")
    cls = type(model.endpoint)
    selected.append(f"{cls.__module__}:{cls.__qualname__}")

from lionhub.providers import manifest

declared = {
    declaration.diagnostic_id.partition(":")[0]
    for fragment in manifest.get_provider_manifest().fragments
    for declaration in fragment.endpoints
}
packages = {cls.split(".")[2] for cls in selected}
print(
    json.dumps(
        {
            "selected": selected,
            "declared_implementations_loaded": sorted(m for m in declared if m in sys.modules),
            "other_packages": implementation_modules(packages),
            "eager_load_flag": EndpointRegistry._loaded,
        }
    )
)
"""
)

_CANONICAL_CHILD = (
    r"""
import json
import sys

from lionagi.service.connections.registry import EndpointRegistry
"""
    + _IMPLEMENTATION_MODULES
    + r"""
canonical = {name: EndpointRegistry.canonical_provider(name) for name in sys.argv[1:]}
print(
    json.dumps(
        {
            "canonical": canonical,
            "implementations": implementation_modules(),
            "eager_load_flag": EndpointRegistry._loaded,
        }
    )
)
"""
)

_PARITY_CHILD = r"""
import json
import sys
import warnings

from lionagi.service.connections.match_endpoint import match_endpoint

warnings.simplefilter("ignore")
cases = json.loads(sys.argv[1])
if sys.argv[2] == "reverse":
    cases = cases[::-1]
selected = {}
for provider, endpoint in cases:
    cls = type(match_endpoint(provider=provider, endpoint=endpoint))
    selected[f"{provider}|{endpoint}"] = f"{cls.__module__}:{cls.__qualname__}"
print(json.dumps(selected))
"""

_SCRIPTED_CHILD = (
    r"""
import json
import sys

from lionagi.service.connections.match_endpoint import match_endpoint
"""
    + _IMPLEMENTATION_MODULES
    + r"""
cls = type(match_endpoint(provider="scripted", endpoint=""))
print(
    json.dumps({"cls": f"{cls.__module__}:{cls.__qualname__}", "hub": implementation_modules()})
)
"""
)

_LISTING_CHILD = r"""
import json

from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import EndpointRegistry

match_endpoint(provider="scripted", endpoint="")
match_endpoint(provider="groq", endpoint="")
rows = EndpointRegistry.list_providers()
print(json.dumps({"rows": [[row["provider"], row["endpoint"]] for row in rows]}))
"""

_LISTING_ORDER_CHILD = r"""
import json

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.connections.registry import EndpointRegistry
from lionhub.providers import manifest

# Providers the catalog lists after others are selected before anything is listed.
match_endpoint(provider="scripted", endpoint="")
match_endpoint(provider="groq", endpoint="")
match_endpoint(provider="claude", endpoint="")


class Registered(Endpoint):
    pass


EndpointRegistry.register(provider="registered-provider", endpoint="chat")(Registered)
rows = [[row["provider"], row["endpoint"]] for row in EndpointRegistry.list_providers()]
catalog = [
    [fragment.key, declaration.key]
    for fragment in manifest.get_provider_manifest().fragments
    for declaration in fragment.endpoints
]
print(json.dumps({"rows": rows, "catalog": catalog}))
"""

_ABSENT_DEPENDENCY_CHILD = r"""
import json
import sys

# A None entry makes ``import ollama`` fail and ``find_spec("ollama")`` answer None, which is
# what an interpreter without the package does.
sys.modules["ollama"] = None

from lionagi._feature_errors import MissingFeatureError
from lionagi.service.connections.match_endpoint import match_endpoint
from lionagi.service.imodel import iModel


def describe(error):
    return {
        "type": type(error).__name__,
        "feature": error.feature,
        "provider": error.provider,
        "endpoint": error.endpoint,
        "distribution": error.missing_distribution,
        "hint": error.install_hint,
        "source": error.source,
        "name": error.name,
        "cause": type(error.__cause__).__name__,
    }


out = {"failures": {}}
for endpoint in ("chat/completions", "embeddings", "generate", "chat", ""):
    try:
        match_endpoint(provider="ollama", endpoint=endpoint, model="a-model")
    except MissingFeatureError as error:
        out["failures"][endpoint] = describe(error)
try:
    iModel(provider="ollama", model="a-model")
except MissingFeatureError as error:
    out["imodel"] = describe(error)
other = iModel(provider="openai", model="a-model", api_key="placeholder-value")
out["other"] = type(other.endpoint).__name__
print(json.dumps(out))
"""


def _parity_expectations():
    """``{(provider, endpoint): class}`` for every built-in key, alias and letter case,
    written from the literal table in ``test_registry_match_pins``."""
    expected = {}
    for provider, rows in BY_PROVIDER.items():
        names = []
        for name in (provider, *rows[0].provider_aliases):
            names += _spellings(name, name.upper(), _mixed_case(name))
        for name in names:
            expected[(name, "")] = DEFAULT_CLASS[name.lower()]
            for row in rows:
                for endpoint in _spellings(row.endpoint, *row.aliases):
                    expected[(name, endpoint)] = row.cls
            expected[(name, UNLISTED_ENDPOINT)] = rows[0].cls if len(rows) == 1 else _GENERIC
    return expected


# -- fresh interpreters: what a selection imports ---------------------------


@pytest.mark.parametrize(
    ("names", "modules"),
    [
        pytest.param(["openai"], ["lionhub.providers.openai.chat"], id="openai"),
        pytest.param(["groq"], ["lionhub.providers.groq.chat"], id="groq"),
        pytest.param(["claude"], ["lionhub.providers.anthropic.claude_code"], id="claude-alias"),
        pytest.param(
            ["openai", "groq"],
            ["lionhub.providers.groq.chat", "lionhub.providers.openai.chat"],
            id="two-providers",
        ),
    ],
)
def test_selecting_a_provider_imports_only_the_selected_implementation(tmp_path, names, modules):
    result = _run_child(_SELECT_CHILD, tmp_path, *names)
    # Must-be-present control: each selected class was built, so its module is loaded.
    assert result["selected"] == [DEFAULT_CLASS[name] for name in names]
    # No endpoint module was imported except the selected ones, not even a sibling endpoint of
    # the same provider.
    assert result["declared_implementations_loaded"] == modules
    # No module of any other provider package was imported besides its configuration module.
    assert result["other_packages"] == []
    # The eager-load flag stays down: selection did not load every provider.
    assert result["eager_load_flag"] is False


def test_canonical_provider_resolves_declared_names_without_importing_an_implementation(tmp_path):
    names = [
        spelling
        for provider, rows in BY_PROVIDER.items()
        for name in (provider, *rows[0].provider_aliases)
        for spelling in _spellings(name, name.upper(), _mixed_case(name))
    ]
    names += ["No_Such_Provider", " codex "]
    result = _run_child(_CANONICAL_CHILD, tmp_path, *names)
    expected = {"No_Such_Provider": "no_such_provider", " codex ": " codex "}
    for provider, rows in BY_PROVIDER.items():
        for name in (provider, *rows[0].provider_aliases):
            for spelling in _spellings(name, name.upper(), _mixed_case(name)):
                expected[spelling] = provider
    assert result["canonical"] == expected
    assert result["implementations"] == []
    assert result["eager_load_flag"] is False


@pytest.mark.parametrize("order", ["forward", "reverse"])
def test_every_built_in_key_alias_and_default_selects_the_same_class(tmp_path, order):
    expected = _parity_expectations()
    assert len(expected) > 300
    cases = [list(case) for case in sorted(expected)]
    selected = _run_child(_PARITY_CHILD, tmp_path, json.dumps(cases), order)
    assert selected == {
        f"{provider}|{endpoint}": cls for (provider, endpoint), cls in expected.items()
    }


def test_the_scripted_provider_selects_without_importing_any_hub_implementation(tmp_path):
    result = _run_child(_SCRIPTED_CHILD, tmp_path)
    assert result["cls"] == "lionagi.testing._endpoint:ScriptedEndpoint"
    assert result["hub"] == []


def test_listing_after_a_partial_selection_is_complete_and_keeps_scripted_last(tmp_path):
    result = _run_child(_LISTING_CHILD, tmp_path)
    rows = result["rows"]
    assert {tuple(row) for row in rows} == {(row.provider, row.endpoint) for row in ROWS}
    assert len(rows) == len(ROWS)
    assert rows[-1][0] == "scripted"


def test_listing_rows_follow_catalog_order_whichever_provider_was_selected_first(tmp_path):
    result = _run_child(_LISTING_ORDER_CHILD, tmp_path)
    # The built-in rows come in the bundled catalog's order although groq and claude_code were
    # selected, and so registered, before anything else; the row registered by the caller
    # follows them, and the scripted provider's row is last.
    assert result["rows"] == [
        *result["catalog"],
        ["registered-provider", "chat"],
        ["scripted", "chat/completions"],
    ]


def test_an_absent_declared_dependency_of_a_built_in_provider_raises_a_missing_feature_error(
    tmp_path,
):
    result = _run_child(_ABSENT_DEPENDENCY_CHILD, tmp_path)
    expected = {
        "feature": "providers",
        "provider": "ollama",
        "distribution": "ollama",
        "hint": "pip install lionagi[ollama]",
        "source": "lionhub.providers",
        "name": "ollama",
        "type": "MissingFeatureError",
        "cause": "ModuleNotFoundError",
    }
    assert result["failures"] == {
        "chat/completions": {**expected, "endpoint": "chat/completions"},
        "embeddings": {**expected, "endpoint": "embeddings"},
        "generate": {**expected, "endpoint": "generate"},
        "chat": {**expected, "endpoint": "chat/completions"},
        "": {**expected, "endpoint": "chat/completions"},
    }
    assert result["imodel"] == {**expected, "endpoint": "chat/completions"}
    # Another provider stays usable in the same process after the failure.
    assert result["other"] == "OpenaiChatEndpoint"


# -- injected catalogs: the selection and the failure rows ------------------


class _Stub:
    """Stands in for an endpoint class; keeps what it was built with."""

    def __init__(self, config=None, **kwargs):
        self.config = config
        self.kwargs = kwargs


_DEPENDENCY = DependencyRequirement(
    distribution="depx-dist",
    import_roots=("depx",),
    version_constraint=">=1",
    install_hint="pip install lionagi[depx]",
)
_SOURCE = "stub source"


def _factory(calls, label, cls=_Stub):
    """A factory that records its label and returns ``cls``."""

    def factory():
        calls.append(label)
        return cls

    return factory


def _raising(calls, label, error):
    """A factory that records its label and raises ``error``."""

    def factory():
        calls.append(label)
        raise error

    return factory


def _declaration(key, factory, *, aliases=(), requirements=()):
    return EndpointDeclaration(
        key=key,
        aliases=aliases,
        kind="api",
        factory=factory,
        requirements=requirements,
        settings={},
        diagnostic_id=f"stub_module_{key}:Endpoint",
    )


def _fragment(key, declarations, *, aliases=()):
    return ProviderFragment(
        contract_version=1,
        key=key,
        aliases=aliases,
        endpoints=tuple(declarations),
        default_endpoint=declarations[0].key,
        context_windows=None,
    )


def _install(monkeypatch, *fragments):
    """Make the stub catalog the bundled catalog for the rest of the test."""
    catalog = compose_provider_catalog(
        ProviderManifest(contract_version=1, source=_SOURCE, fragments=tuple(fragments))
    )
    monkeypatch.setattr(_provider_compat, "default_provider_catalog", lambda: catalog)
    return catalog


def _failing_alpha(monkeypatch, error):
    """Install ``alpha`` (endpoints ``a`` and ``b``, ``a`` first and the default), whose ``a``
    factory raises ``error``, beside a healthy ``beta``. Returns the factory call log."""
    calls = []
    alpha = _fragment(
        "alpha",
        [
            _declaration("a", _raising(calls, "alpha/a", error), requirements=(_DEPENDENCY,)),
            _declaration("b", _factory(calls, "alpha/b")),
        ],
    )
    beta = _fragment("beta", [_declaration("x", _factory(calls, "beta/x"))])
    _install(monkeypatch, alpha, beta)
    return calls


def test_a_selection_runs_only_the_selected_factory(monkeypatch):
    calls = []
    alpha = _fragment(
        "alpha",
        [
            _declaration("a", _factory(calls, "alpha/a"), aliases=("first",)),
            _declaration("b", _factory(calls, "alpha/b")),
            _declaration("c", _factory(calls, "alpha/c")),
        ],
        aliases=("alp",),
    )
    beta = _fragment("beta", [_declaration("x", _factory(calls, "beta/x"))])
    _install(monkeypatch, alpha, beta)

    assert type(match_endpoint("alpha", "b", model="a-model")) is _Stub
    assert calls == ["alpha/b"]
    result = match_endpoint("ALP", "first")
    assert type(result) is _Stub
    assert result.config is None
    assert calls == ["alpha/b", "alpha/a"]
    match_endpoint("alpha", "")
    assert calls == ["alpha/b", "alpha/a", "alpha/a"]
    # A provider declaring one endpoint selects it for any endpoint name.
    match_endpoint("beta", "any/endpoint")
    assert calls == ["alpha/b", "alpha/a", "alpha/a", "beta/x"]


def test_an_endpoint_a_healthy_provider_does_not_declare_routes_to_the_generic_endpoint(
    monkeypatch,
):
    calls = []
    alpha = _fragment(
        "alpha",
        [_declaration("a", _factory(calls, "alpha/a")), _declaration("b", _factory(calls, "b"))],
    )
    _install(monkeypatch, alpha)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = match_endpoint("alpha", "undeclared/endpoint")
    assert type(result) is Endpoint
    assert result.config.provider == "alpha"
    assert "b" not in calls


def test_an_unknown_provider_consults_plugins_and_is_then_not_found(monkeypatch):
    calls = []
    _install(monkeypatch, _fragment("alpha", [_declaration("a", _factory(calls, "alpha/a"))]))
    consulted = []
    monkeypatch.setattr(
        EndpointRegistry,
        "_consult_plugin_providers",
        classmethod(lambda cls: consulted.append(True) or False),
    )
    with pytest.raises(ProviderNotFoundError) as raised:
        match_endpoint("nobody", "")
    assert consulted == [True]
    assert "alpha" in str(raised.value)
    assert calls == []
    result = match_endpoint("nobody", "", openai_compatible=True)
    assert type(result) is Endpoint
    assert calls == []


DEFECTS = {
    "internal-import-failure": ImportError("an internal module cannot be imported"),
    "other-missing-module": ModuleNotFoundError("No module named 'other'", name="other"),
    "submodule-of-a-declared-root": ModuleNotFoundError(
        "No module named 'depx.sub'", name="depx.sub"
    ),
    "missing-module-without-a-name": ModuleNotFoundError("depx is not installed"),
    "syntax-error": SyntaxError("invalid syntax"),
    "factory-defect": RuntimeError("the factory has a defect"),
}


@pytest.mark.parametrize("error", DEFECTS.values(), ids=DEFECTS.keys())
def test_a_defective_declaration_raises_an_activation_error_with_the_cause_chained(
    monkeypatch, error
):
    _failing_alpha(monkeypatch, error)
    with pytest.raises(ProviderActivationError) as raised:
        match_endpoint("alpha", "a")
    failure = raised.value
    assert not isinstance(failure, MissingFeatureError)
    assert (failure.provider, failure.endpoint, failure.source, failure.reason) == (
        "alpha",
        "a",
        _SOURCE,
        "activation_failed",
    )
    assert failure.__cause__ is error


def test_a_declared_dependency_that_is_absent_raises_a_missing_feature_error(monkeypatch):
    error = ModuleNotFoundError("No module named 'depx'", name="depx")
    _failing_alpha(monkeypatch, error)
    with pytest.raises(MissingFeatureError) as raised:
        match_endpoint("alpha", "a")
    failure = raised.value
    assert not isinstance(failure, ProviderActivationError)
    assert (
        failure.feature,
        failure.provider,
        failure.endpoint,
        failure.missing_distribution,
        failure.install_hint,
        failure.source,
        failure.name,
    ) == ("providers", "alpha", "a", "depx-dist", "pip install lionagi[depx]", _SOURCE, "depx")
    assert failure.__cause__ is error


def test_an_activation_error_raised_by_a_factory_reaches_the_caller_unchanged(monkeypatch):
    cause = ImportError("the configuration cannot be imported")
    error = ProviderActivationError(
        provider="alpha", endpoint="a", source="the factory's source", reason="activation_failed"
    )
    error.__cause__ = cause
    _failing_alpha(monkeypatch, error)
    with pytest.raises(ProviderActivationError) as raised:
        match_endpoint("alpha", "a")
    assert (raised.value.provider, raised.value.endpoint, raised.value.source) == (
        "alpha",
        "a",
        "the factory's source",
    )
    assert raised.value.__cause__ is cause


@pytest.mark.parametrize(
    "extra",
    [{}, {"openai_compatible": True}, {"base_url": _CUSTOM_BASE_URL}],
    ids=["plain", "openai-compatible", "base-url"],
)
@pytest.mark.parametrize("endpoint", ["a", "", "undeclared/endpoint"])
def test_no_generic_route_hides_a_failed_declaration(monkeypatch, endpoint, extra):
    # ``undeclared/endpoint`` names no endpoint of ``alpha``, whose default endpoint is the
    # one that fails: the provider is not healthy, so the generic endpoint does not answer.
    _failing_alpha(monkeypatch, RuntimeError("the factory has a defect"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ProviderActivationError) as raised:
            match_endpoint("alpha", endpoint, **extra)
    assert raised.value.provider == "alpha"


def test_other_declarations_and_providers_stay_usable_after_a_failure(monkeypatch):
    calls = _failing_alpha(monkeypatch, RuntimeError("the factory has a defect"))
    with pytest.raises(ProviderActivationError):
        match_endpoint("alpha", "a")
    assert type(match_endpoint("alpha", "b")) is _Stub
    assert type(match_endpoint("beta", "")) is _Stub
    assert calls == ["alpha/a", "alpha/b", "beta/x"]


def test_a_failure_is_cached_per_declaration_and_warns_once(monkeypatch, caplog):
    error = RuntimeError("the factory has a defect")
    calls = _failing_alpha(monkeypatch, error)
    raised = []
    with caplog.at_level(logging.WARNING, logger=registry_module.logger.name):
        for _ in range(3):
            with pytest.raises(ProviderActivationError) as caught:
                match_endpoint("alpha", "a")
            raised.append(caught.value)
    assert calls == ["alpha/a"]
    warned = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warned) == 1
    assert "alpha" in warned[0].getMessage()
    assert "has a defect" not in warned[0].getMessage()
    # Each selection raises its own error object with the same fields and the same cause.
    assert len({id(failure) for failure in raised}) == 3
    assert {(f.provider, f.endpoint, f.source, f.reason) for f in raised} == {
        ("alpha", "a", _SOURCE, "activation_failed")
    }
    assert all(failure.__cause__ is error for failure in raised)


@pytest.fixture
def cold_registry(monkeypatch):
    """Make the next listing a cold load of whatever catalog the test installs.

    The loaded flag, the record of failed providers, the entry list and the per-declaration
    failure cache are replaced for the test and put back afterwards.
    """
    monkeypatch.setattr(EndpointRegistry, "_loaded", False)
    monkeypatch.setattr(EndpointRegistry, "_failed", ())
    monkeypatch.setattr(EndpointRegistry, "_entries", list(EndpointRegistry._entries))
    monkeypatch.setattr(EndpointRegistry, "_activation_failures", {})


def _warnings(caplog):
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


_ABSENT_DEPENDENCY = ModuleNotFoundError("No module named 'depx'", name="depx")


def test_a_listing_does_not_call_again_a_factory_that_failed_in_a_selection(
    monkeypatch, caplog, cold_registry
):
    calls = _failing_alpha(monkeypatch, ImportError("an internal module cannot be imported"))
    with caplog.at_level(logging.WARNING, logger=registry_module.logger.name):
        with pytest.raises(ProviderActivationError):
            match_endpoint("alpha", "a")
        EndpointRegistry.list_providers()
    # The listing neither calls the failed factory again nor repeats its warning; the provider's
    # other declaration and the healthy provider still load.
    assert calls == ["alpha/a", "alpha/b", "beta/x"]
    assert len(_warnings(caplog)) == 1
    assert EndpointRegistry.failed_providers() == [
        {"provider": "alpha", "endpoint": "a", "source": _SOURCE, "reason": "activation_failed"}
    ]


class _AlphaB(_Stub):
    """An endpoint class with the module and qualified name of the ``b`` declaration that
    ``_alpha_with_a_registering_b`` installs, so registering it under ``alpha`` is allowed."""


_AlphaB.__module__ = "stub_module_b"
_AlphaB.__qualname__ = "Endpoint"


def _alpha_with_a_registering_b(monkeypatch):
    """Install ``alpha``, whose ``a`` factory raises and whose ``b`` factory registers a real
    endpoint entry, beside a healthy ``beta``. Returns the factory call log."""
    monkeypatch.setattr(EndpointRegistry, "_alias_owners", dict(EndpointRegistry._alias_owners))
    calls = []

    def register_b():
        calls.append("alpha/b")
        if not any(entry.cls is _AlphaB for entry in EndpointRegistry._entries):
            EndpointRegistry.register("alpha", "b")(_AlphaB)
        return _AlphaB

    alpha = _fragment(
        "alpha",
        [
            _declaration("a", _raising(calls, "alpha/a", ImportError("an internal import failed"))),
            _declaration("b", register_b),
        ],
    )
    beta = _fragment("beta", [_declaration("x", _factory(calls, "beta/x"))])
    _install(monkeypatch, alpha, beta)
    return calls


@pytest.mark.parametrize("selected_first", [False, True], ids=["cold", "after-a-selection"])
def test_a_listing_lists_the_healthy_endpoint_of_a_provider_whose_other_endpoint_fails(
    monkeypatch, cold_registry, selected_first
):
    calls = _alpha_with_a_registering_b(monkeypatch)
    if selected_first:
        match_endpoint("alpha", "b")
    rows = [
        (row["provider"], row["endpoint"])
        for row in EndpointRegistry.list_providers()
        if row["provider"] == "alpha"
    ]
    # The rows do not depend on whether a selection activated ``alpha/b`` before the listing.
    assert rows == [("alpha", "b")]
    assert "alpha/b" in calls
    assert EndpointRegistry.failed_providers() == [
        {"provider": "alpha", "endpoint": "a", "source": _SOURCE, "reason": "activation_failed"}
    ]


def test_a_provider_whose_declarations_all_fail_has_one_failed_row_for_its_first(
    monkeypatch, cold_registry
):
    calls = []
    second = RuntimeError("the second factory has a defect")
    alpha = _fragment(
        "alpha",
        [
            _declaration("a", _raising(calls, "alpha/a", ImportError("an internal import failed"))),
            _declaration("b", _raising(calls, "alpha/b", second)),
        ],
    )
    _install(monkeypatch, alpha)
    assert EndpointRegistry.failed_providers() == [
        {"provider": "alpha", "endpoint": "a", "source": _SOURCE, "reason": "activation_failed"}
    ]
    assert calls == ["alpha/a", "alpha/b"]
    # The second failure is kept for its own declaration: a selection raises it without a
    # second call of its factory.
    with pytest.raises(ProviderActivationError) as raised:
        match_endpoint("alpha", "b")
    assert (raised.value.endpoint, raised.value.__cause__) == ("b", second)
    assert calls == ["alpha/a", "alpha/b"]


def test_a_failure_first_seen_by_a_listing_is_cached_for_later_selections(
    monkeypatch, caplog, cold_registry
):
    error = ImportError("an internal module cannot be imported")
    calls = _failing_alpha(monkeypatch, error)
    with caplog.at_level(logging.WARNING, logger=registry_module.logger.name):
        EndpointRegistry.list_providers()
        raised = []
        for _ in range(2):
            with pytest.raises(ProviderActivationError) as caught:
                match_endpoint("alpha", "a")
            raised.append(caught.value)
    assert calls == ["alpha/a", "alpha/b", "beta/x"]
    assert len(_warnings(caplog)) == 1
    assert all(failure.__cause__ is error for failure in raised)


@pytest.mark.parametrize(
    "error", [*DEFECTS.values(), _ABSENT_DEPENDENCY], ids=[*DEFECTS, "absent-declared-dependency"]
)
def test_failed_providers_reports_every_activation_failure_a_selection_translates(
    monkeypatch, cold_registry, error
):
    _failing_alpha(monkeypatch, error)
    rows = EndpointRegistry.failed_providers()
    with pytest.raises((ProviderActivationError, MissingFeatureError)) as raised:
        match_endpoint("alpha", "a")
    selected = raised.value
    # The listing reports the typed error a selection of the same endpoint raises.
    recorded = EndpointRegistry._failed[0].error
    assert type(recorded) is type(selected)
    assert recorded.__cause__ is selected.__cause__ is error
    assert rows == [
        {
            "provider": "alpha",
            "endpoint": selected.endpoint,
            "source": selected.source,
            "reason": selected.reason,
        }
    ]
    assert selected.endpoint == "a"
    assert selected.reason == (
        "missing_dependency" if error is _ABSENT_DEPENDENCY else "activation_failed"
    )


@pytest.mark.parametrize("error", DEFECTS.values(), ids=DEFECTS.keys())
def test_a_listing_completes_and_is_not_repeated_when_a_declaration_fails(
    monkeypatch, cold_registry, error
):
    calls = _failing_alpha(monkeypatch, error)
    rows = EndpointRegistry.list_providers()
    assert EndpointRegistry._loaded is True
    # The provider's other declaration and the healthy provider loaded, and the rows still
    # describe the providers that were registered.
    assert calls == ["alpha/a", "alpha/b", "beta/x"]
    assert rows == EndpointRegistry.list_providers()
    EndpointRegistry.failed_providers()
    assert calls == ["alpha/a", "alpha/b", "beta/x"]


def test_failed_providers_follow_catalog_order_whichever_failed_first_in_a_selection(
    monkeypatch, cold_registry
):
    calls = []
    failure = ImportError("an internal module cannot be imported")
    alpha = _fragment("alpha", [_declaration("a", _raising(calls, "alpha/a", failure))])
    beta = _fragment("beta", [_declaration("x", _factory(calls, "beta/x"))])
    gamma = _fragment("gamma", [_declaration("g", _raising(calls, "gamma/g", failure))])
    _install(monkeypatch, alpha, beta, gamma)
    with pytest.raises(ProviderActivationError):
        match_endpoint("gamma", "g")
    assert [row["provider"] for row in EndpointRegistry.failed_providers()] == ["alpha", "gamma"]
    assert calls == ["gamma/g", "alpha/a", "beta/x"]


class _NeedsDependency(_Stub):
    """An endpoint whose constructor imports a module that is not installed."""

    def __init__(self, config=None, **kwargs):
        raise ModuleNotFoundError("No module named 'depx'", name="depx")


class _NeedsOtherModule(_Stub):
    def __init__(self, config=None, **kwargs):
        raise ModuleNotFoundError("No module named 'other'", name="other")


class _RejectsConfiguration(_Stub):
    def __init__(self, config=None, **kwargs):
        raise ValueError("a configuration the endpoint rejects")


def _constructor_alpha(monkeypatch, cls):
    calls = []
    alpha = _fragment(
        "alpha", [_declaration("a", _factory(calls, "alpha/a", cls), requirements=(_DEPENDENCY,))]
    )
    _install(monkeypatch, alpha)


def test_a_declared_dependency_missing_in_the_constructor_raises_a_missing_feature_error(
    monkeypatch,
):
    _constructor_alpha(monkeypatch, _NeedsDependency)
    for _ in range(2):
        with pytest.raises(MissingFeatureError) as raised:
            match_endpoint("alpha", "a")
        failure = raised.value
        assert (failure.provider, failure.endpoint, failure.missing_distribution) == (
            "alpha",
            "a",
            "depx-dist",
        )
        assert failure.install_hint == "pip install lionagi[depx]"
        assert isinstance(failure.__cause__, ModuleNotFoundError)


@pytest.mark.parametrize(
    ("cls", "error_type"),
    [(_NeedsOtherModule, ModuleNotFoundError), (_RejectsConfiguration, ValueError)],
    ids=["undeclared-module", "invalid-configuration"],
)
def test_other_constructor_errors_keep_their_types(monkeypatch, cls, error_type):
    _constructor_alpha(monkeypatch, cls)
    with pytest.raises(error_type) as raised:
        match_endpoint("alpha", "a")
    assert type(raised.value) is error_type


def _empty_registry(monkeypatch):
    """Leave nothing registered, so a built-in name can only come from the catalog."""
    monkeypatch.setattr(EndpointRegistry, "_entries", [])
    monkeypatch.setattr(EndpointRegistry, "_alias_owners", {})


def _contract_version_error():
    return ProviderContractVersionError(source=_SOURCE, expected=(1,), observed=99)


def _manifest_activation_error():
    error = ProviderActivationError(
        provider="providers", endpoint=None, source=_SOURCE, reason="activation_failed"
    )
    error.__cause__ = SyntaxError("invalid syntax")
    return error


@pytest.mark.parametrize(
    ("make_error", "cause_type"),
    [(_contract_version_error, type(None)), (_manifest_activation_error, SyntaxError)],
    ids=["contract-version", "manifest-activation"],
)
@pytest.mark.parametrize(
    "extra",
    [{}, {"openai_compatible": True}, {"base_url": _CUSTOM_BASE_URL}],
    ids=["plain", "compatible", "base-url"],
)
def test_a_catalog_that_cannot_load_fails_the_selection_of_a_built_in_provider(
    monkeypatch, make_error, cause_type, extra
):
    _empty_registry(monkeypatch)

    def cannot_load():
        raise make_error()

    monkeypatch.setattr(_provider_compat, "default_provider_catalog", cannot_load)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(type(make_error())) as raised:
            match_endpoint("openai", "", **extra)
    assert type(raised.value) is type(make_error())
    assert type(raised.value.__cause__) is cause_type


def _missing_hub(monkeypatch):
    """Leave nothing registered and make the bundled provider hub absent."""
    _empty_registry(monkeypatch)

    def cannot_load():
        error = MissingFeatureError(
            feature="providers",
            missing_distribution="lionagi",
            install_hint="reinstall the matching lionagi release",
            source="the manifest module",
            import_root="lionhub",
        )
        error.__cause__ = ModuleNotFoundError("No module named 'lionhub'", name="lionhub")
        raise error

    monkeypatch.setattr(_provider_compat, "default_provider_catalog", cannot_load)


def test_a_missing_hub_names_the_requested_provider(monkeypatch):
    _missing_hub(monkeypatch)
    with pytest.raises(MissingFeatureError) as raised:
        match_endpoint("OpenAI", "")
    failure = raised.value
    assert (failure.feature, failure.provider, failure.missing_distribution) == (
        "providers",
        "OpenAI",
        "lionagi",
    )
    assert failure.name == "lionhub"
    assert isinstance(failure.__cause__, ModuleNotFoundError)


def test_a_missing_hub_fails_a_custom_host_that_does_not_ask_for_the_generic_endpoint(
    monkeypatch,
):
    _missing_hub(monkeypatch)
    with pytest.raises(MissingFeatureError) as raised:
        match_endpoint("my-own-host", "chat/completions")
    assert raised.value.provider == "my-own-host"


@pytest.mark.parametrize(
    "extra",
    [{"openai_compatible": True}, {"base_url": _CUSTOM_BASE_URL}],
    ids=["compatible", "base-url"],
)
def test_a_missing_hub_fails_a_request_that_asks_for_the_generic_endpoint(monkeypatch, extra):
    # A 1.x installation without the hub is incomplete, and the generic OpenAI-compatible
    # endpoint does not hide that: a custom host that asks for it with ``openai_compatible=True``
    # or ``base_url=`` gets the error a built-in name gets, and no routing warning is issued.
    _missing_hub(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(MissingFeatureError) as raised:
            match_endpoint("my-own-host", "chat/completions", **extra)
    failure = raised.value
    assert (failure.feature, failure.provider, failure.missing_distribution) == (
        "providers",
        "my-own-host",
        "lionagi",
    )
    assert "reinstall the matching lionagi release" in failure.install_hint
    assert failure.name == "lionhub"
    assert isinstance(failure.__cause__, ModuleNotFoundError)


@pytest.mark.parametrize(
    "extra",
    [{}, {"openai_compatible": True}, {"base_url": _CUSTOM_BASE_URL}],
    ids=["plain", "compatible", "base-url"],
)
def test_a_provider_registered_by_the_caller_resolves_when_the_hub_is_missing(monkeypatch, extra):
    _missing_hub(monkeypatch)

    class Registered(_Stub):
        pass

    EndpointRegistry.register(provider="lazy-pin-provider", endpoint="chat")(Registered)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert type(match_endpoint("lazy-pin-provider", "", **extra)) is Registered


def test_a_provider_registered_by_the_caller_resolves_when_the_catalog_cannot_load(monkeypatch):
    _empty_registry(monkeypatch)

    def cannot_load():
        raise _contract_version_error()

    monkeypatch.setattr(_provider_compat, "default_provider_catalog", cannot_load)

    class Registered(_Stub):
        pass

    EndpointRegistry.register(provider="lazy-pin-provider", endpoint="chat")(Registered)
    assert type(match_endpoint("lazy-pin-provider", "")) is Registered
