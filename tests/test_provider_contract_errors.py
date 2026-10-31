# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the provider-contract errors and the shared missing-feature error.

None of these errors belongs to the ``ProviderError`` family: that family
classifies provider call failures and callers that catch it retry or fall back,
which a failed activation or an unsupported contract must never trigger.
"""

from __future__ import annotations

import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lionagi._feature_errors import MissingFeatureError
from lionagi._provider_errors import (
    ProviderActivationError,
    ProviderCompositionError,
    ProviderContractVersionError,
    ProviderError,
)

ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-test-not-a-real-key"


def _missing_with_extra() -> MissingFeatureError:
    return MissingFeatureError(
        feature="state",
        extra="state",
        missing_distribution="aiosqlite",
        requested_symbol="StateDB",
        install_hint="pip install 'lionagi[state]'",
        provider="scripted",
        endpoint="chat",
        reason="missing_extra",
        source="lionagi.state",
        import_root="aiosqlite",
    )


def _missing_without_extra() -> MissingFeatureError:
    return MissingFeatureError(
        feature="providers",
        extra=None,
        missing_distribution="lionagi",
        requested_symbol=None,
        install_hint="reinstall the matching lionagi release",
    )


def _contract_error() -> ProviderContractVersionError:
    return ProviderContractVersionError(
        source="lionhub.providers.manifest", expected=(1, 2), observed="2"
    )


def _activation_error() -> ProviderActivationError:
    return ProviderActivationError(
        provider="openai",
        endpoint="chat",
        source="lionhub.providers.manifest",
        reason="activation_failed",
    )


def _composition_error() -> ProviderCompositionError:
    return ProviderCompositionError()


FACTORIES = [
    _missing_with_extra,
    _missing_without_extra,
    _contract_error,
    _activation_error,
    _composition_error,
]


# --- Hierarchy ---


@pytest.mark.parametrize(
    ("cls", "base"),
    [
        (MissingFeatureError, ImportError),
        (ProviderContractVersionError, ValueError),
        (ProviderActivationError, RuntimeError),
        (ProviderCompositionError, ValueError),
    ],
)
def test_each_error_has_exactly_the_specified_base(cls, base):
    assert cls.__bases__ == (base,)


@pytest.mark.parametrize(
    "cls",
    [
        MissingFeatureError,
        ProviderContractVersionError,
        ProviderActivationError,
        ProviderCompositionError,
    ],
)
def test_no_error_belongs_to_the_provider_error_family(cls):
    assert not issubclass(cls, ProviderError)


def test_missing_feature_error_is_an_import_error():
    assert isinstance(_missing_with_extra(), ImportError)


def test_missing_feature_error_takes_keyword_arguments_only():
    with pytest.raises(TypeError):
        MissingFeatureError("state", "state", "aiosqlite", None, "hint")  # type: ignore[misc]


# --- Fields and messages ---


def test_missing_feature_error_fields_and_message_with_extra():
    err = _missing_with_extra()
    assert err.feature == "state"
    assert err.extra == "state"
    assert err.missing_distribution == "aiosqlite"
    assert err.requested_symbol == "StateDB"
    assert err.install_hint == "pip install 'lionagi[state]'"
    assert err.provider == "scripted"
    assert err.endpoint == "chat"
    assert err.reason == "missing_extra"
    assert err.source == "lionagi.state"
    assert str(err) == (
        "LionAGI feature 'state' requires optional extra 'state'; "
        "install it with: pip install 'lionagi[state]'"
    )


def test_missing_feature_error_without_extra_names_the_distribution():
    err = _missing_without_extra()
    assert err.extra is None
    assert err.provider is None
    assert err.endpoint is None
    assert err.reason is None
    assert err.source is None
    assert err.requested_symbol is None
    assert str(err) == (
        "LionAGI feature 'providers' requires distribution 'lionagi', which is not "
        "installed; guidance: reinstall the matching lionagi release"
    )


def test_missing_feature_error_sets_the_import_error_name_from_the_import_root():
    assert _missing_with_extra().name == "aiosqlite"
    assert _missing_without_extra().name is None


def test_contract_version_error_fields_and_message():
    err = _contract_error()
    assert err.source == "lionhub.providers.manifest"
    assert err.expected == (1, 2)
    assert err.observed == "2"
    assert str(err) == (
        "Unsupported provider contract version in lionhub.providers.manifest: "
        "expected one of 1, 2; observed '2'"
    )


@pytest.mark.parametrize("observed", [True, "1", None, 1.5])
def test_contract_version_error_keeps_the_observed_value_as_given(observed):
    err = ProviderContractVersionError(source="manifest", expected=(1,), observed=observed)
    assert err.observed is observed
    assert str(err).endswith(f"observed {observed!r}")


def test_activation_error_fields_and_message():
    err = _activation_error()
    assert (err.provider, err.endpoint, err.reason) == ("openai", "chat", "activation_failed")
    assert err.source == "lionhub.providers.manifest"
    assert str(err) == (
        "Provider 'openai' endpoint 'chat' could not be activated from "
        "lionhub.providers.manifest: reason=activation_failed"
    )


def test_activation_error_without_endpoint_and_with_cycle_reason():
    err = ProviderActivationError(
        provider="openai", endpoint=None, source="manifest", reason="cycle"
    )
    assert err.endpoint is None
    assert str(err) == "Provider 'openai' could not be activated from manifest: reason=cycle"


@pytest.mark.parametrize("reason", ["", "timeout", "Cycle", None])
def test_activation_error_rejects_a_reason_outside_the_two_allowed(reason):
    with pytest.raises(ValueError):
        ProviderActivationError(
            provider="openai",
            endpoint=None,
            source="manifest",
            reason=reason,  # type: ignore[arg-type]
        )


def test_composition_error_names_the_catalog_and_never_a_distribution():
    err = _composition_error()
    assert str(err) == (
        "No provider catalog was supplied; explicit composition of a provider catalog is required."
    )
    assert "distribution" not in str(err)
    assert "install" not in str(err).lower()


# --- Pickling ---


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda f: f.__name__)
@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_pickle_keeps_every_field_and_the_message(factory, protocol):
    err = factory()
    clone = pickle.loads(pickle.dumps(err, protocol=protocol))
    assert type(clone) is type(err)
    assert str(clone) == str(err)
    assert clone.args == err.args
    assert {k: v for k, v in vars(clone).items() if not k.startswith("_")} == {
        k: v for k, v in vars(err).items() if not k.startswith("_")
    }


def test_pickle_keeps_the_import_error_name():
    clone = pickle.loads(pickle.dumps(_missing_with_extra()))
    assert clone.name == "aiosqlite"


# --- Messages are built from the fields only ---


def _raise_chained(factory):
    try:
        try:
            raise RuntimeError(f"factory failed with {SECRET}")
        except RuntimeError as exc:
            raise factory() from exc
    except Exception as err:  # noqa: BLE001 - the error under test
        return err
    raise AssertionError("unreachable")


@pytest.mark.parametrize("factory", FACTORIES, ids=lambda f: f.__name__)
def test_a_chained_cause_never_reaches_the_message_or_repr(factory):
    err = _raise_chained(factory)
    assert isinstance(err.__cause__, RuntimeError)
    assert SECRET not in str(err)
    assert SECRET not in repr(err)


# --- Import surface ---


def _run_fresh(code: str, *argv: str) -> str:
    proc = subprocess.run(  # noqa: S603 -- this interpreter, a fixed program
        [sys.executable, "-c", textwrap.dedent(code), *argv],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return proc.stdout


def test_feature_errors_module_needs_only_the_standard_library():
    # Loaded by file path so the package's own imports do not run: what the module
    # itself imports is what the child's module table shows afterwards.
    out = _run_fresh(
        """
        import importlib.util, sys

        before = set(sys.modules)
        spec = importlib.util.spec_from_file_location("feature_errors_probe", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules["feature_errors_probe"] = module
        spec.loader.exec_module(module)
        added = {name.split(".")[0] for name in set(sys.modules) - before}
        extra = sorted(added - set(sys.stdlib_module_names) - {"feature_errors_probe"})
        assert not extra, extra
        assert module.MissingFeatureError.__bases__ == (ImportError,)
        print("ok")
        """,
        str(ROOT / "lionagi" / "_feature_errors.py"),
    )
    assert out.strip() == "ok"


def test_importing_feature_errors_loads_no_hub_module():
    out = _run_fresh(
        """
        import sys

        import lionagi._feature_errors

        hub = sorted(m for m in sys.modules if m == "lionhub" or m.startswith("lionhub."))
        assert not hub, hub
        assert "lionagi._feature_errors" in sys.modules
        print("ok")
        """
    )
    assert out.strip() == "ok"
