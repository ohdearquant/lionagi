from __future__ import annotations

import asyncio
import json as _json
import socket
import threading
import uuid
from pathlib import Path


SHOW_DIR = Path("/Users/lion/khive-work/shows/lion-studio-init")
FIXTURE_TOPIC = "lion-studio-init"


def test_shows_list_returns_array():
    from fastapi.testclient import TestClient

    from apps.studio.server.app import app

    client = TestClient(app)
    r = client.get("/api/shows")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    topics = {item["topic"] for item in data}
    assert FIXTURE_TOPIC in topics
    lion_show = next(item for item in data if item["topic"] == FIXTURE_TOPIC)
    assert "play_count" in lion_show
    assert isinstance(lion_show["play_count"], int)


def test_show_detail_has_meta():
    from fastapi.testclient import TestClient

    from apps.studio.server.app import app

    client = TestClient(app)
    r = client.get(f"/api/shows/{FIXTURE_TOPIC}")
    assert r.status_code == 200
    data = r.json()
    assert data["topic"] == FIXTURE_TOPIC
    assert isinstance(data["show_md"], str)
    assert "# Show: lion-studio-init" in data["show_md"]
    plays = data["plays"]
    assert isinstance(plays, list)
    assert len(plays) > 0
    play_names = {p["name"] for p in plays}
    assert "lift-backend" in play_names
    lb = next(p for p in plays if p["name"] == "lift-backend")
    assert lb["meta"]["branch"] == "show/lion-studio-init/lift-backend"
    assert lb["verdict"]["gate_passed"] is True


async def test_stream_endpoint_emits_event():
    import httpx
    import uvicorn

    from apps.studio.server.app import app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    for _ in range(30):
        if server.started:
            break
        await asyncio.sleep(0.1)

    unique_name = f".stream-test-{uuid.uuid4().hex}.txt"
    test_file = SHOW_DIR / unique_name

    async def _watch_for_event() -> dict | None:
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET",
                f"{base}/api/shows/{FIXTURE_TOPIC}/stream",
                timeout=6.0,
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        evt = _json.loads(line[len("data: "):])
                    except _json.JSONDecodeError:
                        continue
                    if evt.get("path") == unique_name:
                        return evt
        return None  # pragma: no cover

    async def _write_later() -> None:
        await asyncio.sleep(0.4)
        test_file.write_text("stream test content")

    try:
        write_task = asyncio.create_task(_write_later())
        evt = await asyncio.wait_for(_watch_for_event(), timeout=5.0)
        await write_task
        assert evt is not None
        assert evt["type"] in ("new", "change")
        assert isinstance(evt["size"], int)
        assert evt["size"] > 0
    finally:
        server.should_exit = True
        server_thread.join(timeout=3)
        if test_file.exists():
            test_file.unlink()
