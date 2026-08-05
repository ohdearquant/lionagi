# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The periodic stack dump is off unless asked for, and says why when it refuses.

The disarmed arm is the load-bearing one. A diagnostic hook that costs anything
when nobody asked for it is a worse problem than the hang it exists to
diagnose, so "no timer, no file, no behaviour change" is asserted directly
rather than inferred from the code path not being reached.

Every refusal is asserted through the channel that carries it. The hook prints
to stderr, so a test that only checked "it did not arm" would pass on a version
that refuses silently, which is the shape the flag-says-whether-log-says-why
rule exists to prevent.
"""

from __future__ import annotations

import faulthandler
import time
from pathlib import Path

import pytest

from lionagi.studio import _traceback_dump as td


@pytest.fixture(autouse=True)
def _never_leave_a_timer_armed():
    """A leaked timer would dump stacks into a closed file for the rest of the run."""
    yield
    td.disarm_traceback_dump()


@pytest.fixture
def spy(monkeypatch):
    """Record calls to the stdlib arming call instead of making them."""
    calls: list[dict] = []

    def fake(timeout, repeat=False, file=None, exit=False):  # noqa: A002
        calls.append({"timeout": timeout, "repeat": repeat, "file": file, "exit": exit})

    # td holds the module, not the function, so one patch covers both names.
    monkeypatch.setattr(faulthandler, "dump_traceback_later", fake)
    return calls


def _outside_any_checkout(tmp_path: Path) -> Path:
    """pytest's tmp_path is outside the repo, but assert it rather than assume."""
    assert td._inside_a_git_tree(tmp_path) is None, (
        f"{tmp_path} is inside a checkout, so it cannot stand in for an external path"
    )
    return tmp_path


def test_it_does_not_arm_when_the_variable_is_absent(tmp_path, monkeypatch, spy, capsys):
    monkeypatch.delenv(td._ENV_PATH, raising=False)
    target = _outside_any_checkout(tmp_path) / "dump.txt"

    assert td.arm_traceback_dump() is False
    assert spy == [], "a timer was armed with nothing asking for one"
    assert not target.exists()
    assert capsys.readouterr().err == "", "the disarmed path must be silent as well as inert"


def test_it_arms_on_the_variable_and_appends_to_the_named_path(tmp_path, monkeypatch, spy):
    target = _outside_any_checkout(tmp_path) / "nested" / "dump.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))

    assert td.arm_traceback_dump() is True

    assert len(spy) == 1
    call = spy[0]
    assert call["repeat"] is True, "a single dump cannot show whether a frame is stuck"
    assert call["timeout"] == td._DEFAULT_INTERVAL_SECONDS
    assert call["file"] is not None
    assert target.exists(), "the file is opened at arm time, not at the first dump"
    assert call["file"].mode == "a", "an overwriting handle loses every dump but the last"


def test_the_interval_is_configurable(tmp_path, monkeypatch, spy):
    target = _outside_any_checkout(tmp_path) / "dump.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))
    monkeypatch.setenv(td._ENV_INTERVAL, "3.5")

    assert td.arm_traceback_dump() is True
    assert spy[0]["timeout"] == 3.5


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5"])
def test_a_bad_interval_refuses_out_loud_rather_than_arming_a_default(
    tmp_path, monkeypatch, spy, capsys, bad
):
    target = _outside_any_checkout(tmp_path) / "dump.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))
    monkeypatch.setenv(td._ENV_INTERVAL, bad)

    assert td.arm_traceback_dump() is False
    assert spy == []
    err = capsys.readouterr().err
    assert "not armed" in err, err
    assert td._ENV_INTERVAL in err, err


def test_it_refuses_a_path_inside_a_checkout(monkeypatch, spy, capsys):
    """A dump under a tracked tree is one `git add -A` away from being committed.

    The candidate path is a real one inside this checkout, because a fake one
    would not exercise the check. Cleaned up unconditionally: a version that
    creates the file before refusing would otherwise leave it in the working
    tree, where the next run of this same test reads it as the earlier failure
    rather than its own.
    """
    inside = Path(__file__).resolve().parent / "refused-dump.txt"
    inside.unlink(missing_ok=True)
    assert td._inside_a_git_tree(inside.parent) is not None, (
        "this test's own directory is not in a checkout, so it proves nothing"
    )
    monkeypatch.setenv(td._ENV_PATH, str(inside))

    assert td.arm_traceback_dump() is False
    assert spy == []
    assert not inside.exists(), "the refused path must not be created on the way to refusing"
    err = capsys.readouterr().err
    assert "not armed" in err and "git tree" in err, err
    inside.unlink(missing_ok=True)


def test_an_unwritable_path_refuses_out_loud_rather_than_arming_nothing(
    tmp_path, monkeypatch, spy, capsys
):
    """The failure the operator is most likely to hit, and the one silence hides best."""
    blocker = _outside_any_checkout(tmp_path) / "not-a-directory"
    blocker.write_text("")
    target = blocker / "dump.txt"  # parent is a regular file, so mkdir and open both fail
    monkeypatch.setenv(td._ENV_PATH, str(target))

    assert td.arm_traceback_dump() is False
    assert spy == []
    err = capsys.readouterr().err
    assert "not armed" in err, err
    assert str(target) in err, "the refusal must name the path the operator set"


def test_disarm_is_safe_when_nothing_was_ever_armed(monkeypatch, spy):
    monkeypatch.delenv(td._ENV_PATH, raising=False)

    td.disarm_traceback_dump()
    td.disarm_traceback_dump()


def test_a_real_arm_writes_real_stacks(tmp_path, monkeypatch):
    """One end-to-end pass with the stdlib call unmocked.

    Every other test here spies on ``dump_traceback_later``, so all of them
    would pass on a version that arms something which never writes anything.
    """
    target = _outside_any_checkout(tmp_path) / "real.txt"
    monkeypatch.setenv(td._ENV_PATH, str(target))
    monkeypatch.setenv(td._ENV_INTERVAL, "0.2")

    assert td.arm_traceback_dump() is True
    try:
        deadline = time.time() + 5.0
        while target.stat().st_size == 0 and time.time() < deadline:
            time.sleep(0.05)
        content = target.read_text()
    finally:
        td.disarm_traceback_dump()

    assert "Thread" in content or "File " in content, (
        f"the armed timer produced no stack text within the window: {content[:200]!r}"
    )
