# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Group escalation retains spawn-bound authority when an initial signal fails."""

from __future__ import annotations

import asyncio
import signal

import pytest

from lionagi.ln import _proc

_PGID = 4242
_MEMBER = (4243, 20.0)
_WITNESSES = (_MEMBER,)


class _SignalFailure(RuntimeError):
    pass


class _Child:
    pid = _PGID

    def __init__(self, *, exits_on_terminate=True):
        self.returncode = None
        self.exits_on_terminate = exits_on_terminate
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1
        if self.exits_on_terminate:
            self.returncode = -signal.SIGTERM

    def kill(self):
        self.killed += 1
        self.returncode = -signal.SIGKILL


class _Group:
    def __init__(self, first_failure):
        self.first_failure = first_failure
        self.calls = []
        self.descendant_alive = True
        self.reused = False
        self.witness_missing = False
        self.first_attempt = asyncio.Event()

    def killpg(self, pgid, sig):
        assert pgid == _PGID
        self.calls.append(sig)
        if sig == signal.SIGTERM:
            self.first_attempt.set()
            raise self.first_failure
        if sig == 0:
            if isinstance(self.first_failure, ProcessLookupError):
                raise ProcessLookupError()
            if isinstance(self.first_failure, PermissionError):
                raise PermissionError()
            if not self.descendant_alive:
                raise ProcessLookupError()
            return
        assert sig == signal.SIGKILL
        self.descendant_alive = False

    def identity(self, pid, pgid):
        assert (pid, pgid) == (_MEMBER[0], _PGID)
        if self.reused:
            return "found", (_MEMBER[0], _MEMBER[1] + 1)
        if self.witness_missing:
            return "missing", None
        if self.descendant_alive:
            return "found", _MEMBER
        return "missing", None


@pytest.fixture
def patch_process_calls(monkeypatch):
    def install(group):
        monkeypatch.setattr(_proc.os, "getpgrp", lambda: 9999)
        monkeypatch.setattr(_proc.os, "killpg", group.killpg)
        monkeypatch.setattr(_proc, "_pinned_group_identity", group.identity)

    return install


@pytest.mark.asyncio
async def test_initial_group_error_still_escalates_witnessed_descendant_after_child_exit(
    patch_process_calls, monkeypatch
):
    failure = _SignalFailure("first group signal failed")
    group = _Group(failure)
    patch_process_calls(group)
    original = _proc._signal_group_outcome
    used_witnesses = []

    def observe(pgid, witnesses, sig):
        used_witnesses.append((sig, witnesses))
        return original(pgid, witnesses, sig)

    monkeypatch.setattr(_proc, "_signal_group_outcome", observe)
    child = _Child()
    with pytest.raises(_SignalFailure) as raised:
        await asyncio.wait_for(
            _proc.aterminate_process_group(child, grace=0.02, group_witnesses=_WITNESSES),
            timeout=1,
        )

    assert raised.value is failure
    assert child.terminated == 1
    assert child.killed == 1
    assert (signal.SIGTERM, _WITNESSES) in used_witnesses
    assert (signal.SIGKILL, _WITNESSES) in used_witnesses
    assert [sig for sig in group.calls if sig != 0] == [signal.SIGTERM, signal.SIGKILL]
    assert not group.descendant_alive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ProcessLookupError(), PermissionError()], ids=["esrch", "eperm"]
)
async def test_absent_or_forbidden_group_is_not_retried(patch_process_calls, failure):
    group = _Group(failure)
    patch_process_calls(group)
    child = _Child()
    try:
        await asyncio.wait_for(
            _proc.aterminate_process_group(child, grace=0.02, group_witnesses=_WITNESSES),
            timeout=1,
        )
    except (ProcessLookupError, PermissionError):
        # Either reporting or returning a documented refusal must not retry it.
        pass
    assert child.terminated == 1
    assert [sig for sig in group.calls if sig != 0] == [signal.SIGTERM]


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["reused", "missing"])
async def test_lost_witness_is_not_signal_authority(patch_process_calls, loss):
    failure = _SignalFailure("first group signal failed")
    group = _Group(failure)
    patch_process_calls(group)
    child = _Child()
    first_killpg = group.killpg

    def first_signal_recycles_group(pgid, sig):
        if sig == signal.SIGTERM:
            try:
                return first_killpg(pgid, sig)
            finally:
                group.reused = loss == "reused"
                group.witness_missing = loss == "missing"
        return first_killpg(pgid, sig)

    _proc.os.killpg = first_signal_recycles_group
    with pytest.raises(_SignalFailure) as raised:
        await asyncio.wait_for(
            _proc.aterminate_process_group(child, grace=0.02, group_witnesses=_WITNESSES),
            timeout=1,
        )
    assert raised.value is failure
    assert child.terminated == 1
    assert signal.SIGKILL not in group.calls


@pytest.mark.asyncio
async def test_cancellation_retains_prior_group_error_in_exception_chain(patch_process_calls):
    failure = _SignalFailure("first group signal failed")
    group = _Group(failure)
    patch_process_calls(group)
    child = _Child(exits_on_terminate=False)
    task = asyncio.create_task(
        _proc.aterminate_process_group(child, grace=5, group_witnesses=_WITNESSES)
    )
    await asyncio.wait_for(group.first_attempt.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(task, timeout=1)
    assert child.terminated == 1
    assert signal.SIGKILL not in group.calls
    exc = raised.value
    chain = []
    while exc is not None and id(exc) not in {id(item) for item in chain}:
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__
    assert failure in chain


@pytest.mark.asyncio
async def test_eintr_rechecks_witness_and_escalates_then_preserves_error(
    patch_process_calls, monkeypatch
):
    import errno

    failure = OSError(errno.EINTR, "interrupted group signal")
    group = _Group(failure)
    patch_process_calls(group)
    identity_reads = []
    original_identity = group.identity

    def observe_identity(pid, pgid):
        result = original_identity(pid, pgid)
        identity_reads.append(result)
        return result

    monkeypatch.setattr(_proc, "_pinned_group_identity", observe_identity)
    child = _Child()
    with pytest.raises(OSError) as raised:
        await asyncio.wait_for(
            _proc.aterminate_process_group(child, grace=0.02, group_witnesses=_WITNESSES),
            timeout=1,
        )
    assert raised.value is failure
    assert raised.value.errno == errno.EINTR
    assert identity_reads.count(("found", _MEMBER)) >= 2
    assert [sig for sig in group.calls if sig != 0] == [signal.SIGTERM, signal.SIGKILL]
    assert child.terminated == 1
    assert child.killed == 1
    assert not group.descendant_alive


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_anyio_cancellation_propagates_with_prior_group_error(patch_process_calls, backend):
    if backend == "trio":
        pytest.importorskip("trio")
    import anyio

    async def exercise():
        failure = _SignalFailure("first group signal failed")
        group = _Group(failure)
        group.first_attempt = anyio.Event()
        patch_process_calls(group)
        child = _Child(exits_on_terminate=False)
        observed = []
        scope = anyio.CancelScope()

        async def terminate():
            with scope:
                try:
                    await _proc.aterminate_process_group(child, grace=5, group_witnesses=_WITNESSES)
                except anyio.get_cancelled_exc_class() as exc:
                    observed.append(exc)
                    raise

        with anyio.fail_after(1):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(terminate)
                await group.first_attempt.wait()
                scope.cancel()
        assert len(observed) == 1
        assert isinstance(observed[0], anyio.get_cancelled_exc_class())
        assert observed[0].__cause__ is failure or observed[0].__context__ is failure
        assert child.terminated == 1
        assert signal.SIGKILL not in group.calls

    anyio.run(exercise, backend=backend)


class _FirstSignalFailure(_SignalFailure):
    pass


class _RetrySignalFailure(_SignalFailure):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["same", "existing_cause", "earlier_points_to_later", "cycle"])
async def test_repeated_signal_errors_preserve_existing_chains_without_new_cycles(
    patch_process_calls, monkeypatch, caplog, case
):
    import logging

    first = _FirstSignalFailure("first signal failed")
    retry = first if case == "same" else _RetrySignalFailure("escalation failed")
    prior_cause = RuntimeError("retry's existing cause")
    if case == "existing_cause":
        retry.__cause__ = prior_cause
    elif case in {"earlier_points_to_later", "cycle"}:
        first.__cause__ = retry
        if case == "cycle":
            retry.__cause__ = first

    group = _Group(first)
    patch_process_calls(group)
    original_killpg = group.killpg

    def fail_on_both_signals(pgid, sig):
        if sig == signal.SIGKILL:
            group.calls.append(sig)
            raise retry
        return original_killpg(pgid, sig)

    monkeypatch.setattr(_proc.os, "killpg", fail_on_both_signals)
    child = _Child()
    with caplog.at_level(logging.WARNING, logger="lionagi.ln._proc"):
        with pytest.raises(_SignalFailure) as raised:
            await asyncio.wait_for(
                _proc.aterminate_process_group(child, grace=0.02, group_witnesses=_WITNESSES),
                timeout=1,
            )
    assert raised.value is retry
    assert child.terminated == 1
    assert child.killed == 1
    assert [sig for sig in group.calls if sig != 0] == [signal.SIGTERM, signal.SIGKILL]
    assert retry.__cause__ is not retry
    assert retry.__context__ is not retry
    if case == "existing_cause":
        assert retry.__cause__ is prior_cause
    elif case == "earlier_points_to_later":
        assert first.__cause__ is retry
        assert retry.__cause__ is None
    elif case == "cycle":
        # The source already contained a cycle: do not mutate either edge.
        assert first.__cause__ is retry
        assert retry.__cause__ is first
    if case != "same":
        assert "_FirstSignalFailure" in caplog.text


def test_unknown_backport_group_without_native_group_type_refuses_link(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    class BackportSignalGroup(Exception):
        def __init__(self, members):
            super().__init__("backport group")
            self.exceptions = members

    later = _RetrySignalFailure("escalation failed")
    # A backport member can refer to the later exception. Ignoring that
    # unknown member format would create a cycle when adding later -> first.
    earlier = BackportSignalGroup((later,))
    monkeypatch.setattr(_proc, "builtins", SimpleNamespace())
    with caplog.at_level(logging.WARNING, logger="lionagi.ln._proc"):
        _proc._attach_prior_group_error(later, earlier)
    assert later.__cause__ is None
    assert later.__context__ is None
    assert earlier.exceptions == (later,)
    assert "BackportSignalGroup" in caplog.text


@pytest.mark.parametrize("oversized", ["earlier", "later"])
def test_native_group_with_too_many_members_refuses_link_and_logs(caplog, oversized):
    import builtins
    import logging

    group_type = getattr(builtins, "BaseExceptionGroup", None)
    if group_type is None:
        pytest.skip("native exception groups require Python 3.11+")
    # Repeated members ensure the member-entry cap is checked independently of
    # a limit on distinct exception objects.
    member = RuntimeError("group member")
    group = group_type("large group", [member] * 129)
    earlier = group if oversized == "earlier" else _FirstSignalFailure("first")
    later = group if oversized == "later" else _RetrySignalFailure("retry")
    with caplog.at_level(logging.WARNING, logger="lionagi.ln._proc"):
        _proc._attach_prior_group_error(later, earlier)
    assert later.__cause__ is None
    assert later.__context__ is None
    assert len(group.exceptions) == 129
    assert all(child is member for child in group.exceptions)
    assert type(earlier).__name__ in caplog.text


def test_suppressed_context_logs_first_error_without_rewriting_chain(caplog):
    import logging

    earlier = _FirstSignalFailure("first")
    later = _RetrySignalFailure("retry")
    later.__context__ = earlier
    later.__suppress_context__ = True
    with caplog.at_level(logging.WARNING, logger="lionagi.ln._proc"):
        _proc._attach_prior_group_error(later, earlier)
    assert later.__cause__ is None
    assert later.__context__ is earlier
    assert later.__suppress_context__ is True
    assert "_FirstSignalFailure" in caplog.text
