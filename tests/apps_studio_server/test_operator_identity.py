# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Durable turn identity behavior at each Operator consumer boundary."""

from __future__ import annotations

import importlib
import os

import pytest

_CONSUMERS = ["application_mcp", "cancel_run", "rename_session", "resume_run", "run_control"]
_ENV_NAMES = [
    "LIONAGI_OPERATOR_DB_PATH",
    "LIONAGI_OPERATOR_CONVERSATION_ID",
    "LIONAGI_OPERATOR_REQUEST_ID",
]
_ERROR = "Studio application bridge is missing its durable turn identity"


def _common():
    return importlib.import_module("lionagi.studio.operator._common")


def _set_identity(monkeypatch, values):
    for name, value in zip(_ENV_NAMES, values, strict=True):
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("spelling", ["exact", "padded", "whitespace-only"])
def test_identity_keeps_exact_values_and_uses_the_supplied_store_factory(monkeypatch, spelling):
    values = ("operator.sqlite", "conversation-id", "request-id")
    if spelling == "padded":
        values = tuple(" " + value + " " for value in values)
    elif spelling == "whitespace-only":
        values = (" ", "  ", "   ")
    _set_identity(monkeypatch, values)
    store = object()
    calls = []

    def factory(path):
        calls.append(path)
        return store

    actual = _common()._identity(factory)
    assert type(actual) is tuple and len(actual) == 3
    assert actual[0] is store
    assert actual[1:] == values[1:]
    assert calls == [values[0]]


@pytest.mark.parametrize("missing_name", _ENV_NAMES, ids=["database", "conversation", "request"])
@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
def test_missing_or_empty_identity_refuses_before_constructing_a_store(
    monkeypatch, missing_name, empty
):
    values = ("private-db-path", "private-conversation", "private-request")
    _set_identity(monkeypatch, values)
    if empty:
        monkeypatch.setenv(missing_name, "")
    else:
        monkeypatch.delenv(missing_name)
    calls = []

    def factory(path):
        calls.append(path)
        raise AssertionError("a partial identity must not construct a store")

    with pytest.raises(RuntimeError) as caught:
        _common()._identity(factory)
    assert type(caught.value) is RuntimeError
    assert caught.value.args == (_ERROR,)
    assert all(value not in str(caught.value) for value in values)
    assert calls == []


@pytest.mark.parametrize("consumer", _CONSUMERS)
def test_shared_identity_policy_reaches_each_existing_import_path(monkeypatch, consumer):
    module = importlib.import_module("lionagi.studio.operator." + consumer)
    common = _common()
    _set_identity(monkeypatch, ("operator.sqlite", "original-conversation", "original-request"))
    expected = (object(), "shared-conversation", "shared-request")
    duplicate_store = object()
    seen = []

    def factory(_path):
        return duplicate_store

    def policy(store_factory):
        seen.append(store_factory)
        return expected

    monkeypatch.setattr(common, "_identity", policy)
    monkeypatch.setattr(module, "OperatorStore", factory)
    assert module._identity() is expected
    assert seen == [factory]


@pytest.mark.parametrize("failing_name", _ENV_NAMES, ids=["database", "conversation", "request"])
def test_an_unreadable_environment_propagates_the_original_failure(monkeypatch, failing_name):
    common = _common()
    _set_identity(monkeypatch, ("operator.sqlite", "conversation-id", "request-id"))
    failure = PermissionError("identity environment is unavailable")
    original_get = os.environ.get
    reads = []
    factories = []

    def get(name, default=None):
        if name in _ENV_NAMES:
            reads.append(name)
        if name == failing_name:
            raise failure
        return original_get(name, default)

    def factory(path):
        factories.append(path)
        raise AssertionError("an unreadable identity must not construct a store")

    # Keep the real environment and unrelated lookups, including test reporting, intact.
    with monkeypatch.context() as scope:
        scope.setattr(os.environ, "get", get)
        with pytest.raises(PermissionError) as caught:
            common._identity(factory)
    assert caught.value is failure
    assert reads == _ENV_NAMES[: _ENV_NAMES.index(failing_name) + 1]
    assert factories == []


@pytest.mark.parametrize("failure_kind", ["io", "value"])
def test_store_construction_failures_are_not_relabelled(monkeypatch, failure_kind):
    _set_identity(monkeypatch, ("operator.sqlite", "conversation-id", "request-id"))
    failure_type = OSError if failure_kind == "io" else ValueError
    failure = failure_type("store construction is unavailable")

    def factory(_path):
        raise failure

    with pytest.raises(failure_type) as caught:
        _common()._identity(factory)
    assert caught.value is failure


def test_a_complete_identity_constructs_the_real_store_without_opening_a_database(
    monkeypatch, tmp_path
):
    from lionagi.studio.operator.store import OperatorStore

    path = tmp_path / "operator.sqlite"
    _set_identity(monkeypatch, (str(path), "conversation-id", "request-id"))
    actual = _common()._identity(OperatorStore)
    assert type(actual[0]) is OperatorStore
    assert actual[1:] == ("conversation-id", "request-id")
    assert not path.exists()
