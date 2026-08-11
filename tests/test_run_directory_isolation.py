"""The suite's run directory must be its own, whatever the environment says.

Verifies from the outside (a probe subprocess) that tests/conftest.py's
LIONAGI_HOME redirect holds even when the invoking environment already sets
LIONAGI_HOME. See docs/internals/ci.md#run-directory-isolation for the
mechanism and why a subprocess is required to observe it.
"""

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_REPO_ROOT = _TESTS_DIR.parent

# Reports the constants as lionagi bound them, from inside a suite that loaded
# the real tests/conftest.py. Writes rather than prints so the answer survives
# pytest's output capture.
_PROBE_TEST = """
import json
import os
from pathlib import Path

from lionagi import _paths


def test_report_bound_paths():
    Path(os.environ["PROBE_RESULT"]).write_text(
        json.dumps(
            {
                "lionagi_home": str(_paths.LIONAGI_HOME),
                "runs_root": str(_paths.RUNS_ROOT),
                "env_lionagi_home": os.environ["LIONAGI_HOME"],
            }
        )
    )
"""


# Makes the suite's own root impossible to delete via chmod(0o500) on a
# subdirectory, so rmtree walks in and stops. Paths are written down BEFORE
# the lock goes on, so a caller can find and clear the root even if this
# process hangs or is killed before reporting anything else.
_UNREMOVABLE_PROBE_TEST = """
import json
import os
from pathlib import Path


def test_leave_the_run_directory_unremovable():
    home = Path(os.environ["LIONAGI_HOME"])
    locked = home / "locked"
    locked.mkdir(parents=True, exist_ok=True)
    (locked / "kept").write_text("x")
    Path(os.environ["PROBE_RESULT"]).write_text(
        json.dumps({"lionagi_home": str(home), "locked": str(locked)})
    )
    locked.chmod(0o500)
"""


def _undo_lock_and_remove(reported: dict) -> None:
    """Restore whatever a probe made unremovable, then delete its root.

    Works from what the probe wrote down rather than a live handle, so it can
    run even after a call that raised before it could hand anything back. The
    removal is unguarded (its error is what tells a reader whether to fix
    permissions or free disk) and the path is checked again afterwards,
    because a removal that raises nothing has still failed if the directory
    is there -- reporting otherwise is the exact failure this module exists
    to catch.
    """
    locked = reported.get("locked")
    if locked and os.path.exists(locked):
        try:
            os.chmod(locked, stat.S_IRWXU)
        except OSError as e:
            raise RuntimeError(f"could not unlock {locked}: {e}") from e
    home = reported.get("lionagi_home")
    if not home or not os.path.exists(home):
        # Nothing recorded, or the probe's own cleanup already got there --
        # which is the ordinary case for a probe that locked nothing.
        return
    try:
        shutil.rmtree(home)
    except OSError as e:
        raise RuntimeError(f"could not remove the probe run directory {home}: {e}") from e
    if os.path.exists(home):
        raise RuntimeError(
            f"the probe run directory {home} is still on disk after a removal that raised nothing"
        )


def _recover_reported_roots(result_paths) -> None:
    """Undo every recorded lock, and name every one that could not be undone.

    Every record gets its turn even when an earlier one fails, since stopping
    at the first failure would strand the later roots with nothing coming
    back for them; failures are collected and raised together at the end.
    The per-record guard is deliberately wide -- whatever it catches is named
    in the consolidated failure, rather than letting one unexpected error
    decide the remaining roots aren't worth attempting.
    """
    failures = []
    for result_path in result_paths:
        try:
            reported = json.loads(Path(result_path).read_text())
        except (OSError, ValueError):
            # No record, or an unusable one: there is nothing to act on. A
            # probe that never got as far as writing never got as far as
            # locking either.
            continue
        try:
            _undo_lock_and_remove(reported)
        except Exception as e:  # noqa: BLE001 -- re-raised below, never discarded
            failures.append(f"  {e}")
    if failures:
        raise RuntimeError(
            "probe run directories were left behind and could not be recovered:\n"
            + "\n".join(failures)
        )


@pytest.fixture
def probe(tmp_path):
    """Run one probe pytest and hand back what it reported from inside.

    The probe file lives in a dot-directory under tests/ so real conftest
    discovery applies to it (a file under /tmp would collect nothing) while
    pytest's default norecursedirs keeps an ordinary suite run from picking
    it up -- passing the file path explicitly collects it anyway. Recorded
    paths are read back at teardown to clean up after a call that never
    returned (timeout, unparseable result), and are also hung off the
    returned callable so a test can drive the same recovery and read what it
    reports.
    """

    reported_paths: list[Path] = []

    def _run(
        env_overrides: dict[str, str], source: str = _PROBE_TEST
    ) -> tuple[subprocess.CompletedProcess, dict]:
        probe_dir = Path(tempfile.mkdtemp(prefix=".run-isolation-probe-", dir=_TESTS_DIR))
        result_path = tmp_path / f"bound-{len(reported_paths)}.json"
        reported_paths.append(result_path)
        try:
            (probe_dir / "test_probe.py").write_text(source)

            env = dict(os.environ)
            env.pop("LIONAGI_TEST_HOME", None)
            env["PROBE_RESULT"] = str(result_path)
            env.update(env_overrides)

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(probe_dir / "test_probe.py"),
                    # -n0 beats the -n auto in addopts: the probe is one test and
                    # xdist workers would only add startup cost.
                    "-n0",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                env=env,
                timeout=300,
            )
            bound = json.loads(result_path.read_text()) if result_path.exists() else {}
            return completed, bound
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)

    _run.reported_paths = reported_paths

    yield _run

    _recover_reported_roots(reported_paths)


def test_preset_lionagi_home_does_not_become_the_suite_root(probe, tmp_path):
    """A ``LIONAGI_HOME`` already in the environment must not be adopted."""
    caller_home = tmp_path / "caller-store"
    caller_home.mkdir()

    completed, bound = probe({"LIONAGI_HOME": str(caller_home)})

    assert completed.returncode == 0, (
        f"probe pytest failed (rc={completed.returncode}):\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert bound, "probe produced no result file"

    bound_home = Path(bound["lionagi_home"])
    assert bound_home != caller_home, (
        "the suite adopted the caller's LIONAGI_HOME as its run directory root; "
        f"tests would write into {caller_home}"
    )
    assert caller_home not in bound_home.parents, (
        f"the suite's root {bound_home} is inside the caller's store {caller_home}"
    )
    # Not merely different: a root the suite made for itself, under the
    # temporary directory it cleans up at exit.
    assert Path(tempfile.gettempdir()).resolve() in bound_home.resolve().parents
    assert Path(bound["runs_root"]) == bound_home / "runs"
    assert bound["env_lionagi_home"] == str(bound_home)

    # The caller's directory is not just unbound, it is untouched.
    assert list(caller_home.iterdir()) == []


def test_lionagi_test_home_is_the_deliberate_way_through(probe, tmp_path):
    """``LIONAGI_TEST_HOME`` points the suite somewhere specific, on purpose."""
    caller_home = tmp_path / "caller-store"
    caller_home.mkdir()
    chosen_home = tmp_path / "chosen-store"

    completed, bound = probe(
        {"LIONAGI_HOME": str(caller_home), "LIONAGI_TEST_HOME": str(chosen_home)}
    )

    assert completed.returncode == 0, (
        f"probe pytest failed (rc={completed.returncode}):\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert bound, "probe produced no result file"
    assert Path(bound["lionagi_home"]) == chosen_home
    assert Path(bound["runs_root"]) == chosen_home / "runs"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root may unlink from a directory it cannot write to, so nothing here is unremovable",
)
def test_a_root_that_cannot_be_removed_is_reported(probe, tmp_path):
    """A cleanup that fails must say which directory it left behind, and why.

    atexit cleanup runs past the point where a failure can be a test result,
    so only another process watching this one exit can observe it. The probe
    forces the removal to fail via ordinary permissions; this reads the
    exiting process's stderr.
    """
    caller_home = tmp_path / "caller-store"
    caller_home.mkdir()

    completed, bound = probe({"LIONAGI_HOME": str(caller_home)}, source=_UNREMOVABLE_PROBE_TEST)

    try:
        assert completed.returncode == 0, (
            f"probe pytest failed (rc={completed.returncode}):\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        assert bound, "probe produced no result file"

        home = Path(bound["lionagi_home"])
        assert home.exists(), (
            f"{home} was removed after all, so this test forced no failure and "
            "proves nothing about how one is reported"
        )
        assert str(home) in completed.stderr, (
            "the suite left a temporary run directory behind and said nothing that "
            f"names it; stderr:\n{completed.stderr}"
        )
        # The error too, not just the path: a reader has to be able to tell a
        # permission problem from a full disk.
        assert os.strerror(errno.EACCES) in completed.stderr, (
            f"the report does not name the error that stopped the removal:\n{completed.stderr}"
        )
    finally:
        # Whatever the assertions did, nothing unremovable outlives this test.
        if bound:
            _undo_lock_and_remove(bound)

    assert not Path(bound["lionagi_home"]).exists(), (
        "this test could not remove what it made unremovable"
    )


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root may unlink from a directory it cannot write to, so nothing here is unremovable",
)
def test_one_failed_recovery_neither_hides_itself_nor_strands_the_next_root(
    probe, tmp_path, monkeypatch
):
    """Recovering two locked roots, where the first recovery cannot be done.

    The first root's unlocking is turned into a no-op so its removal cannot
    succeed -- the failure a removal that ignored its own errors would carry
    out and report as done. Asserts the failure reaches the caller naming the
    root and the reason, and that the second root is recovered anyway; a loop
    that stops at the first failure would leave it locked with nothing coming
    back for it.
    """
    caller_home = tmp_path / "caller-store"
    caller_home.mkdir()

    _, first = probe({"LIONAGI_HOME": str(caller_home)}, source=_UNREMOVABLE_PROBE_TEST)
    _, second = probe({"LIONAGI_HOME": str(caller_home)}, source=_UNREMOVABLE_PROBE_TEST)

    assert first and second, "a probe produced no result file"
    first_home = Path(first["lionagi_home"])
    second_home = Path(second["lionagi_home"])
    assert first_home != second_home
    assert first_home.exists() and second_home.exists(), (
        "both probes were cleaned up after all, so this test forced no failure "
        "and proves nothing about recovering from one"
    )

    real_chmod = os.chmod

    def _leave_the_first_locked(path, mode, *args, **kwargs):
        if str(path) == first["locked"]:
            return None
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", _leave_the_first_locked)
    try:
        with pytest.raises(RuntimeError) as failure:
            _recover_reported_roots(probe.reported_paths)

        reported = str(failure.value)
        assert str(first_home) in reported, (
            f"the failed recovery does not name the root it left behind:\n{reported}"
        )
        assert os.strerror(errno.EACCES) in reported, (
            f"the failed recovery does not name the error that stopped it:\n{reported}"
        )
        # The failure was real, not a message about a directory that went away
        # on its own.
        assert first_home.exists(), (
            f"{first_home} was recovered after all, so the reported failure is false"
        )
        # And the point: the later record was still attempted.
        assert not second_home.exists(), (
            f"{second_home} is still locked on disk because an earlier recovery failed; "
            "it was recorded and nothing else will come back for it"
        )
    finally:
        monkeypatch.setattr(os, "chmod", real_chmod)
        # Now that permissions can be restored, the same recovery finishes the
        # job -- and says nothing, because there is nothing left to say.
        _recover_reported_roots(probe.reported_paths)

    assert not first_home.exists(), "this test could not remove what it made unremovable"


def test_a_removal_that_exhausts_the_stack_is_reported_too(monkeypatch, capsys):
    """Not every way a removal fails comes from the filesystem.

    rmtree descends recursively, so a tree deep enough exhausts the stack and
    raises RecursionError while every directory in it is perfectly removable.
    The root is still left behind, so this must reach the same stderr
    message rather than escape as an atexit traceback.
    """
    from tests.conftest import _remove_test_home

    def _too_deep(_root):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(shutil, "rmtree", _too_deep)

    _remove_test_home("/nowhere/deep-root")

    reported = capsys.readouterr().err
    assert "/nowhere/deep-root" in reported, (
        f"the report does not name the root that was left behind:\n{reported}"
    )
    assert "maximum recursion depth exceeded" in reported, (
        f"the report does not name the error that stopped the removal:\n{reported}"
    )


def test_a_bug_in_the_removal_is_not_dressed_up_as_a_cleanup_failure(monkeypatch, capsys):
    """Only the filesystem's refusals are turned into a message.

    Calling rmtree wrongly is a defect in this suite, not a directory the
    machine would not delete; reporting it as the latter would send a reader
    hunting for a root that isn't there.
    """
    from tests.conftest import _remove_test_home

    def _misused(_root):
        raise TypeError("rmtree() got an unexpected keyword argument")

    monkeypatch.setattr(shutil, "rmtree", _misused)

    with pytest.raises(TypeError):
        _remove_test_home("/nowhere/deep-root")

    assert "/nowhere/deep-root" not in capsys.readouterr().err
