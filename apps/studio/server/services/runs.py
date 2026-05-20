from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from lionagi.cli._runs import RUNS_ROOT, list_runs as _list_runs


def list_runs() -> list[dict[str, Any]]:
    out = []
    for run_dir in _list_runs():
        manifest = run_dir.read_manifest()
        out.append(
            {
                **manifest,
                "run_id": run_dir.run_id,
                "state_root": str(run_dir.state_root),
                "artifact_root": str(run_dir.artifact_root),
            }
        )
    return out


def get_run(run_id: str) -> dict[str, Any] | None:
    if not RUNS_ROOT.exists():
        return None
    state_root = RUNS_ROOT / run_id
    if not state_root.is_dir():
        matches = [
            d
            for d in RUNS_ROOT.iterdir()
            if d.is_dir() and d.name.startswith(run_id)
        ]
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

    return {
        "run_id": run_id,
        "state_root": str(state_root),
        "artifact_root": str(artifact_root),
        "manifest": manifest,
        "branches": branches,
    }


def stream_run_events(run_id: str) -> AsyncGenerator[str, None] | None:
    """Return an async generator yielding SSE lines from buffer.jsonl, or None if not live."""
    if not RUNS_ROOT.exists():
        return None
    state_root = RUNS_ROOT / run_id
    if not state_root.is_dir():
        matches = [
            d
            for d in RUNS_ROOT.iterdir()
            if d.is_dir() and d.name.startswith(run_id)
        ]
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
