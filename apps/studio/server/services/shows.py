from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from ..config import SHOWS_ROOT


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _play_dirs(show_dir: Path) -> list[Path]:
    try:
        return [p for p in sorted(show_dir.iterdir()) if p.is_dir()]
    except OSError:
        return []


def list_shows() -> list[dict[str, Any]]:
    if not SHOWS_ROOT.exists():
        return []

    out: list[dict[str, Any]] = []
    for path in sorted(SHOWS_ROOT.iterdir()):
        if not path.is_dir():
            continue

        plays = _play_dirs(path)
        metas = [m for m in (_read_json(play / "_meta.json") for play in plays) if m]
        latest = max(
            metas,
            key=lambda m: str(m.get("started_at") or m.get("ended_at") or ""),
            default={},
        )
        latest_status = str(latest.get("status") or "unknown")
        try:
            last_update = max(
                [path.stat().st_mtime, *[play.stat().st_mtime for play in plays]],
            )
        except OSError:
            last_update = None

        out.append(
            {
                "topic": path.name,
                "path": str(path),
                "play_count": len(plays),
                "latest_status": latest_status,
                "last_update": last_update,
            }
        )
    return out


def get_show(topic: str) -> dict[str, Any] | None:
    show_dir = SHOWS_ROOT / topic
    if not show_dir.is_dir():
        return None

    plays: list[dict[str, Any]] = []
    for play_dir in _play_dirs(show_dir):
        meta = _read_json(play_dir / "_meta.json") or {}
        verdict = _read_json(play_dir / "_verdict.json")
        try:
            updated_at = play_dir.stat().st_mtime
        except OSError:
            updated_at = None
        plays.append(
            {
                "name": play_dir.name,
                "meta": meta,
                "verdict": verdict,
                "updated_at": updated_at,
            }
        )

    return {
        "topic": topic,
        "path": str(show_dir),
        "show_md": _read_text(show_dir / "_show.md"),
        "plays": plays,
    }


async def watch_show(topic: str) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events as files in the show directory mutate."""
    topic_dir = SHOWS_ROOT / topic
    seen_files: dict[str, tuple[float, int]] = {}

    while True:
        for path in sorted(topic_dir.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue

            key = str(path.relative_to(topic_dir))
            current = (stat.st_mtime, stat.st_size)
            previous = seen_files.get(key)
            if previous == current:
                continue

            seen_files[key] = current
            event_type = "new" if previous is None else "change"
            evt = {"type": event_type, "path": key, "size": stat.st_size}
            yield f"data: {json.dumps(evt)}\n\n"

        await asyncio.sleep(0.5)
