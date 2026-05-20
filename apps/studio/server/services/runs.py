from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from lionagi.cli._runs import RUNS_ROOT
from lionagi.cli._runs import list_runs as _list_runs

from ._path_safety import safe_path_join

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _adapt_summary(
    manifest: dict[str, Any], run_id: str, state_root: Path, artifact_root: Path
) -> dict[str, Any]:
    """Return a RunSummary-shaped dict from a manifest and run paths."""
    branches_dir = state_root / "branches"
    step_count = 0
    if branches_dir.exists():
        try:
            step_count = len(list(branches_dir.glob("*.json")))
        except OSError:
            step_count = 0
    if not step_count and isinstance(manifest.get("steps"), list):
        step_count = len(manifest["steps"])

    return {
        "run_id": run_id,
        "state_root": str(state_root),
        "artifact_root": str(artifact_root),
        "worker_name": str(manifest.get("worker_name") or manifest.get("worker") or ""),
        "task": str(manifest.get("task") or ""),
        "status": str(manifest.get("status") or "pending"),
        "step_count": step_count,
        "started_at": manifest.get("started_at") or None,
        "finished_at": manifest.get("finished_at") or None,
    }


def _adapt_detail(
    run_id: str,
    state_root: Path,
    artifact_root: Path,
    manifest: dict[str, Any],
    branches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a RunDetail-shaped dict."""
    summary = _adapt_summary(manifest, run_id, state_root, artifact_root)
    graph_raw = manifest.get("graph") or {}
    graph = {
        "nodes": graph_raw.get("nodes") or [],
        "edges": graph_raw.get("edges") or [],
    }
    return {
        **summary,
        "error": manifest.get("error") or None,
        "cwd": manifest.get("cwd") or None,
        "steps": manifest.get("steps") or None,
        "graph": graph,
        # Keep raw data for downstream consumers
        "manifest": manifest,
        "branches": branches,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_runs(
    worker: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    out = []
    for run_dir in _list_runs():
        manifest = run_dir.read_manifest()
        summary = _adapt_summary(
            manifest, run_dir.run_id, run_dir.state_root, run_dir.artifact_root
        )
        # Apply optional query filters
        if worker and summary["worker_name"] != worker:
            continue
        if status and summary["status"] != status:
            continue
        out.append(summary)
    return out


def get_run(run_id: str) -> dict[str, Any] | None:
    if not RUNS_ROOT.exists():
        return None

    # Validate + resolve the run_id path component
    safe_path_join(RUNS_ROOT, run_id)  # raises 404 if unsafe

    state_root = RUNS_ROOT / run_id
    if not state_root.is_dir():
        matches = [d for d in RUNS_ROOT.iterdir() if d.is_dir() and d.name.startswith(run_id)]
        if not matches:
            return None
        state_root = sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        run_id = state_root.name

    manifest_path = state_root / "run.json"
    artifact_root = state_root / "artifacts"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            art = manifest.get("artifact_root")
            if art:
                artifact_root = Path(art)
        except (OSError, json.JSONDecodeError):
            pass

    branches: list[dict[str, Any]] = []
    branches_dir = state_root / "branches"
    if branches_dir.exists():
        for bf in sorted(
            branches_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                branches.append(json.loads(bf.read_text()))
            except (OSError, json.JSONDecodeError):
                pass

    return _adapt_detail(run_id, state_root, artifact_root, manifest, branches)


def stream_run_events(run_id: str) -> AsyncGenerator[str, None] | None:
    """Return an async generator yielding SSE lines from buffer.jsonl, or None if not live."""
    if not RUNS_ROOT.exists():
        return None

    # Validate path component before filesystem access
    safe_path_join(RUNS_ROOT, run_id)  # raises 404 if unsafe

    state_root = RUNS_ROOT / run_id
    if not state_root.is_dir():
        matches = [d for d in RUNS_ROOT.iterdir() if d.is_dir() and d.name.startswith(run_id)]
        if not matches:
            return None
        state_root = sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]

    stream_dir = state_root / "stream"
    if not stream_dir.exists():
        return None

    buffers = list(stream_dir.glob("*.buffer.jsonl"))
    if not buffers:
        return None

    buffer_path = sorted(buffers, key=lambda p: p.stat().st_mtime, reverse=True)[0]

    async def _gen() -> AsyncGenerator[str, None]:
        try:
            with buffer_path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield f"data: {line}\n\n"
                while True:
                    line = fh.readline()
                    if line:
                        line = line.strip()
                        if line:
                            yield f"data: {line}\n\n"
                    else:
                        if not buffer_path.exists():
                            yield f"data: {json.dumps({'type': 'done'})}\n\n"
                            break
                        await asyncio.sleep(0.1)
        except OSError:
            yield f"data: {json.dumps({'type': 'error', 'msg': 'stream read error'})}\n\n"

    return _gen()
