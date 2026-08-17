# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Executable RED contract for ADR-0119 immutable registry composition."""

from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from types import ModuleType
from typing import Any, ClassVar, TypeVar

import pytest

from lionagi.ln import json_dumpb
from lionagi.ln.types import DataClass, Params, UnhashableStructuralValueError, Unset


def _api() -> ModuleType:
    """Load the physical authority so semantic failures are independent of re-exports."""
    return importlib.import_module("lionagi.ln.types.registry")


def _entry(api: ModuleType, key: str, value: str | None = None):
    return api.RegistryEntry(key=key, value=value if value is not None else key.upper())


def _fragment(
    api: ModuleType,
    owner: str,
    *keys: str,
    version: str = "1",
    feature: Any = Unset,
):
    return api.RegistryFragment(
        owner=owner,
        items=tuple(_entry(api, key) for key in keys),
        version=version,
        feature=feature,
    )


def _keys(snapshot: Any) -> tuple[str, ...]:
    return tuple(record.entry.key for record in snapshot.items)


def _values(snapshot: Any) -> tuple[Any, ...]:
    return tuple(record.entry.value for record in snapshot.items)


def test_composition_preserves_explicit_fragment_and_item_order():
    api = _api()
    core = _fragment(api, "core", "alpha", "beta")
    feature = _fragment(api, "feature", "gamma", "delta")

    snapshot = api.Registry.compose(core, feature, name="widgets", version="1")

    assert snapshot.name == "widgets"
    assert snapshot.fragments == (core, feature)
    assert _keys(snapshot) == ("alpha", "beta", "gamma", "delta")
    assert _values(snapshot) == ("ALPHA", "BETA", "GAMMA", "DELTA")
    assert snapshot.overrides == ()
    assert snapshot.version == "1"
    assert all(isinstance(record, api.RegistryRecord) for record in snapshot.items)
    assert tuple(record.owner for record in snapshot.items) == (
        "core",
        "core",
        "feature",
        "feature",
    )
    assert tuple(record.fragment_version for record in snapshot.items) == (
        "1",
        "1",
        "1",
        "1",
    )
    assert all(record.feature is Unset for record in snapshot.items)


def test_snapshot_and_fragments_are_immutable_structural_values():
    api = _api()
    core = _fragment(api, "core", "alpha")
    snapshot = api.Registry.compose(core, name="widgets", version="1")
    original_hash = hash(snapshot)

    with pytest.raises((FrozenInstanceError, AttributeError)):
        snapshot.items = ()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        core.items = ()

    assert hash(snapshot) == original_hash
    assert snapshot == api.Registry.compose(core, name="widgets", version="1")


def test_duplicate_key_error_names_key_and_both_fragment_owners_deterministically():
    api = _api()
    core = _fragment(api, "core", "alpha", version="1")
    feature = _fragment(api, "feature", "alpha", version="2")

    def collide() -> str:
        with pytest.raises(api.DuplicateRegistryKeyError) as exc_info:
            api.Registry.compose(core, feature, name="widgets", version="3")
        return str(exc_info.value)

    first = collide()
    second = collide()

    assert first == second
    assert all(token in first for token in ("alpha", "core", "feature"))


def test_duplicate_key_within_one_fragment_reports_both_item_positions():
    api = _api()
    fragment = api.RegistryFragment(
        owner="core",
        version="1",
        items=(_entry(api, "alpha", "FIRST"), _entry(api, "alpha", "SECOND")),
    )

    with pytest.raises(api.DuplicateRegistryKeyError) as exc_info:
        api.Registry.compose(fragment, name="widgets", version="1")

    message = str(exc_info.value)
    assert all(token in message for token in ("alpha", "core", "item 0", "item 1"))


def test_repeated_fragment_owner_fails_even_when_keys_do_not_overlap():
    api = _api()
    first = _fragment(api, "core", "alpha")
    second = _fragment(api, "core", "beta", version="2")

    with pytest.raises(api.DuplicateRegistryOwnerError) as exc_info:
        api.Registry.compose(first, second, name="widgets", version="1")

    assert all(token in str(exc_info.value) for token in ("core", "fragment 0", "fragment 1"))


def test_exact_versioned_override_replaces_in_place_and_records_provenance():
    api = _api()
    core_alpha = _entry(api, "alpha", "A1")
    feature_alpha = _entry(api, "alpha", "A2")
    core = api.RegistryFragment(
        owner="core",
        items=(core_alpha, _entry(api, "beta", "B1")),
        version="1",
    )
    feature = api.RegistryFragment(
        owner="feature",
        items=(feature_alpha, _entry(api, "gamma", "G1")),
        version="2",
    )
    rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="3",
        rule_version="widgets-alpha-v1",
    )

    class VersionedWidgetRegistry(api.Registry[str]):
        override_rules = (rule,)

    snapshot = VersionedWidgetRegistry.compose(
        core,
        feature,
        name="widgets",
        version="3",
    )

    assert _keys(snapshot) == ("alpha", "beta", "gamma")
    assert _values(snapshot) == ("A2", "B1", "G1")
    assert len(snapshot.overrides) == 1
    override = snapshot.overrides[0]
    assert isinstance(override, api.RegistryOverride)
    assert override.rule == rule
    assert override.displaced == core_alpha
    assert override.replacement == feature_alpha
    assert snapshot.items[0].owner == "feature"
    assert snapshot.items[0].fragment_version == "2"
    assert b"widgets-alpha-v1" in json_dumpb(snapshot.to_dict(mode="json"))


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    (
        ("incumbent_owner", "other-core"),
        ("incumbent_fragment_version", "999"),
        ("replacement_owner", "other-feature"),
        ("replacement_fragment_version", "999"),
        ("registry_version", "999"),
    ),
)
def test_override_rule_must_match_both_owners_and_versions_exactly(field, wrong_value):
    api = _api()
    core = _fragment(api, "core", "alpha", version="1")
    feature = _fragment(api, "feature", "alpha", version="2")
    rule_values = {
        "key": "alpha",
        "incumbent_owner": "core",
        "incumbent_fragment_version": "1",
        "replacement_owner": "feature",
        "replacement_fragment_version": "2",
        "registry_version": "3",
        "rule_version": "widgets-alpha-v1",
    }
    rule_values[field] = wrong_value
    rule = api.RegistryOverrideRule(**rule_values)

    class MismatchedRegistry(api.Registry[str]):
        override_rules = (rule,)

    with pytest.raises(api.DuplicateRegistryKeyError):
        MismatchedRegistry.compose(core, feature, name="widgets", version="3")


def test_ambiguous_matching_override_rules_fail_closed():
    api = _api()
    rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="3",
        rule_version="widgets-alpha-v1",
    )

    class AmbiguousRegistry(api.Registry[str]):
        override_rules = (rule, rule)

    with pytest.raises(api.AmbiguousRegistryOverrideError, match="alpha"):
        AmbiguousRegistry.compose(
            _fragment(api, "core", "alpha", version="1"),
            _fragment(api, "feature", "alpha", version="2"),
            name="widgets",
            version="3",
        )


def test_unversioned_registry_cannot_apply_an_override_rule():
    api = _api()
    rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="3",
        rule_version="widgets-alpha-v1",
    )

    class VersionedRegistry(api.Registry[str]):
        override_rules = (rule,)

    with pytest.raises(api.DuplicateRegistryKeyError):
        VersionedRegistry.compose(
            _fragment(api, "core", "alpha", version="1"),
            _fragment(api, "feature", "alpha", version="2"),
            name="widgets",
        )


def test_chained_overrides_preserve_the_first_slot_and_ordered_history():
    api = _api()
    first_rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="4",
        rule_version="core-to-feature-v1",
    )
    second_rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="feature",
        incumbent_fragment_version="2",
        replacement_owner="plugin",
        replacement_fragment_version="3",
        registry_version="4",
        rule_version="feature-to-plugin-v1",
    )

    class ChainedRegistry(api.Registry[str]):
        override_rules = (first_rule, second_rule)

    snapshot = ChainedRegistry.compose(
        _fragment(api, "core", "alpha", "beta", version="1"),
        _fragment(api, "feature", "alpha", "gamma", version="2"),
        _fragment(api, "plugin", "alpha", "delta", version="3"),
        name="widgets",
        version="4",
    )

    assert _keys(snapshot) == ("alpha", "beta", "gamma", "delta")
    assert snapshot.items[0].owner == "plugin"
    assert tuple(item.rule.rule_version for item in snapshot.overrides) == (
        "core-to-feature-v1",
        "feature-to-plugin-v1",
    )


def test_equal_value_override_still_retains_both_entries_and_source_history():
    api = _api()
    incumbent = _entry(api, "alpha", "SAME")
    replacement = _entry(api, "alpha", "SAME")
    core = api.RegistryFragment(
        owner="core",
        version="1",
        items=(incumbent,),
        feature=Unset,
    )
    feature = api.RegistryFragment(
        owner="feature",
        version="2",
        items=(replacement,),
        feature="optional-widget",
    )
    rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="3",
        rule_version="same-value-history-v1",
    )

    class SameValueRegistry(api.Registry[str]):
        override_rules = (rule,)

    snapshot = SameValueRegistry.compose(core, feature, name="widgets", version="3")
    history = snapshot.overrides[0]
    history_projection = history.to_dict(mode="json")

    assert history.displaced == incumbent
    assert history.replacement == replacement
    assert set(history_projection) >= {"rule", "displaced", "replacement"}
    assert snapshot.items[0].owner == "feature"
    assert snapshot.items[0].feature == "optional-widget"


def test_override_rules_are_registry_subclass_classvars_not_snapshot_input():
    api = _api()
    rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="3",
        rule_version="classvar-v1",
    )

    class DeclaredRegistry(api.Registry[str]):
        override_rules = (rule,)

    assert DeclaredRegistry.override_rules == (rule,)
    assert "override_rules" not in DeclaredRegistry.field_names()
    with pytest.raises(TypeError):
        api.Registry.compose(
            _fragment(api, "core", "alpha"),
            name="widgets",
            version="1",
            override_rules=(rule,),
        )


def test_registry_subtype_rules_are_frozen_when_the_subtype_is_created():
    api = _api()
    rule = api.RegistryOverrideRule(
        key="alpha",
        incumbent_owner="core",
        incumbent_fragment_version="1",
        replacement_owner="feature",
        replacement_fragment_version="2",
        registry_version="3",
        rule_version="frozen-rule-v1",
    )

    class FrozenRuleRegistry(api.Registry[str]):
        override_rules = (rule,)

    with pytest.raises(TypeError, match="override_rules"):
        setattr(FrozenRuleRegistry, "override_rules", ())
    with pytest.raises(TypeError, match="override_rules"):
        delattr(FrozenRuleRegistry, "override_rules")

    snapshot = FrozenRuleRegistry.compose(
        _fragment(api, "core", "alpha", version="1"),
        _fragment(api, "feature", "alpha", version="2"),
        name="widgets",
        version="3",
    )
    assert _values(snapshot) == ("ALPHA",)
    assert snapshot.overrides[0].rule is rule
    with pytest.raises((AttributeError, TypeError)):
        snapshot.transient_cache = {}

    with pytest.raises(TypeError, match="compose"):
        setattr(FrozenRuleRegistry, "compose", classmethod(lambda cls: None))
    with pytest.raises(TypeError, match="items"):
        delattr(FrozenRuleRegistry, "items")
    with pytest.raises(TypeError, match="__bases__"):
        setattr(FrozenRuleRegistry, "__bases__", (api.Registry,))
    with pytest.raises(TypeError, match="_hidden"):
        FrozenRuleRegistry._hidden = {}
    with pytest.raises(TypeError, match="__getattribute__"):
        FrozenRuleRegistry.__getattribute__ = lambda self, name: ()
    with pytest.raises(TypeError, match="late_metadata"):
        FrozenRuleRegistry.late_metadata = "forged"

    def forged_pickle_helper(instance):
        return ["FORGED"]

    for helper_name, dataclass_name in (
        ("__getstate__", "_dataclass_getstate"),
        ("__setstate__", "_dataclass_setstate"),
    ):
        forged_pickle_helper.__module__ = "dataclasses"
        forged_pickle_helper.__name__ = dataclass_name
        with pytest.raises(TypeError, match=helper_name):
            setattr(FrozenRuleRegistry, helper_name, forged_pickle_helper)

    class EqualLabel(str):
        def __eq__(self, other):
            return True

    with pytest.raises(TypeError, match="__qualname__"):
        FrozenRuleRegistry.__qualname__ = EqualLabel("forged")
    with pytest.raises(TypeError, match="__qualname__"):
        api.Registry.__qualname__ = EqualLabel("forged")


def test_registry_subtypes_support_frozen_classvar_metadata_and_generic_reuse():
    api = _api()
    item_type = TypeVar("item_type")

    class GenericCatalog(api.Registry[item_type]):
        domain: ClassVar[str] = "policy"
        _profile: ClassVar[tuple[str, ...]] = ("builtins",)

    class StringCatalog(GenericCatalog[str]):
        pass

    snapshot = StringCatalog.compose(
        _fragment(api, "core", "alpha"),
        name="widgets",
        version="1",
    )

    assert GenericCatalog.domain == "policy"
    assert GenericCatalog._profile == ("builtins",)
    assert "domain" not in snapshot.field_names()
    assert "_profile" not in snapshot.field_names()
    assert "domain" not in snapshot.to_dict()
    assert "_profile" not in snapshot.to_dict()
    with pytest.raises(TypeError, match="domain"):
        GenericCatalog.domain = "forged"
    with pytest.raises(TypeError, match="_profile"):
        del GenericCatalog._profile


def test_registry_subtypes_reject_instance_fields_and_mutable_classvars():
    api = _api()

    with pytest.raises(TypeError, match="snapshot field domain"):

        class InstanceFieldCatalog(api.Registry[str]):
            domain: str = "policy"

    with pytest.raises(TypeError, match="ClassVar cache"):

        class MutableClassVarCatalog(api.Registry[str]):
            cache: ClassVar[dict[str, str]] = {}


@pytest.mark.parametrize("field_name", ("__getattribute__", "allowed", "field_names"))
def test_registry_subtype_classvars_cannot_shadow_inherited_protocols(field_name):
    api = _api()

    with pytest.raises(TypeError, match=f"ClassVar {field_name} cannot shadow"):
        type(
            "ShadowCatalog",
            (api.Registry,),
            {
                "__annotations__": {field_name: ClassVar[object]},
                field_name: "forged",
            },
        )


def test_registry_subtype_classvars_cannot_install_descriptors():
    api = _api()

    def spoof(instance, name):
        return object.__getattribute__(instance, name)

    with pytest.raises(TypeError, match="ClassVar callback cannot be a descriptor"):

        class DescriptorCatalog(api.Registry[str]):
            callback: ClassVar[object] = spoof

    @dataclass(frozen=True, slots=True)
    class HiddenDescriptor:
        def __getattribute__(self, name):
            if name == "__get__":
                raise AttributeError(name)
            return object.__getattribute__(self, name)

        def __get__(self, instance, owner):
            return "FORGED"

    with pytest.raises(TypeError, match="ClassVar metadata cannot be a descriptor"):

        class HiddenDescriptorCatalog(api.Registry[str]):
            metadata: ClassVar[object] = HiddenDescriptor()


def test_registry_rules_and_fragment_inputs_require_exact_primitive_types():
    api = _api()

    class ForgedRule(api.RegistryOverrideRule):
        def matches(self, **kwargs):
            return True

    forged_rule = ForgedRule(
        key="wrong",
        incumbent_owner="wrong",
        incumbent_fragment_version="wrong",
        replacement_owner="wrong",
        replacement_fragment_version="wrong",
        registry_version="wrong",
        rule_version="wrong",
    )
    with pytest.raises(TypeError, match="override_rules"):

        class ForgedRuleCatalog(api.Registry[str]):
            override_rules = (forged_rule,)

    class ForgedEntry(api.RegistryEntry[str]):
        pass

    with pytest.raises(TypeError, match="only RegistryEntry"):
        api.RegistryFragment(
            owner="core",
            version="1",
            items=(ForgedEntry(key="alpha", value="ALPHA"),),
        )

    class ForgedFragment(api.RegistryFragment[str]):
        pass

    forged_fragment = ForgedFragment(
        owner="core",
        version="1",
        items=(_entry(api, "alpha"),),
    )
    with pytest.raises(TypeError, match="only RegistryFragment"):
        api.Registry.compose(forged_fragment, name="widgets", version="1")


@pytest.mark.parametrize(
    "method_name",
    (
        "__init__",
        "__new__",
        "__setattr__",
        "__eq__",
        "__hash__",
        "_validate",
        "compose",
        "to_dict",
        "with_updates",
        "get",
    ),
)
def test_registry_subtypes_cannot_replace_invariant_forming_methods(method_name):
    api = _api()

    with pytest.raises(TypeError, match=method_name):
        type(
            "BypassRegistry",
            (api.Registry,),
            {method_name: lambda *args, **kwargs: None},
        )


def test_registry_subtypes_reject_mixin_mro_bypasses():
    api = _api()

    class InitMixin:
        def __init__(self, **kwargs):
            Params.__init__(self, **kwargs)

    class ComposeMixin:
        @classmethod
        def compose(cls, *fragments, **kwargs):
            return "forged"

    with pytest.raises(TypeError, match="exactly one Registry base"):
        type("InitBypass", (InitMixin, api.Registry), {})
    with pytest.raises(TypeError, match="exactly one Registry base"):
        type("ComposeBypass", (api.Registry, ComposeMixin), {})


def test_registry_subtypes_are_declaration_only_authorities():
    api = _api()

    with pytest.raises(TypeError, match="authority member by_table"):

        class DomainMethodCatalog(api.Registry[str]):
            def by_table(self, table: str):
                return self[table]


def test_direct_construction_and_with_updates_cannot_bypass_compose():
    api = _api()
    core = _fragment(api, "core", "alpha")
    snapshot = api.Registry.compose(core, name="widgets", version="1")

    with pytest.raises((TypeError, ValueError), match="(?i)compose|derived|items|snapshot"):
        type(snapshot)(
            name="widgets",
            fragments=(core,),
            items=snapshot.items,
            overrides=snapshot.overrides,
            version="1",
        )
    with pytest.raises((TypeError, ValueError), match="(?i)compose|derived|snapshot"):
        snapshot.with_updates(name="renamed")

    assert _keys(snapshot) == ("alpha",)


def test_fresh_object_cannot_use_the_public_params_initializer_to_forge_a_snapshot():
    api = _api()

    for snapshot_type in (api.Registry, type("ClosedRegistry", (api.Registry,), {})):
        forged = object.__new__(snapshot_type)
        with pytest.raises(TypeError, match="invariant-forming constructor"):
            Params.__init__(
                forged,
                name="forged",
                fragments=(),
                items=(),
                overrides=(),
                version="1",
            )


def test_base_initializer_cannot_rewrite_a_snapshot_or_nested_entry():
    api = _api()
    core = _fragment(api, "core", "alpha")
    snapshot = api.Registry.compose(core, name="widgets", version="1")
    before_projection = snapshot.to_dict(mode="json")
    before_hash = hash(snapshot)
    entry = core.items[0]
    entry_projection = entry.to_dict(mode="json")
    entry_hash = hash(entry)

    with pytest.raises(TypeError, match="invariant-forming constructor"):
        Params.__init__(
            snapshot,
            name="forged",
            fragments=(),
            items=(),
            overrides=(),
            version="9",
        )
    with pytest.raises(TypeError, match="already initialized"):
        Params.__init__(entry, key="forged", value="FORGED")

    assert snapshot.to_dict(mode="json") == before_projection
    assert hash(snapshot) == before_hash
    assert entry.to_dict(mode="json") == entry_projection
    assert hash(entry) == entry_hash


def test_lookup_surface_reads_the_frozen_records_without_a_hidden_index():
    api = _api()
    snapshot = api.Registry.compose(
        _fragment(api, "core", "alpha", "beta"),
        name="widgets",
        version="1",
    )

    assert snapshot.keys() == ("alpha", "beta")
    assert snapshot.values() == ("ALPHA", "BETA")
    assert snapshot.get("alpha") == "ALPHA"
    assert snapshot.get("missing") is Unset
    assert snapshot.get("missing", None) is None
    assert snapshot.get("missing", default=False) is False
    assert snapshot["beta"] == "BETA"
    assert "alpha" in snapshot
    assert "missing" not in snapshot
    assert 1 not in snapshot
    assert snapshot.owner_of("alpha") == "core"
    with pytest.raises(KeyError, match="missing"):
        snapshot["missing"]
    with pytest.raises(KeyError, match="missing"):
        snapshot.owner_of("missing")


@pytest.mark.parametrize(
    "factory",
    (
        lambda api: api.RegistryEntry(key="", value="value"),
        lambda api: api.RegistryEntry(key="   ", value="value"),
        lambda api: api.RegistryFragment(owner="", version="1", items=()),
        lambda api: api.RegistryFragment(owner="core", version="", items=()),
        lambda api: api.RegistryFragment(owner="core", version="1", items=(), feature="   "),
        lambda api: api.Registry.compose(name="", version="1"),
        lambda api: api.Registry.compose(name="widgets", version=""),
    ),
)
def test_identifiers_are_nonempty_exact_strings(factory):
    api = _api()

    with pytest.raises((TypeError, ValueError)):
        factory(api)


def test_label_subclasses_cannot_spoof_exact_override_authorization():
    api = _api()

    class WildcardLabel(str):
        def __eq__(self, other):
            return isinstance(other, str)

        __hash__ = str.__hash__

    with pytest.raises(TypeError):
        api.RegistryEntry(key=WildcardLabel("alpha"), value="value")
    with pytest.raises(TypeError):
        api.RegistryOverrideRule(
            key=WildcardLabel("wrong-key"),
            incumbent_owner="wrong-owner",
            incumbent_fragment_version="wrong-version",
            replacement_owner="wrong-replacement",
            replacement_fragment_version="wrong-version",
            registry_version="wrong-version",
            rule_version="spoof-v1",
        )


def test_fragment_items_must_be_an_explicit_tuple_of_entries():
    api = _api()

    with pytest.raises(TypeError):
        api.RegistryFragment(owner="core", version="1", items=[_entry(api, "alpha")])
    with pytest.raises(TypeError):
        api.RegistryFragment(owner="core", version="1", items=("alpha",))


@dataclass(slots=True)
class _MutableValue(DataClass):
    label: str


@pytest.mark.parametrize(
    "mutable_value",
    ([], {}, _MutableValue("mutable")),
    ids=("list", "dict", "mutable-dataclass"),
)
def test_registry_rejects_mutable_or_structurally_unhashable_values(mutable_value):
    api = _api()

    def compose_mutable_value() -> None:
        item = api.RegistryEntry(key="mutable", value=mutable_value)
        fragment = api.RegistryFragment(owner="core", items=(item,), version="1")
        api.Registry.compose(fragment, name="widgets", version="1")

    with pytest.raises(UnhashableStructuralValueError):
        compose_mutable_value()


def test_two_profiles_are_isolated_and_optional_fragments_require_explicit_composition():
    api = _api()
    core = _fragment(api, "core", "alpha")
    optional = _fragment(api, "optional", "extra", feature="optional-widget")

    base_profile = api.Registry.compose(core, name="base", version="1")
    base_projection = base_profile.to_dict(mode="json")
    full_profile = api.Registry.compose(core, optional, name="full", version="1")

    assert _keys(base_profile) == ("alpha",)
    assert _keys(full_profile) == ("alpha", "extra")
    assert base_profile.fragments == (core,)
    assert full_profile.fragments == (core, optional)
    assert full_profile.items[-1].feature == "optional-widget"
    assert base_profile.to_dict(mode="json") == base_projection
    assert base_profile != full_profile


def test_import_and_reload_of_optional_fragment_do_not_mutate_active_snapshot(
    tmp_path,
    monkeypatch,
):
    api = _api()
    module_name = "_lionagi_optional_registry_fixture"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "from lionagi.ln.types import RegistryEntry, RegistryFragment\n"
        "OPTIONAL_FRAGMENT = RegistryFragment(\n"
        "    owner='optional',\n"
        "    items=(RegistryEntry(key='extra', value='EXTRA'),),\n"
        "    version='1',\n"
        "    feature='optional-widget',\n"
        ")\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys

    sys.modules.pop(module_name, None)
    core = _fragment(api, "core", "alpha")
    active = api.Registry.compose(core, name="active", version="1")
    before = active.to_dict(mode="json")
    before_hash = hash(active)

    try:
        optional_module = importlib.import_module(module_name)
        first_fragment = optional_module.OPTIONAL_FRAGMENT
        reloaded = importlib.reload(optional_module)
        second_fragment = reloaded.OPTIONAL_FRAGMENT
        importlib.reload(reloaded)

        first_full = api.Registry.compose(
            core,
            first_fragment,
            name="full",
            version="1",
        )
        second_full = api.Registry.compose(
            core,
            second_fragment,
            name="full",
            version="1",
        )
    finally:
        sys.modules.pop(module_name, None)

    assert first_fragment == second_fragment
    assert first_full == second_full
    assert _keys(first_full) == ("alpha", "extra")
    assert active.to_dict(mode="json") == before
    assert hash(active) == before_hash
    assert _keys(active) == ("alpha",)


def test_concurrent_compositions_do_not_share_builder_or_profile_state():
    api = _api()
    core = _fragment(api, "core", "alpha", "beta")
    optional = _fragment(api, "optional", "extra", feature="optional-widget")

    def compose(index: int):
        if index % 2:
            return api.Registry.compose(core, optional, name="full", version="1")
        return api.Registry.compose(core, name="base", version="1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = tuple(pool.map(compose, range(128)))

    base_snapshots = snapshots[::2]
    full_snapshots = snapshots[1::2]
    assert all(_keys(snapshot) == ("alpha", "beta") for snapshot in base_snapshots)
    assert all(_keys(snapshot) == ("alpha", "beta", "extra") for snapshot in full_snapshots)
    assert len(set(base_snapshots)) == 1
    assert len(set(full_snapshots)) == 1
