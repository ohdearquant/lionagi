"""Exercise validation sanitization with mutable compatibility exception groups."""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

_PRIVATE = "private validation input"
_CYCLE_MESSAGE = "API call failed: cyclic exception group"


@pytest.fixture
def fallback_group(monkeypatch):
    """Load the real no-backport shim without changing process-wide imports."""
    spec = importlib.util.find_spec("lionagi.ln.concurrency._compat")
    assert spec is not None and spec.origin is not None
    original_import = builtins.__import__

    def import_for_shim(name, *args, **kwargs):
        if name == "sys":
            return SimpleNamespace(version_info=(3, 10))
        if name == "exceptiongroup":
            raise ImportError("backport intentionally unavailable")
        return original_import(name, *args, **kwargs)

    shim_builtins = dict(vars(builtins), __import__=import_for_shim)
    namespace = {"__name__": "test_fallback_exception_groups", "__builtins__": shim_builtins}
    source = Path(spec.origin).read_bytes()
    exec(compile(source, spec.origin, "exec"), namespace)

    base, group = namespace["BaseExceptionGroup"], namespace["ExceptionGroup"]
    assert group.__module__ == "test_fallback_exception_groups"
    probe = group("probe", [RuntimeError("ordinary")])
    probe.exceptions = (probe,)
    assert probe.exceptions[0] is probe
    imodel = importlib.import_module("lionagi.service.imodel")
    monkeypatch.setattr(imodel, "BaseExceptionGroup", base)
    monkeypatch.setattr(imodel, "ExceptionGroup", group)
    return imodel, base, group


def _validation_error():
    return ValidationError.from_exception_data(
        "Input", [{"type": "int_parsing", "loc": (), "input": _PRIVATE}]
    )


def _assert_safe_graph(root, base_group):
    pending = [root]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ValidationError):
            pytest.fail("exception graph retained a validation error", pytrace=False)
        if _PRIVATE in str(current) or _PRIVATE in repr(current):
            pytest.fail("exception graph retained validation input", pytrace=False)
        pending.extend((current.__cause__, current.__context__))
        if isinstance(current, base_group):
            pending.extend(current.exceptions)


def _sanitize_with_step_limit(imodel, error):
    previous = sys.gettrace()
    steps = 0

    def trace(frame, event, arg):
        nonlocal steps
        if event == "line" and frame.f_code.co_filename == imodel.__file__:
            steps += 1
            if steps > 10000:
                raise AssertionError("exception sanitization did not terminate")
        return trace

    try:
        sys.settrace(trace)
        return imodel._sanitize_execution_error(error)
    finally:
        sys.settrace(previous)


@pytest.mark.parametrize("location", ["top", "__cause__", "__context__"])
def test_validation_group_cycles_use_a_fixed_placeholder(fallback_group, location):
    imodel, base_group, group_type = fallback_group
    ordinary = LookupError("ordinary")
    group = group_type("cycle", [ordinary])
    group.exceptions = (group, _validation_error(), ordinary)

    if location == "top":
        result = _sanitize_with_step_limit(imodel, group)
        cleaned = result
    else:
        outer = RuntimeError("outer")
        setattr(outer, location, group)
        result = _sanitize_with_step_limit(imodel, outer)
        assert result is outer
        cleaned = getattr(result, location)

    assert isinstance(cleaned, group_type)
    assert cleaned is not group
    assert type(cleaned.exceptions[0]) is ValueError
    assert str(cleaned.exceptions[0]) == _CYCLE_MESSAGE
    assert cleaned.exceptions[-1] is ordinary
    if location == "top":
        assert len(cleaned.exceptions) == 3
        assert isinstance(cleaned.exceptions[1], ValueError)
    else:
        assert len(cleaned.exceptions) == 2
    _assert_safe_graph(result, base_group)


@pytest.mark.parametrize("location", ["top", "__cause__", "__context__"])
def test_cycle_without_validation_keeps_group_identity(fallback_group, location):
    imodel, base_group, group_type = fallback_group
    ordinary = LookupError("ordinary")
    group = group_type("cycle", [ordinary])
    group.exceptions = (group, ordinary)
    if location == "top":
        result = _sanitize_with_step_limit(imodel, group)
        assert result is group
    else:
        outer = RuntimeError("outer")
        setattr(outer, location, group)
        result = _sanitize_with_step_limit(imodel, outer)
        assert result is outer and getattr(result, location) is group
    _assert_safe_graph(result, base_group)


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_all_validation_linked_group_is_removed(fallback_group, link):
    imodel, base_group, group_type = fallback_group
    outer = RuntimeError("outer")
    setattr(outer, link, group_type("private", [_validation_error()]))
    assert imodel._sanitize_execution_error(outer) is outer
    assert getattr(outer, link) is None
    _assert_safe_graph(outer, base_group)


def test_shared_group_keeps_distinct_root_and_link_policies(fallback_group):
    imodel, base_group, group_type = fallback_group
    ordinary = LookupError("ordinary")
    shared = group_type("shared", [ordinary, _validation_error()])
    linked = RuntimeError("linked")
    linked.__cause__ = shared
    root = group_type("root", [shared, linked])

    result = imodel._sanitize_execution_error(root)

    top_shared, same_linked = result.exceptions
    assert same_linked is linked
    assert top_shared.exceptions[0] is ordinary
    assert isinstance(top_shared.exceptions[1], ValueError)
    assert linked.__cause__.exceptions == (ordinary,)
    assert top_shared is not linked.__cause__
    _assert_safe_graph(result, base_group)
