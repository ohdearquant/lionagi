# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Hook admission and class fallback retain their module-loading behavior."""

import importlib
import sys
import types

import pytest

import lionagi._class_registry as registry
import lionagi.agent.settings as settings
from lionagi import ln
from lionagi.ln import _utils as ln_utils


class _CustomImportError(ImportError):
    pass


def _failure(kind):
    if kind == "attribute":
        return AttributeError("module attribute failed")
    cls = {
        "plain": ImportError,
        "subclass": _CustomImportError,
        "missing": ModuleNotFoundError,
    }[kind]
    return cls("module initialization failed", name="dependency", path="/fixture/dependency.py")


def _module(monkeypatch, name, **attributes):
    module = types.ModuleType(name)
    vars(module).update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "LION_CLASS_REGISTRY", {})
    monkeypatch.setattr(registry, "_BUILTIN_MODULES", ())
    monkeypatch.setattr(ln_utils, "_TYPE_CACHE", {})
    monkeypatch.setattr(ln_utils, "_ALLOWED_MODULE_PREFIXES", {"lionagi."})
    return registry


@pytest.fixture
def package_with_child(tmp_path, monkeypatch):
    children = []

    def make(name):
        directory = tmp_path / name.rsplit(".", 1)[-1]
        directory.mkdir()
        marker = directory / "child-executed"
        (directory / "selected.py").write_text(
            "from pathlib import Path\n" + f"Path({str(marker)!r}).write_text('loaded')\n"
        )
        module = _module(monkeypatch, name, __path__=[str(directory)])
        child = name + ".selected"
        assert child not in sys.modules
        children.append(child)
        return module, marker, child

    try:
        yield make
    finally:
        for child in children:
            sys.modules.pop(child, None)


class TestHookImportBehavior:
    @pytest.mark.parametrize("reference", ["", "untrusted_hooks"])
    def test_no_colon_returns_before_module_loading(self, monkeypatch, reference):
        calls = []

        def unexpected(name, package=None):
            calls.append(name)
            raise AssertionError("module loading must not start")

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", unexpected)
            result = settings._import_hook(reference, trusted_hook_modules=set())
        assert result is None
        assert calls == []

    @pytest.mark.parametrize("name", ["untrusted_hooks", "trusted_hooks.child"])
    def test_exact_module_trust_is_checked_before_loading(self, monkeypatch, name):
        calls = []

        def unexpected(module, package=None):
            calls.append(module)
            raise AssertionError("untrusted module loading must not start")

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", unexpected)
            with pytest.raises(PermissionError) as caught:
                settings._import_hook(name + ":handler", trusted_hook_modules={"trusted_hooks"})
        assert type(caught.value) is PermissionError
        assert str(caught.value) == (
            f"Untrusted hook module {name!r}. Add it to trusted_hook_modules to allow."
        )
        assert calls == []

    def test_builtin_callable_identity_is_preserved(self):
        from lionagi.agent.hooks import guard_destructive

        assert (
            settings._import_hook(
                "lionagi.agent.hooks:guard_destructive",
                trusted_hook_modules={"lionagi.agent.hooks"},
            )
            is guard_destructive
        )

    def test_cached_module_attributes_are_read_each_time(self, monkeypatch):
        def first():
            return "first"

        def second():
            return "second"

        module = _module(monkeypatch, "cached_hook_contract", handler=first)
        reference = module.__name__ + ":handler"
        assert settings._import_hook(reference, trusted_hook_modules={module.__name__}) is first
        module.handler = second
        assert settings._import_hook(reference, trusted_hook_modules={module.__name__}) is second
        assert sys.modules[module.__name__] is module

    @pytest.mark.parametrize("attribute", ["", "selected", "absent"])
    def test_missing_attributes_do_not_load_package_children(
        self, monkeypatch, package_with_child, attribute
    ):
        module, marker, child = package_with_child("hook_package_contract")
        actual_import = importlib.import_module
        calls = []

        def observe(name, package=None):
            calls.append(name)
            return actual_import(name, package=package)

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", observe)
            result = settings._import_hook(
                module.__name__ + ":" + attribute, trusted_hook_modules={module.__name__}
            )
        assert result is None
        assert calls == [module.__name__]
        assert child not in sys.modules
        assert not marker.exists()

    @pytest.mark.parametrize("kind", ["plain", "subclass", "missing", "attribute"])
    def test_import_and_attribute_failures_return_none(self, monkeypatch, kind):
        failure = _failure(kind)

        def fail(name, package=None):
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", fail)
            result = settings._import_hook(
                "failing_hook_contract:handler", trusted_hook_modules={"failing_hook_contract"}
            )
        assert result is None

    @pytest.mark.parametrize("failure_type", [RuntimeError, ValueError])
    def test_other_initialization_failures_keep_the_original_exception(
        self, monkeypatch, failure_type
    ):
        failure = failure_type("hook initialization failed")

        def fail(name, package=None):
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", fail)
            with pytest.raises(failure_type) as caught:
                settings._import_hook(
                    "failing_hook_contract:handler", trusted_hook_modules={"failing_hook_contract"}
                )
        assert caught.value is failure
        assert str(caught.value) == "hook initialization failed"

    def test_trusted_empty_module_retains_the_native_value_error(self):
        with pytest.raises(ValueError) as caught:
            settings._import_hook(":handler", trusted_hook_modules={""})
        assert type(caught.value) is ValueError
        assert str(caught.value) == "Empty module name"
        assert caught.value.__cause__ is None


class TestBuiltinClassFallbackBehavior:
    def test_exact_registry_hit_precedes_module_loading(self, monkeypatch, isolated_registry):
        selected = type("Selected", (), {})
        isolated_registry.LION_CLASS_REGISTRY["Selected"] = selected
        calls = []

        def unexpected(name, package=None):
            calls.append(name)
            raise AssertionError("an exact registry hit must not load a module")

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", unexpected)
            result = isolated_registry.get_class("Selected")
        assert result is selected
        assert isolated_registry.LION_CLASS_REGISTRY == {"Selected": selected}
        assert calls == []

    @pytest.mark.parametrize("kind", ["plain", "subclass", "missing"])
    def test_import_failure_does_not_prevent_later_success(
        self, monkeypatch, isolated_registry, kind
    ):
        failed_name = "lionagi._fallback_failed_contract"
        found_name = "lionagi._fallback_found_contract"
        selected = type("Selected", (), {"__module__": found_name})
        _module(monkeypatch, found_name, Selected=selected)
        monkeypatch.setattr(
            isolated_registry, "_BUILTIN_MODULES", (failed_name, found_name, "never_loaded")
        )
        failure = _failure(kind)
        actual_import = importlib.import_module
        calls = []

        def observe(name, package=None):
            calls.append(name)
            if name == failed_name:
                raise failure
            return actual_import(name, package=package)

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", observe)
            result = isolated_registry.get_class("Selected")
        assert result is selected
        assert calls == [failed_name, found_name]
        assert isolated_registry.LION_CLASS_REGISTRY == {}

    def test_foreign_and_nonclass_attributes_do_not_win_before_a_builtin(
        self, monkeypatch, isolated_registry
    ):
        foreign = type("Selected", (), {"__module__": "foreign_models"})
        selected = type("Selected", (), {"__module__": "lionagi._defined_contract"})
        modules = (
            _module(monkeypatch, "lionagi._foreign_contract", Selected=foreign),
            _module(monkeypatch, "lionagi._nonclass_contract", Selected=object()),
            _module(monkeypatch, "lionagi._defined_contract", Selected=selected),
        )
        monkeypatch.setattr(
            isolated_registry, "_BUILTIN_MODULES", tuple(module.__name__ for module in modules)
        )
        isolated_registry.LION_CLASS_REGISTRY["legacy.Selected"] = foreign
        assert isolated_registry.get_class("Selected") is selected

    def test_cached_builtin_module_is_read_again_without_caching_its_class(
        self, monkeypatch, isolated_registry
    ):
        name = "lionagi._changing_builtin_contract"
        first = type("Selected", (), {"__module__": name})
        second = type("Selected", (), {"__module__": name})
        module = _module(monkeypatch, name, Selected=first)
        monkeypatch.setattr(isolated_registry, "_BUILTIN_MODULES", (name,))
        assert isolated_registry.get_class("Selected") is first
        module.Selected = second
        assert isolated_registry.get_class("Selected") is second
        assert sys.modules[name] is module
        assert isolated_registry.LION_CLASS_REGISTRY == {}

    def test_registry_suffix_remains_available_after_failed_builtin_imports(
        self, monkeypatch, isolated_registry
    ):
        selected = type("Selected", (), {"__module__": "external_models"})
        isolated_registry.LION_CLASS_REGISTRY["legacy.Selected"] = selected
        monkeypatch.setattr(isolated_registry, "_BUILTIN_MODULES", ("unavailable_builtin",))
        calls = []

        def fail(name, package=None):
            calls.append(name)
            raise ImportError("built-in unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", fail)
            result = isolated_registry.get_class("Selected")
        assert result is selected
        assert calls == ["unavailable_builtin"]

    def test_missing_attribute_does_not_load_a_package_child(
        self, monkeypatch, isolated_registry, package_with_child
    ):
        module, marker, child = package_with_child("lionagi._builtin_package_contract")
        monkeypatch.setattr(isolated_registry, "_BUILTIN_MODULES", (module.__name__,))
        actual_import = importlib.import_module
        calls = []

        def observe(name, package=None):
            calls.append(name)
            return actual_import(name, package=package)

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", observe)
            with pytest.raises(ValueError):
                isolated_registry.get_class("selected")
        assert calls == [module.__name__]
        assert child not in sys.modules
        assert not marker.exists()

    @pytest.mark.parametrize("kind", ["plain", "subclass", "missing"])
    def test_failed_builtin_imports_do_not_replace_the_final_bare_name_error(
        self, monkeypatch, isolated_registry, kind
    ):
        monkeypatch.setattr(isolated_registry, "_BUILTIN_MODULES", ("missing_builtin",))
        failure = _failure(kind)

        def fail(name, package=None):
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", fail)
            with pytest.raises(ValueError) as caught:
                isolated_registry.get_class("AbsentContractClass")
        assert type(caught.value) is ValueError
        assert str(caught.value) == (
            "Unable to find class 'AbsentContractClass': a bare name resolves only to a class "
            "in lionagi's built-in modules or in LION_CLASS_REGISTRY; give its dotted path"
        )
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None

    @pytest.mark.parametrize("kind", ["plain", "subclass", "missing"])
    def test_skipped_builtin_error_does_not_replace_the_dotted_load_cause(
        self, monkeypatch, isolated_registry, kind
    ):
        requested = "lionagi._missing_dotted_contract"
        class_name = requested + ".AbsentContractClass"
        monkeypatch.delitem(sys.modules, requested, raising=False)
        monkeypatch.setattr(isolated_registry, "_BUILTIN_MODULES", ("missing_builtin",))
        original = _failure(kind)
        root_cause = RuntimeError("dependency root cause")
        original.__cause__ = root_cause
        skipped = ImportError("a different built-in failed", name="builtin_dependency")
        calls = []

        def fail(name, package=None):
            calls.append(name)
            if name == requested:
                raise original
            assert name == "missing_builtin"
            raise skipped

        with monkeypatch.context() as patch:
            patch.setattr(importlib, "import_module", fail)
            with pytest.raises(ValueError) as caught:
                isolated_registry.get_class(class_name)
        assert calls == [requested, "missing_builtin"]
        assert type(caught.value) is ValueError
        assert str(caught.value) == (
            f"Unable to find class '{class_name}': no class of that name loads from that "
            "module path; if the class has moved, give its current dotted path "
            "(loading it failed: module initialization failed)"
        )
        load_error = caught.value.__cause__
        assert type(load_error) is ValueError
        assert str(load_error) == (
            f"Failed to load type '{class_name}': module initialization failed"
        )
        assert load_error.__cause__ is original
        assert type(original) is type(_failure(kind))
        assert str(original) == "module initialization failed"
        assert original.name == "dependency"
        assert original.path == "/fixture/dependency.py"
        assert original.__cause__ is root_cause
        assert ln_utils._TYPE_CACHE == {}


class TestSharedImportDelegation:
    @pytest.mark.parametrize("caller", ["hook", "class_fallback"])
    def test_successful_resolution_uses_the_shared_module_loader(
        self, monkeypatch, isolated_registry, caller
    ):
        name = "lionagi._delegation_contract"

        def handler():
            return "handled"

        selected = type("Selected", (), {"__module__": name})
        _module(monkeypatch, name, handler=handler, Selected=selected)
        monkeypatch.setattr(isolated_registry, "_BUILTIN_MODULES", (name,))
        actual_import = ln.import_module
        calls = []

        def observe(module_name, *args, **kwargs):
            calls.append(module_name)
            return actual_import(module_name, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(ln, "import_module", observe)
            if caller == "hook":
                result = settings._import_hook(name + ":handler", trusted_hook_modules={name})
                expected = handler
            else:
                result = isolated_registry.get_class("Selected")
                expected = selected
        assert result is expected
        assert calls == [name]
