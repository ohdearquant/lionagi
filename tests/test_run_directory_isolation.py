"""The suite's run directory must be its own, whatever the environment says.

``tests/conftest.py`` redirects ``LIONAGI_HOME`` to a temporary directory before
any lionagi import, because ``lionagi._paths`` reads it once at import and
derives ``RUNS_ROOT`` from it. The redirect is only worth anything if it also
holds when the invoking environment already sets ``LIONAGI_HOME`` — that is the
case where a developer's real store, or a persistent CI one, is what the suite
would otherwise write into, and it is the case a conftest that only fills in a
missing value gets wrong while looking correct.

Checking that from inside the running suite is not possible: by then the
redirect has already happened and the original environment is gone. So this
launches a second pytest with ``LIONAGI_HOME`` pre-set to a disposable
directory and asks the collected code what it actually bound.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_REPO_ROOT = _TESTS_DIR.parent

# Reports the constants as lionagi bound them, from inside a suite that loaded
# the real tests/conftest.py. Writes rather than prints so the answer survives
# whatever pytest does to captured output.
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


@pytest.fixture
def probe(tmp_path):
    """Run one probe pytest and hand back what lionagi bound inside it.

    The probe file lives in a dot-directory under ``tests/`` so that the real
    conftest applies to it (conftest discovery walks up the filesystem, so a
    file in ``/tmp`` would collect nothing) while pytest's default
    ``norecursedirs`` keeps an ordinary suite run from picking it up. Passing
    the file path explicitly collects it anyway.
    """

    def _run(env_overrides: dict[str, str]) -> tuple[subprocess.CompletedProcess, dict]:
        probe_dir = Path(tempfile.mkdtemp(prefix=".run-isolation-probe-", dir=_TESTS_DIR))
        result_path = tmp_path / "bound.json"
        try:
            (probe_dir / "test_probe.py").write_text(_PROBE_TEST)

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

    return _run


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
