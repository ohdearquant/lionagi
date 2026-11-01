# SPDX-License-Identifier: Apache-2.0

"""Provider catalog contract types and their composition.

Rules the composition mirrors, as the registry applies them today:

* ``EndpointRegistry.register`` stores ``strip().lower()`` of a provider key and of every
  provider alias, and drops repeated aliases.
* ``EndpointRegistry._match_registered`` lowercases the provider it is asked for and does not
  strip it; endpoint names are compared exactly, against the endpoint key and its aliases.
  An empty endpoint name selects the provider's first entry (the catalog uses the provider's
  default endpoint), and an unmatched non-empty name selects the provider's entry when it has
  exactly one.
* ``token_budget._longest_prefix_match`` returns the window of the longest key contained in the
  lowercased model name; the first key declared wins a tie.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from lionagi._provider_errors import ProviderCollisionError, ProviderContractVersionError
from lionagi.service.connections import provider_catalog
from lionagi.service.connections.provider_catalog import (
    SUPPORTED_CONTRACT_VERSIONS,
    DependencyRequirement,
    EndpointDeclaration,
    ProviderCatalog,
    ProviderFragment,
    ProviderManifest,
    compose_provider_catalog,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _factory():
    raise AssertionError("the catalog must never call an endpoint factory")


def make_requirement(**overrides):
    fields = {
        "distribution": "example-dist",
        "import_roots": ("example_root",),
        "version_constraint": None,
        "install_hint": "pip install example-dist",
    }
    fields.update(overrides)
    return DependencyRequirement(**fields)


def make_endpoint(**overrides):
    fields = {
        "key": "chat",
        "aliases": (),
        "kind": "api",
        "factory": _factory,
        "requirements": (),
        "settings": {},
        "diagnostic_id": "example.chat",
    }
    fields.update(overrides)
    return EndpointDeclaration(**fields)


def make_fragment(**overrides):
    fields = {
        "contract_version": 1,
        "key": "alpha",
        "aliases": (),
        "endpoints": (make_endpoint(),),
        "default_endpoint": "chat",
        "context_windows": None,
    }
    fields.update(overrides)
    return ProviderFragment(**fields)


def make_manifest(**overrides):
    fields = {"contract_version": 1, "source": "src", "fragments": (make_fragment(),)}
    fields.update(overrides)
    return ProviderManifest(**fields)


def compose(*fragments):
    return compose_provider_catalog(fragments, source="src")


def collision(*fragments):
    with pytest.raises(ProviderCollisionError) as info:
        compose(*fragments)
    error = info.value
    assert error.first_owner in str(error)
    assert error.second_owner in str(error)
    return error


# --- immutability ------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "field"),
    [
        (make_requirement, "distribution"),
        (make_endpoint, "key"),
        (make_fragment, "key"),
        (make_manifest, "source"),
    ],
)
def test_declaration_types_reject_assignment_and_have_no_instance_dict(make, field):
    declaration = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(declaration, field, "changed")
    assert not hasattr(declaration, "__dict__")


def test_catalog_rejects_assignment():
    catalog = compose(make_fragment())
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.source = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.providers = ()
    assert isinstance(catalog.providers, tuple)


def test_endpoint_settings_are_read_only_and_copied():
    original = {"timeout": 5}
    declaration = make_endpoint(settings=original)
    assert isinstance(declaration.settings, types.MappingProxyType)
    with pytest.raises(TypeError):
        declaration.settings["timeout"] = 6
    with pytest.raises(TypeError):
        declaration.settings["added"] = 1
    original["timeout"] = 99
    original["added"] = 1
    assert dict(declaration.settings) == {"timeout": 5}


def test_context_windows_are_read_only_and_copied():
    original = {"model-a": 100}
    fragment = make_fragment(context_windows=original)
    assert isinstance(fragment.context_windows, types.MappingProxyType)
    with pytest.raises(TypeError):
        fragment.context_windows["model-a"] = 1
    with pytest.raises(TypeError):
        fragment.context_windows["model-b"] = 2
    original["model-a"] = 1
    original["model-b"] = 2
    assert dict(fragment.context_windows) == {"model-a": 100}


# --- shape validation --------------------------------------------------------


def test_tuple_fields_are_stored_as_tuples():
    requirement = make_requirement(import_roots=["one", "two"])
    endpoint = make_endpoint(aliases=["a", "b"], requirements=[requirement])
    fragment = make_fragment(aliases=["x"], endpoints=[endpoint])
    manifest = make_manifest(fragments=[fragment])
    assert requirement.import_roots == ("one", "two")
    assert endpoint.aliases == ("a", "b")
    assert endpoint.requirements == (requirement,)
    assert fragment.aliases == ("x",)
    assert fragment.endpoints == (endpoint,)
    assert manifest.fragments == (fragment,)


@pytest.mark.parametrize(
    ("make", "overrides", "error", "field"),
    [
        (make_requirement, {"distribution": ""}, ValueError, "distribution"),
        (make_requirement, {"distribution": 3}, TypeError, "distribution"),
        (make_requirement, {"import_roots": "pkg"}, TypeError, "import_roots"),
        (make_requirement, {"import_roots": ("",)}, ValueError, "import_roots"),
        (make_requirement, {"version_constraint": 5}, TypeError, "version_constraint"),
        (make_requirement, {"install_hint": None}, TypeError, "install_hint"),
        (make_endpoint, {"key": ""}, ValueError, "key"),
        (make_endpoint, {"key": None}, TypeError, "key"),
        (make_endpoint, {"aliases": "x"}, TypeError, "aliases"),
        (make_endpoint, {"aliases": ("",)}, ValueError, "aliases"),
        (make_endpoint, {"kind": "other"}, ValueError, "kind"),
        (make_endpoint, {"factory": "not callable"}, TypeError, "factory"),
        (make_endpoint, {"requirements": ("x",)}, TypeError, "requirements"),
        (make_endpoint, {"settings": []}, TypeError, "settings"),
        (make_endpoint, {"diagnostic_id": ""}, ValueError, "diagnostic_id"),
        (make_fragment, {"key": "  "}, ValueError, "key"),
        (make_fragment, {"endpoints": ()}, ValueError, "endpoints"),
        (make_fragment, {"endpoints": ("x",)}, TypeError, "endpoints"),
        (make_fragment, {"default_endpoint": "missing"}, ValueError, "default_endpoint"),
        (make_fragment, {"context_windows": []}, TypeError, "context_windows"),
        (make_fragment, {"context_windows": {"": 1}}, ValueError, "context_windows"),
        (make_manifest, {"source": ""}, ValueError, "source"),
        (make_manifest, {"fragments": ("x",)}, TypeError, "fragments"),
    ],
)
def test_construction_rejects_bad_shape_naming_the_field(make, overrides, error, field):
    with pytest.raises(error, match=field):
        make(**overrides)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "100"])
def test_context_window_values_must_be_positive_ints(bad):
    with pytest.raises((TypeError, ValueError), match="context_windows"):
        make_fragment(context_windows={"model-a": bad})


@pytest.mark.parametrize("version", [None, "1", True, 2, 0])
def test_fragment_and_manifest_construction_do_not_check_contract_versions(version):
    assert make_fragment(contract_version=version).contract_version is version
    assert make_manifest(contract_version=version).contract_version is version


# --- contract versions ---------------------------------------------------------


@pytest.mark.parametrize("observed", [None, "1", True, 2, 0, 1.0])
def test_unsupported_fragment_version_raises_version_error(observed):
    with pytest.raises(ProviderContractVersionError) as info:
        compose(make_fragment(contract_version=observed))
    assert info.value.observed is observed
    assert info.value.source == "src"
    assert info.value.expected == SUPPORTED_CONTRACT_VERSIONS


@pytest.mark.parametrize("observed", [None, "1", True, 2])
def test_unsupported_manifest_version_raises_version_error(observed):
    manifest = make_manifest(contract_version=observed)
    with pytest.raises(ProviderContractVersionError) as info:
        compose_provider_catalog(manifest, source="src")
    assert info.value.observed is observed
    assert info.value.expected == SUPPORTED_CONTRACT_VERSIONS


def test_manifest_and_fragment_versions_must_agree():
    manifest = make_manifest(contract_version=1, fragments=(make_fragment(contract_version=2),))
    with pytest.raises(ProviderContractVersionError) as info:
        compose_provider_catalog(manifest, source="src")
    assert info.value.observed == 2


def test_disagreement_between_supported_versions_names_the_manifest_version(monkeypatch):
    monkeypatch.setattr(provider_catalog, "SUPPORTED_CONTRACT_VERSIONS", (1, 2))
    manifest = make_manifest(contract_version=1, fragments=(make_fragment(contract_version=2),))
    with pytest.raises(ProviderContractVersionError) as info:
        compose_provider_catalog(manifest, source="src")
    assert info.value.observed == 2
    assert info.value.expected == (1,)


def test_versions_are_checked_before_collisions():
    fragments = (make_fragment(), make_fragment(), make_fragment(key="beta", contract_version="1"))
    with pytest.raises(ProviderContractVersionError) as info:
        compose(*fragments)
    assert info.value.observed == "1"


def test_manifest_version_is_checked_before_collisions():
    manifest = make_manifest(contract_version=2, fragments=(make_fragment(), make_fragment()))
    with pytest.raises(ProviderContractVersionError) as info:
        compose_provider_catalog(manifest, source="src")
    assert info.value.observed == 2


@pytest.mark.parametrize("observed", [None, "1", True, 1.0, 2])
def test_direct_catalog_construction_checks_every_fragment_version(observed):
    fragments = (make_fragment(), make_fragment(key="beta", contract_version=observed))
    with pytest.raises(ProviderContractVersionError) as info:
        ProviderCatalog(providers=fragments, source="direct-src")
    assert info.value.source == "direct-src"
    assert info.value.observed is observed
    assert info.value.expected == SUPPORTED_CONTRACT_VERSIONS


def test_direct_catalog_construction_checks_versions_before_collisions():
    fragments = (make_fragment(), make_fragment(), make_fragment(key="beta", contract_version="1"))
    with pytest.raises(ProviderContractVersionError) as info:
        ProviderCatalog(providers=fragments, source="src")
    assert info.value.observed == "1"


def test_direct_catalog_construction_checks_versions_before_repeated_declarations():
    repeated = make_fragment(contract_version=2)
    with pytest.raises(ProviderContractVersionError) as info:
        ProviderCatalog(providers=(repeated, repeated), source="src")
    assert info.value.observed == 2


def test_replacing_the_providers_of_a_catalog_checks_versions_again():
    catalog = compose(make_fragment())
    with pytest.raises(ProviderContractVersionError) as info:
        dataclasses.replace(catalog, providers=(make_fragment(contract_version=2),))
    assert info.value.observed == 2
    assert info.value.source == "src"


def test_manifest_source_is_used_and_must_match_the_keyword():
    manifest = make_manifest(source="manifest-src")
    catalog = compose_provider_catalog(manifest, source="manifest-src")
    assert catalog.source == "manifest-src"
    assert [fragment.key for fragment in catalog.providers] == ["alpha"]
    with pytest.raises(ValueError, match="source"):
        compose_provider_catalog(manifest, source="other")


@pytest.mark.parametrize(
    ("manifest_version", "fragment_version", "observed"),
    [(None, 1, None), (2, 1, 2), (1, "1", "1"), (1, 2, 2)],
)
def test_versions_are_checked_before_the_manifest_source_is_compared(
    manifest_version, fragment_version, observed
):
    manifest = make_manifest(
        contract_version=manifest_version,
        source="manifest-src",
        fragments=(make_fragment(contract_version=fragment_version),),
    )
    with pytest.raises(ProviderContractVersionError) as info:
        compose_provider_catalog(manifest, source="other")
    assert info.value.observed == observed


def test_version_disagreement_is_reported_before_the_manifest_source_is_compared(monkeypatch):
    monkeypatch.setattr(provider_catalog, "SUPPORTED_CONTRACT_VERSIONS", (1, 2))
    manifest = make_manifest(
        contract_version=1,
        source="manifest-src",
        fragments=(make_fragment(contract_version=2),),
    )
    with pytest.raises(ProviderContractVersionError) as info:
        compose_provider_catalog(manifest, source="other")
    assert info.value.observed == 2
    assert info.value.expected == (1,)


def test_compose_rejects_bad_input_shapes():
    with pytest.raises(TypeError, match="fragments"):
        compose_provider_catalog([object()], source="src")
    with pytest.raises(TypeError, match="fragments"):
        compose_provider_catalog(make_fragment(), source="src")
    with pytest.raises(ValueError, match="source"):
        compose_provider_catalog([], source="")
    with pytest.raises(TypeError):
        compose_provider_catalog([], "src")


# --- collisions ----------------------------------------------------------------


def _assert_owners(error, key, kind):
    assert error.kind == kind
    assert error.key == key
    assert error.first_owner != error.second_owner
    for owner in (error.first_owner, error.second_owner):
        assert "src" in owner


def test_same_provider_key_twice_is_a_collision():
    error = collision(make_fragment(), make_fragment())
    _assert_owners(error, "alpha", "provider")
    assert "alpha" in error.first_owner
    assert "alpha" in error.second_owner


def test_provider_keys_that_normalize_alike_collide():
    error = collision(make_fragment(key="  Alpha "), make_fragment(key="alpha"))
    _assert_owners(error, "alpha", "provider")


def test_provider_alias_claimed_twice_is_a_collision():
    error = collision(
        make_fragment(aliases=("shared",)),
        make_fragment(key="beta", aliases=("Shared",)),
    )
    _assert_owners(error, "shared", "provider_alias")


@pytest.mark.parametrize("alias_first", [True, False])
def test_provider_alias_equal_to_another_provider_key_is_a_collision(alias_first):
    claimer = make_fragment(key="beta", aliases=("alpha",))
    holder = make_fragment()
    error = collision(*((claimer, holder) if alias_first else (holder, claimer)))
    _assert_owners(error, "alpha", "provider_alias")


def test_endpoint_key_twice_inside_one_provider_is_a_collision():
    fragment = make_fragment(
        endpoints=(make_endpoint(), make_endpoint(diagnostic_id="example.chat.other"))
    )
    _assert_owners(collision(fragment), "chat", "endpoint")


def test_endpoint_alias_equal_to_another_endpoint_key_is_a_collision():
    fragment = make_fragment(
        endpoints=(make_endpoint(), make_endpoint(key="stream", aliases=("chat",)))
    )
    _assert_owners(collision(fragment), "chat", "endpoint")


def test_endpoint_alias_claimed_twice_is_a_collision():
    fragment = make_fragment(
        endpoints=(
            make_endpoint(aliases=("fast",)),
            make_endpoint(key="stream", aliases=("fast",)),
        )
    )
    _assert_owners(collision(fragment), "fast", "endpoint")


def test_same_endpoint_name_under_two_providers_is_allowed():
    catalog = compose(make_fragment(), make_fragment(key="beta"))
    assert [fragment.key for fragment in catalog.providers] == ["alpha", "beta"]


def test_same_fragment_object_repeated_is_a_duplicate_declaration():
    fragment = make_fragment()
    _assert_owners(collision(fragment, fragment), "alpha", "duplicate_declaration")


def test_same_endpoint_object_repeated_is_a_duplicate_declaration():
    endpoint = make_endpoint()
    error = collision(make_fragment(endpoints=(endpoint, endpoint)))
    _assert_owners(error, "chat", "duplicate_declaration")


def test_duplicate_declarations_are_reported_before_key_collisions():
    fragment = make_fragment()
    error = collision(fragment, make_fragment(), fragment)
    _assert_owners(error, "alpha", "duplicate_declaration")


# --- key normalization and lookup ----------------------------------------------


def test_keys_are_normalized_as_the_registry_registers_them():
    fragment = make_fragment(key="  Alpha ", aliases=(" A1 ", "a1", "B2"))
    catalog = compose(fragment)
    assert catalog.providers == (fragment,)
    assert catalog.fragment("alpha") is fragment
    assert catalog.fragment("a1") is fragment
    assert catalog.fragment("b2") is fragment
    assert catalog.fragment("ALPHA") is fragment
    assert catalog.fragment("  alpha ") is None
    assert catalog.fragment(" a1") is None
    assert catalog.fragment("missing") is None


def test_endpoint_lookup_lowercases_the_provider_and_matches_endpoint_names_exactly():
    chat = make_endpoint(aliases=("Fast",))
    stream = make_endpoint(key="stream", diagnostic_id="example.stream")
    fragment = make_fragment(aliases=("a1",), endpoints=(chat, stream), default_endpoint="stream")
    catalog = compose(fragment)
    assert catalog.endpoint("alpha") is stream
    assert catalog.endpoint("a1") is stream
    assert catalog.endpoint("ALPHA", "chat") is chat
    assert catalog.endpoint("alpha", "Fast") is chat
    assert catalog.endpoint("alpha", "CHAT") is None
    assert catalog.endpoint("alpha", "fast") is None
    assert catalog.endpoint("alpha", " chat") is None
    assert catalog.endpoint("missing") is None
    assert catalog.endpoint("missing", "chat") is None
    assert catalog.endpoint("  alpha ") is None


def test_empty_endpoint_name_selects_the_default_endpoint():
    chat = make_endpoint()
    stream = make_endpoint(key="stream", diagnostic_id="example.stream")
    catalog = compose(make_fragment(endpoints=(chat, stream), default_endpoint="stream"))
    assert catalog.endpoint("alpha", "") is stream
    assert catalog.endpoint("alpha", None) is stream
    assert catalog.endpoint("alpha") is stream


def test_unmatched_endpoint_name_on_a_one_endpoint_provider_returns_its_endpoint():
    chat = make_endpoint(aliases=("fast",))
    catalog = compose(make_fragment(endpoints=(chat,)))
    assert catalog.endpoint("alpha", "missing") is chat
    assert catalog.endpoint("alpha", "CHAT") is chat
    assert catalog.endpoint("alpha", " chat") is chat
    assert catalog.endpoint("missing", "chat") is None


def test_unmatched_endpoint_name_on_a_two_endpoint_provider_returns_none():
    chat = make_endpoint()
    stream = make_endpoint(key="stream", diagnostic_id="example.stream")
    catalog = compose(make_fragment(endpoints=(chat, stream)))
    assert catalog.endpoint("alpha", "missing") is None
    assert catalog.endpoint("alpha", "CHAT") is None


def test_exact_key_and_alias_still_match_on_a_one_endpoint_provider():
    chat = make_endpoint(aliases=("fast",))
    catalog = compose(make_fragment(endpoints=(chat,)))
    assert catalog.endpoint("alpha", "chat") is chat
    assert catalog.endpoint("alpha", "fast") is chat


def test_same_endpoint_name_resolves_per_provider():
    first = make_endpoint()
    second = make_endpoint(diagnostic_id="example.beta.chat")
    catalog = compose(
        make_fragment(endpoints=(first,)),
        make_fragment(key="beta", endpoints=(second,)),
    )
    assert catalog.endpoint("alpha", "chat") is first
    assert catalog.endpoint("beta", "chat") is second


def test_composition_and_lookup_never_call_the_factory():
    catalog = compose(make_fragment())
    assert catalog.endpoint("alpha").factory is _factory


# --- context windows -------------------------------------------------------------


def test_context_window_longest_key_contained_in_the_lowercased_model_wins():
    catalog = compose(make_fragment(context_windows={"m": 1, "model-a-long": 3, "model-a": 2}))
    assert catalog.context_window("MODEL-A-LONG-v2") == 3
    assert catalog.context_window("prefix-model-a-suffix") == 2
    assert catalog.context_window("xm") == 1


def test_context_window_first_declared_key_wins_a_length_tie():
    catalog = compose(make_fragment(context_windows={"ab": 1, "bc": 2}))
    assert catalog.context_window("abc") == 1


def test_context_window_tries_the_hinted_provider_first_then_declared_order():
    catalog = compose(
        make_fragment(aliases=("a1",), context_windows={"gpt": 100}),
        make_fragment(key="beta", context_windows={"gpt": 200, "zeta": 7}),
    )
    assert catalog.context_window("gpt-x") == 100
    assert catalog.context_window("gpt-x", provider="beta") == 200
    assert catalog.context_window("gpt-x", provider="BETA") == 200
    assert catalog.context_window("gpt-x", provider="a1") == 100
    assert catalog.context_window("zeta-1", provider="alpha") == 7
    assert catalog.context_window("gpt-x", provider="unknown") == 100


def test_context_window_is_none_without_a_match_and_never_a_default():
    catalog = compose(
        make_fragment(context_windows={"model-a": 100}),
        make_fragment(key="beta", context_windows=None),
    )
    assert catalog.context_window("other-model") is None
    assert catalog.context_window("other-model", provider="beta") is None
    assert catalog.context_window("model-a", provider="beta") == 100


def test_context_window_is_none_when_no_fragment_declares_windows():
    catalog = compose(make_fragment(), make_fragment(key="beta"))
    assert catalog.context_window("any-model") is None
    assert catalog.context_window("any-model", provider="alpha") is None


# --- empty catalog and repeatable composition ------------------------------------


def test_empty_catalog_is_valid():
    catalog = compose_provider_catalog([], source="src")
    assert isinstance(catalog, ProviderCatalog)
    assert catalog.providers == ()
    assert catalog.source == "src"
    assert catalog.fragment("alpha") is None
    assert catalog.endpoint("alpha") is None
    assert catalog.context_window("any-model") is None


def test_providers_keep_declared_order():
    catalog = compose(make_fragment(key="beta"), make_fragment())
    assert [fragment.key for fragment in catalog.providers] == ["beta", "alpha"]


def test_composing_identical_input_twice_gives_equal_catalogs():
    def build():
        return compose(
            make_fragment(aliases=("a1",), context_windows={"m": 5}),
            make_fragment(key="beta"),
        )

    assert build() == build()
    manifest = make_manifest(fragments=(make_fragment(), make_fragment(key="beta")))
    assert compose_provider_catalog(manifest, source="src") == compose(*manifest.fragments)


# --- isolation -------------------------------------------------------------------

_ISOLATION_SCRIPT = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("isolated_provider_catalog", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(json.dumps(sorted(sys.modules)))
"""


def test_loading_the_module_imports_no_hub_and_no_sibling_connection_module():
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT, str(Path(provider_catalog.__file__))],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert [name for name in loaded if name.split(".")[0] == "lionhub"] == []
    assert [name for name in loaded if name.startswith("lionagi.service.connections")] == []


def test_module_imports_only_the_standard_library_and_the_provider_errors():
    tree = ast.parse(Path(provider_catalog.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not allowed"
            imported.append(node.module)
    assert imported, "expected at least one import"
    for name in imported:
        assert name == "lionagi._provider_errors" or name.split(".")[0] in sys.stdlib_module_names
