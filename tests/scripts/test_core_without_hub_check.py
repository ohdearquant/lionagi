"""The core-without-hub ratchet: its list, its comparison, its control and its plugin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_core_without_hub as check

ROOT = Path(__file__).resolve().parents[2]


def _report(modules: dict[str, dict[str, int]], exit_code: int = 1) -> dict:
    return {"refused": "lionhub", "exit_code": exit_code, "modules": modules}


def _entry(tests: int = 1, failures: int = 0, hub: int = 0) -> dict[str, int]:
    return {"tests": tests, "failures": failures, "hub_failures": hub}


def test_the_list_reads_paths_and_ignores_comments_and_blank_lines():
    text = "# header\n\ntests/a/test_x.py\n  tests/b/test_y.py  # why\n"
    assert check.read_list(text) == ["tests/a/test_x.py", "tests/b/test_y.py"]


def test_a_module_listed_twice_is_refused():
    with pytest.raises(ValueError, match="listed twice"):
        check.read_list("tests/a.py\ntests/a.py\n")


def test_only_failures_caused_by_the_hub_count():
    report = _report(
        {
            "tests/a.py": _entry(failures=2, hub=1),
            "tests/b.py": _entry(failures=3, hub=0),
            "tests/c.py": _entry(),
        }
    )
    assert check.hub_modules(report) == {"tests/a.py"}


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5])
def test_a_run_that_did_not_complete_is_refused(exit_code):
    with pytest.raises(ValueError, match="did not complete"):
        check.hub_modules(_report({"tests/a.py": _entry()}, exit_code=exit_code))


@pytest.mark.parametrize(
    "modules", [{}, {"tests/a.py": _entry(tests=0)}], ids=["no-module", "no-test"]
)
def test_a_run_that_saw_nothing_is_refused(modules):
    with pytest.raises(ValueError, match="checked nothing"):
        check.hub_modules(_report(modules))


def test_a_matching_list_has_no_problem():
    assert check.problems({"tests/a.py"}, ["tests/a.py"], base=["tests/a.py", "tests/b.py"]) == []


def test_each_disagreement_is_named():
    found = check.problems(
        needing={"tests/new.py", "tests/kept.py"},
        listed=["tests/kept.py", "tests/fixed.py", "tests/added.py"],
        base=["tests/kept.py", "tests/fixed.py"],
    )
    assert found == [
        "tests/new.py fails with lionhub refused and is not on the list",
        "tests/added.py no longer needs lionhub: take it off the list",
        "tests/fixed.py no longer needs lionhub: take it off the list",
        "tests/added.py was added to the list, which may only shrink",
    ]


def test_the_control_catches_both_planted_changes():
    assert check.control({"tests/a.py", "tests/b.py"}, ["tests/a.py", "tests/b.py"]) == []
    assert check.control(set(), []) == []


def test_the_control_reports_a_comparison_that_misses(monkeypatch):
    monkeypatch.setattr(check, "problems", lambda needing, listed, base=None: [])
    missed = check.control({"tests/a.py"}, ["tests/a.py"])
    assert missed == [
        "taking tests/a.py off the list was not caught",
        "a module that needs the hub and is not listed was not caught",
    ]


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_main_passes_on_a_matching_list_and_fails_on_a_shortened_one(tmp_path, capsys):
    report = _write(
        tmp_path,
        "report.json",
        json.dumps(_report({"tests/a.py": _entry(failures=1, hub=1), "tests/b.py": _entry()})),
    )
    listed = _write(tmp_path, "list.txt", "tests/a.py\n")
    empty = _write(tmp_path, "empty.txt", "# nothing\n")
    base = _write(tmp_path, "base.txt", "tests/a.py\n")

    assert check.main(["--report", str(report), "--list", str(listed), "--control"]) == 0
    assert check.main(["--report", str(report), "--list", str(empty)]) == 1
    assert "tests/a.py fails with lionhub refused" in capsys.readouterr().out
    grown = _write(tmp_path, "grown.txt", "tests/a.py\ntests/b.py\n")
    rc = check.main(["--report", str(report), "--list", str(grown), "--base-list", str(base)])
    assert rc == 1
    assert "tests/b.py was added to the list" in capsys.readouterr().out


def test_main_cannot_check_without_a_report(tmp_path):
    listed = _write(tmp_path, "list.txt", "")
    assert check.main(["--report", str(tmp_path / "absent.json"), "--list", str(listed)]) == 2


def test_the_checked_in_list_has_no_duplicate_and_names_existing_modules():
    listed = check.read_list(check.LIST_PATH.read_text(encoding="utf-8"))
    missing = [name for name in listed if not (ROOT / name).is_file()]
    assert missing == []


def _run_plugin(tmp_path: Path, plugin: bool) -> dict | None:
    """Run a small session in ``tmp_path`` and return the plugin's report, if one was written."""
    _write(tmp_path, "test_bare_hub.py", "def test_bare_hub():\n    import lionhub\n")
    _write(
        tmp_path,
        "test_lazy_hub.py",
        "import pytest\n\n"
        "def test_lazy_hub():\n"
        "    try:\n"
        "        import lionhub.providers.manifest\n"
        "    except ImportError:\n"
        "        pytest.fail('no provider')\n",
    )
    _write(
        tmp_path, "test_hub_at_import.py", "import lionhub\n\ndef test_never_runs():\n    pass\n"
    )
    _write(tmp_path, "test_other_failure.py", "def test_other():\n    assert 1 == 2\n")
    _write(tmp_path, "test_passes.py", "def test_passes():\n    assert True\n")
    report = tmp_path / "out" / "report.json"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "lionhub" / "src")]),
        check.REPORT_ENV: str(report),
    }
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-n0"]
    argv += ["--continue-on-collection-errors", "--rootdir", str(tmp_path), str(tmp_path)]
    if plugin:
        argv[3:3] = ["-p", "scripts.hubless_pytest_plugin"]
    subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, timeout=120, check=False)
    if not report.is_file():
        return None
    return json.loads(report.read_text(encoding="utf-8"))


def test_the_plugin_refuses_the_hub_and_attributes_only_its_failures(tmp_path):
    report = _run_plugin(tmp_path, plugin=True)
    assert report is not None
    assert report["exit_code"] == 1
    assert check.hub_modules(report) == {
        "test_bare_hub.py",
        "test_lazy_hub.py",
        "test_hub_at_import.py",
    }
    modules = report["modules"]
    assert modules["test_other_failure.py"]["failures"] == 1
    assert modules["test_passes.py"] == _entry()


def test_without_the_plugin_the_hub_imports_and_nothing_is_reported(tmp_path):
    assert _run_plugin(tmp_path, plugin=False) is None
