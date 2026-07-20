from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "benchmarks"))

from ci_check_provenance import PYTHON_IDENTITY_META_KEYS, check  # noqa: E402

SUITE = "concurrency-asyncio"
PYTHON_IDENTITY = {
    "python_full_version": "3.12.1 (stable)",
    "python_build": "main build",
    "python_compiler": "Clang 16",
}


def _write_result(
    directory: Path,
    *,
    lionagi_file: str,
    scenarios: set[str],
    meta_overrides: dict[str, str] | None = None,
) -> None:
    directory.mkdir()
    meta = {
        "lionagi_file": lionagi_file,
        "anyio": "4.9.0",
        **PYTHON_IDENTITY,
        **(meta_overrides or {}),
    }
    payload = {"meta": meta, "results": {name: {} for name in scenarios}}
    (directory / f"{SUITE}.json").write_text(json.dumps(payload), encoding="utf-8")


def _check_pair(
    tmp_path: Path,
    *,
    baseline_scenarios: set[str] | None = None,
    current_scenarios: set[str] | None = None,
    same_install: bool = False,
    current_meta: dict[str, str] | None = None,
) -> bool:
    baseline_dir = tmp_path / "baseline"
    current_dir = tmp_path / "current"
    baseline_file = "baseline/site-packages/lionagi/__init__.py"
    current_file = baseline_file if same_install else "current/site-packages/lionagi/__init__.py"
    _write_result(
        baseline_dir,
        lionagi_file=baseline_file,
        scenarios=baseline_scenarios if baseline_scenarios is not None else {"shared"},
    )
    _write_result(
        current_dir,
        lionagi_file=current_file,
        scenarios=current_scenarios if current_scenarios is not None else {"shared"},
        meta_overrides=current_meta,
    )
    return check(baseline_dir, current_dir, [SUITE])


def test_check_rejects_same_install_path(tmp_path, capsys):
    assert not _check_pair(tmp_path, same_install=True)
    assert "SAME path" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("baseline_scenarios", "current_scenarios", "message"),
    [
        (set(), {"shared"}, "baseline reported zero overlapping scenarios"),
        ({"shared"}, set(), "current reported zero overlapping scenarios"),
        ({"baseline-only"}, {"current-only"}, "disjoint sets"),
        ({"shared", "dropped"}, {"shared"}, "MISSING from current"),
    ],
)
def test_check_rejects_invalid_scenario_coverage(
    tmp_path, capsys, baseline_scenarios, current_scenarios, message
):
    assert not _check_pair(
        tmp_path,
        baseline_scenarios=baseline_scenarios,
        current_scenarios=current_scenarios,
    )
    assert message in capsys.readouterr().err


def test_check_rejects_dependency_version_mismatch(tmp_path, capsys):
    assert not _check_pair(tmp_path, current_meta={"anyio": "4.10.0"})
    assert "anyio version differs" in capsys.readouterr().err


@pytest.mark.parametrize("identity_key", PYTHON_IDENTITY_META_KEYS)
def test_check_rejects_python_identity_mismatch(tmp_path, capsys, identity_key):
    assert not _check_pair(tmp_path, current_meta={identity_key: "different"})
    assert f"{identity_key} differs" in capsys.readouterr().err


def test_check_accepts_distinct_installs_with_matching_provenance(tmp_path, capsys):
    assert _check_pair(tmp_path, current_scenarios={"shared", "current-only"})
    captured = capsys.readouterr()
    assert "OK -- baseline=" in captured.out
    assert captured.err == ""
