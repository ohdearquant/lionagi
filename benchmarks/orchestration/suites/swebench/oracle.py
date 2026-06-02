"""Deterministic oracle — score patches with the OFFICIAL SWE-bench harness.

There is NO LLM judge here. An instance is resolved iff, after applying the
agent's ``model_patch`` plus the held-out ``test_patch`` at ``base_commit``,
every FAIL_TO_PASS test passes and every PASS_TO_PASS test still passes. We do
not reimplement that — we shell out to ``swebench.harness.run_evaluation`` (the
same harness the leaderboard uses) so our numbers are directly comparable.

Requirements (run-time, not import-time):
  - ``uv pip install swebench``
  - Docker (the harness builds/pulls a per-instance image). On Apple-silicon
    pass ``namespace=""`` (we default to it on darwin) so it builds locally
    instead of pulling x86 images. A cloud path exists via ``modal=True``
    (``--modal true``) which avoids local Docker entirely.

Prediction record (one JSON object per line in predictions.jsonl):
    {"instance_id": ..., "model_name_or_path": ..., "model_patch": "<diff>"}
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

DATASET = "MariusHobbhahn/swe-bench-verified-mini"


def write_predictions(preds: list[dict], path: Path) -> Path:
    """Write predictions.jsonl. Each pred needs instance_id + model_patch;
    model_name_or_path defaults to the config key if absent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for p in preds:
            f.write(
                json.dumps(
                    {
                        "instance_id": p["instance_id"],
                        "model_name_or_path": p.get("model_name_or_path", "lionagi"),
                        "model_patch": p.get("model_patch", "") or "",
                    }
                )
                + "\n"
            )
    return path


def run_evaluation(
    predictions_path: Path,
    run_id: str,
    *,
    model_name: str = "lionagi",
    max_workers: int = 4,
    dataset: str = DATASET,
    namespace: str | None = None,
    modal: bool = False,
    timeout: int = 5400,
) -> dict:
    """Invoke the official harness. Returns the parsed report dict.

    ``namespace=""`` forces local image builds (required on Apple silicon);
    we auto-default to it on darwin. ``modal=True`` runs on Modal's cloud
    (no local Docker). Raises on harness failure with captured output."""
    if namespace is None:
        namespace = "" if platform.system() == "Darwin" else "swebench"
    cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset,
        "--predictions_path",
        str(predictions_path),
        "--run_id",
        run_id,
        "--max_workers",
        str(max_workers),
        "--namespace",
        namespace,
    ]
    if modal:
        cmd += ["--modal", "true"]
    # Report lands at <model_name>.<run_id>.json in cwd (harness convention).
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(predictions_path.parent),
    )
    report = predictions_path.parent / f"{model_name}.{run_id}.json"
    if not report.exists():
        # try the dotted-sanitized name the harness sometimes uses
        cands = list(predictions_path.parent.glob(f"*{run_id}.json"))
        if cands:
            report = cands[0]
    if not report.exists():
        raise RuntimeError(
            f"swebench harness produced no report for run_id={run_id}.\n"
            f"exit={proc.returncode}\nstdout(tail):\n{proc.stdout[-2000:]}\n"
            f"stderr(tail):\n{proc.stderr[-2000:]}"
        )
    return json.loads(report.read_text())


def resolved_map(report: dict) -> dict[str, bool]:
    """instance_id -> resolved? from a harness report. The report lists
    ``resolved_ids`` and the full ``submitted_ids`` (or ``completed_ids``)."""
    resolved = set(report.get("resolved_ids", []))
    submitted = (
        set(
            report.get("submitted_ids")
            or report.get("completed_ids")
            or report.get("unresolved_ids", [])
        )
        | resolved
    )
    return {iid: (iid in resolved) for iid in submitted}


def harness_available() -> bool:
    try:
        import swebench  # noqa: F401

        return True
    except ImportError:
        return False
