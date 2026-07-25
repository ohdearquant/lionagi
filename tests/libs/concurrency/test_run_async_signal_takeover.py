# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Which signals ``run_async`` takes over, and which it leaves alone.

``run_async`` installs its own SIGINT/SIGTERM handlers so a signal cancels the
inner task, and restores the previous ones when it returns. ``getsignal()``
reports ``None`` for a handler installed outside Python, and
``signal.signal(signum, None)`` raises, so a handler taken over in that state
could never be given back: the restore would fail and this runner's handler
would stay installed for the rest of the process, swallowing the signal a
long-lived caller relies on.
"""

import signal as signal_module

import pytest

from lionagi.ln.concurrency.utils import run_async


async def _work():
    return 42


@pytest.fixture
def signal_probe(monkeypatch):
    """Report ``None`` as the prior handler for chosen signals, and record
    every takeover so the decision itself can be asserted."""
    real_getsignal = signal_module.getsignal
    real_signal = signal_module.signal
    installed: list[int] = []

    def probe(*unrestorable):
        def fake_getsignal(signum):
            return None if signum in unrestorable else real_getsignal(signum)

        def fake_signal(signum, handler):
            installed.append(signum)
            return real_signal(signum, handler)

        monkeypatch.setattr(signal_module, "getsignal", fake_getsignal)
        monkeypatch.setattr(signal_module, "signal", fake_signal)

    return probe, installed


def test_abstains_from_a_signal_whose_handler_cannot_be_restored(signal_probe):
    probe, installed = signal_probe
    probe(signal_module.SIGINT)

    assert run_async(_work()) == 42
    assert signal_module.SIGINT not in installed


def test_abstaining_is_per_signal_not_all_or_nothing(signal_probe):
    """Only the signal that cannot be given back is skipped; cancellation
    wiring for the other one is still worth having."""
    probe, installed = signal_probe
    probe(signal_module.SIGINT)

    run_async(_work())

    assert signal_module.SIGTERM in installed


def test_the_signal_it_does_take_over_is_handed_back(signal_probe):
    """Abstaining on one signal must not cost the restore of the other."""
    probe, _installed = signal_probe
    before = signal_module.getsignal(signal_module.SIGTERM)
    probe(signal_module.SIGINT)

    run_async(_work())

    assert signal_module.getsignal(signal_module.SIGTERM) is before


def test_a_failed_restore_does_not_strand_the_other_signal(monkeypatch):
    """Restores run in a finally, often while an exception is already
    propagating. One of them failing must not leave the other signal still
    overridden for the rest of the process."""
    real_signal = signal_module.signal
    original_sigint = signal_module.getsignal(signal_module.SIGINT)
    restored: list[int] = []
    calls = {"n": 0}

    def flaky_signal(signum, handler):
        calls["n"] += 1
        if calls["n"] <= 2:  # the two installs
            return real_signal(signum, handler)
        if signum == signal_module.SIGINT:  # first restore fails
            raise OSError("cannot restore")
        restored.append(signum)
        return real_signal(signum, handler)

    monkeypatch.setattr(signal_module, "signal", flaky_signal)
    try:
        assert run_async(_work()) == 42
        assert signal_module.SIGTERM in restored
    finally:
        real_signal(signal_module.SIGINT, original_sigint)


def test_both_signals_unrestorable_still_runs_the_work(signal_probe):
    """Nothing about signal wiring is load-bearing for producing a result."""
    probe, installed = signal_probe
    probe(signal_module.SIGINT, signal_module.SIGTERM)

    assert run_async(_work()) == 42
    assert installed == []
