from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[2] / "benchmarks" / "run_paired_ab.py"


def _run_cli(
    tmp_path: Path, *, provenance_change: str | None = None
) -> subprocess.CompletedProcess[str]:
    module = tmp_path / "fake_bench.py"
    module.write_text(
        f"""
import argparse
import json
import sys
from datetime import datetime, timezone

parser = argparse.ArgumentParser()
parser.add_argument("--repeat", type=int, required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
varies = args.output.endswith("-1.json")
meta = {{
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "python_executable": sys.executable,
    "dependency_version": "stable",
}}
if varies and {provenance_change == "different"!r}:
    meta["dependency_version"] = "changed"
if varies and {provenance_change == "missing"!r}:
    del meta["dependency_version"]
result = {{
    "meta": meta,
    "results": {{
        "scenario": {{
            "runs": args.repeat,
            "min": 1.0,
            "mean": 1.0,
            "median": 1.0,
            "max": 1.0,
        }}
    }},
}}
with open(args.output, "w", encoding="utf-8") as output:
    json.dump(result, output)
""",
        encoding="utf-8",
    )
    baseline_output = tmp_path / "baseline.json"
    current_output = tmp_path / "current.json"
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--module",
            "fake_bench",
            "--baseline-python",
            sys.executable,
            "--current-python",
            sys.executable,
            "--total-repeat",
            "2",
            "--chunks",
            "2",
            "--baseline-output",
            str(baseline_output),
            "--current-output",
            str(current_output),
            "--work-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_merges_chunks_with_distinct_run_timestamps(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path)

    assert completed.returncode == 0, completed.stderr
    for arm in ("baseline", "current"):
        merged_path = tmp_path / f"{arm}.json"
        assert merged_path.is_file()
        merged = json.loads(merged_path.read_text(encoding="utf-8"))
        assert merged["meta"]["chunk_count"] == 2

        chunks = [
            json.loads((tmp_path / "_chunks" / f"{arm}-chunk-{index}.json").read_text())
            for index in range(2)
        ]
        assert chunks[0]["meta"]["timestamp"] != chunks[1]["meta"]["timestamp"]


@pytest.mark.parametrize(
    ("provenance_change", "error_fragment"),
    [
        ("different", "chunks disagree on meta.dependency_version"),
        ("missing", "missing/non-string on some chunk"),
    ],
)
def test_cli_rejects_non_varying_provenance_disagreement(
    tmp_path: Path, provenance_change: str, error_fragment: str
) -> None:
    completed = _run_cli(tmp_path, provenance_change=provenance_change)

    assert completed.returncode != 0
    assert error_fragment in completed.stderr
