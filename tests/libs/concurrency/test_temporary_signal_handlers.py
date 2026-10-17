# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``temporary_signal_handlers`` takes over signals for one block and gives them back.

It follows ``run_async``'s takeover rule: a signal whose handler was installed
outside Python (``getsignal()`` reports ``None``) cannot be reinstalled, so it
is left alone, and nothing is installed off the main thread, where
``signal.signal`` raises.
"""

import signal as signal_module
import threading

import pytest

from lionagi.ln.concurrency import temporary_signal_handlers

SIGNALS = (signal_module.SIGINT, signal_module.SIGTERM)


def _handler(signum, frame):
    pass


@pytest.fixture
def saved_handlers():
    """Put back whatever a test leaves behind, so a failure cannot leak handlers.

    Holds the real ``signal.signal``: a test's own patch can still be in place
    when this teardown runs.
    """
    real_signal = signal_module.signal
    saved = {signum: signal_module.getsignal(signum) for signum in SIGNALS}
    yield saved
    for signum, prior in saved.items():
        real_signal(signum, prior)


def test_the_handler_is_installed_inside_the_block_and_the_prior_one_comes_back(
    saved_handlers,
):
    with temporary_signal_handlers(_handler):
        assert all(signal_module.getsignal(signum) is _handler for signum in SIGNALS)

    assert {signum: signal_module.getsignal(signum) for signum in SIGNALS} == saved_handlers


def test_the_prior_handlers_come_back_when_the_block_raises(saved_handlers):
    with pytest.raises(KeyboardInterrupt), temporary_signal_handlers(_handler):
        raise KeyboardInterrupt

    assert {signum: signal_module.getsignal(signum) for signum in SIGNALS} == saved_handlers


def test_only_the_named_signals_are_taken_over(saved_handlers):
    with temporary_signal_handlers(_handler, signals=(signal_module.SIGTERM,)):
        assert signal_module.getsignal(signal_module.SIGTERM) is _handler
        assert signal_module.getsignal(signal_module.SIGINT) is saved_handlers[signal_module.SIGINT]


def test_a_signal_whose_handler_cannot_be_given_back_is_left_alone(saved_handlers, monkeypatch):
    real_getsignal = signal_module.getsignal
    real_signal = signal_module.signal
    installed: list[int] = []

    def fake_getsignal(signum):
        return None if signum == signal_module.SIGINT else real_getsignal(signum)

    def fake_signal(signum, handler):
        installed.append(signum)
        return real_signal(signum, handler)

    monkeypatch.setattr(signal_module, "getsignal", fake_getsignal)
    monkeypatch.setattr(signal_module, "signal", fake_signal)

    with temporary_signal_handlers(_handler):
        assert installed == [signal_module.SIGTERM]

    # SIGTERM installed, then given back; SIGINT never touched.
    assert installed == [signal_module.SIGTERM, signal_module.SIGTERM]
    assert real_getsignal(signal_module.SIGTERM) is saved_handlers[signal_module.SIGTERM]


def test_nothing_is_installed_off_the_main_thread(saved_handlers):
    outcome: list[BaseException | None] = []

    def body():
        try:
            with temporary_signal_handlers(_handler):
                pass
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=body)
    worker.start()
    worker.join(timeout=10)

    assert outcome == [None]
    assert {signum: signal_module.getsignal(signum) for signum in SIGNALS} == saved_handlers


def test_a_failed_restore_does_not_strand_the_other_signal(saved_handlers, monkeypatch, caplog):
    real_signal = signal_module.signal
    restored: list[int] = []
    calls = {"n": 0}

    def flaky_signal(signum, handler):
        calls["n"] += 1
        if calls["n"] <= 2:  # the two installs
            return real_signal(signum, handler)
        if signum == signal_module.SIGINT:  # the first restore fails
            raise OSError("cannot restore")
        restored.append(signum)
        return real_signal(signum, handler)

    monkeypatch.setattr(signal_module, "signal", flaky_signal)

    with temporary_signal_handlers(_handler):
        pass

    assert restored == [signal_module.SIGTERM]
    assert signal_module.getsignal(signal_module.SIGTERM) is saved_handlers[signal_module.SIGTERM]
    assert "could not restore the previous handler" in caplog.text


def test_a_failed_install_gives_back_the_signals_already_taken(saved_handlers, monkeypatch):
    real_signal = signal_module.signal

    def refuse_sigterm(signum, handler):
        if signum == signal_module.SIGTERM and handler is _handler:
            raise OSError("cannot install")
        return real_signal(signum, handler)

    monkeypatch.setattr(signal_module, "signal", refuse_sigterm)

    with pytest.raises(OSError, match="cannot install"):
        with temporary_signal_handlers(_handler):
            pytest.fail("the block must not run when an install fails")

    assert signal_module.getsignal(signal_module.SIGINT) is saved_handlers[signal_module.SIGINT]
