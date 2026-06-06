# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Host-side glue: run a lionagi agent inside a Daytona sandbox with the run's
activity streamed live into the local ``state.db`` (ADR-0083).

This ties together the three Phase-2 pieces:

- ``lionagi/tools/daytona.py`` — the sandbox lifecycle (create / upload / exec_stream).
- ``lionagi/tools/sandbox_entry.py`` — the in-sandbox driver that emits ``@@LIONDB@@``
  persistence events on stdout.
- ``lionagi/tools/sandbox_bridge.py`` — the host ``SandboxBridge`` that replays those
  events into the local ``state.db`` exactly as a local run would.

The sandbox's ``exec_stream`` delivers stdout through a *sync* ``on_stdout`` callback;
we hand each line to the *async* bridge over an ``asyncio.Queue`` (via
``call_soon_threadsafe`` so it is correct whether the SDK invokes the callback on the
loop thread or a worker thread). The ``sandbox_factory`` seam makes the whole path
testable with a fake sandbox that replays canned stdout — no Daytona, no credits.

The default snapshot (built by :func:`pi_lionagi_image`) carries node + the ``pi``
coding agent, so ``model="openrouter/deepseek/deepseek-v4-flash"`` with
``provider="pi"`` drives the agent against OpenRouter inside the container.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from lionagi.tools.sandbox_bridge import SandboxBridge

__all__ = ("run_in_sandbox", "pi_lionagi_image", "DEFAULT_SNAPSHOT", "ENTRY_FILE")

_log = logging.getLogger("lionagi.cli")

DEFAULT_SNAPSHOT = "lionagi-pi-py312-v1"
ENTRY_FILE = Path(__file__).resolve().parent / "sandbox_entry.py"

# A sandbox factory returns an async-context-managed sandbox (DaytonaSandbox or a
# fake). Deferred so importing this module never requires the daytona extra.
SandboxFactory = Callable[[], Awaitable[Any]]


def _default_factory(snapshot: str, env: dict[str, str] | None) -> SandboxFactory:
    async def _make():
        from lionagi.tools.daytona import DaytonaSandbox

        return await DaytonaSandbox.create(snapshot=snapshot, env=env)

    return _make


async def run_in_sandbox(
    *,
    instruction: str,
    model: str,
    provider: str | None = None,
    effort: str | None = None,
    repo_url: str | None = None,
    base_commit: str | None = None,
    project: str | None = None,
    invocation_kind: str = "agent",
    env: dict[str, str] | None = None,
    snapshot: str = DEFAULT_SNAPSHOT,
    system_prompt: str | None = None,
    max_extensions: int = 30,
    sandbox_factory: SandboxFactory | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Run a coding agent inside a sandbox; stream its activity to the local state.db.

    Returns ``{"session_id", "status", "result"}``. The session is visible in
    ``li monitor`` from the moment this is called (the bridge creates the row
    before the sandbox emits anything) and accrues messages live as the agent works.

    ``sandbox_factory`` overrides sandbox creation (the test seam). ``env`` carries
    provider API keys into the container *and* into the in-sandbox spec file
    (session commands do not inherit creation-time env_vars — see ``daytona.py``).
    """
    bridge = SandboxBridge(
        invocation_kind=invocation_kind,
        model=model,
        provider=provider,
        effort=effort,
        project=project,
        node_metadata={"sandbox": {"backend": "daytona", "harness": provider or "pi"}},
        db_path=db_path,
    )
    await bridge.start()

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    done_sentinel = object()
    buf = [""]

    def on_stdout(chunk: str) -> None:
        # Sync callback (possibly off-thread): split into lines and hand each to
        # the async drain task. call_soon_threadsafe is safe from any thread.
        buf[0] += chunk
        while "\n" in buf[0]:
            line, buf[0] = buf[0].split("\n", 1)
            loop.call_soon_threadsafe(queue.put_nowait, line)

    async def drain() -> None:
        while True:
            line = await queue.get()
            if line is done_sentinel:
                return
            try:
                await bridge.feed_line(line)
            except Exception as exc:  # noqa: BLE001 — a bad line must not kill the stream
                _log.warning("sandbox run: feed_line failed: %s", exc)

    drain_task = asyncio.create_task(drain())

    status = "completed"
    exc: BaseException | None = None
    result: dict[str, Any] = {}
    try:
        factory = sandbox_factory or _default_factory(snapshot, env)
        async with await factory() as sb:
            home = await sb.home_dir()
            repo = f"{home}/repo"
            if repo_url:
                await sb.clone(repo_url, repo, commit=base_commit)
            await sb.upload_file(ENTRY_FILE, f"{home}/sandbox_entry.py")
            spec = {
                "repo_path": repo if repo_url else home,
                "model": model,
                "provider": provider,
                "instruction": instruction,
                "effort": effort,
                "max_extensions": max_extensions,
                "system_prompt": system_prompt,
                "result_path": f"{home}/result.json",
                "env": env or {},
            }
            await sb.write_text(json.dumps(spec), f"{home}/spec.json")
            code = await sb.exec_stream(
                f"python {home}/sandbox_entry.py {home}/spec.json",
                on_stdout=on_stdout,
            )
            try:
                result = json.loads(await sb.read_text(f"{home}/result.json"))
            except Exception as e:  # noqa: BLE001 — no result.json == a failed run
                result = {"status": f"no result.json (exit {code}): {e}"}
            if str(result.get("status", "ok")) != "ok":
                status = "failed"
    except Exception as e:  # noqa: BLE001 — surface as a failed session, never crash the caller
        exc = e
        status = "failed"
        result = {"status": f"runner error: {type(e).__name__}: {e}"}
        _log.warning("sandbox run failed: %s", e, exc_info=True)
    finally:
        # Flush any trailing partial line, then signal the drain to stop and join it
        # so every emitted event is persisted before we write the terminal status.
        if buf[0].strip():
            loop.call_soon_threadsafe(queue.put_nowait, buf[0])
        loop.call_soon_threadsafe(queue.put_nowait, done_sentinel)
        await drain_task
        await bridge.finish(status=status, exception=exc)

    return {"session_id": bridge.session_id, "status": status, "result": result}


def pi_lionagi_image(
    *,
    python: str = "3.12",
    pip_spec: str = "lionagi",
    pi_pkg: str = "@mariozechner/pi-coding-agent",
    node_setup: str = "https://deb.nodesource.com/setup_20.x",
):
    """A ``daytona.Image`` = lionagi deps + node + the ``pi`` coding agent.

    Built once into the :data:`DEFAULT_SNAPSHOT` (via
    ``lionagi.tools.daytona.ensure_snapshot``), then reused per run. ``pi`` is the
    harness; pointing it at ``--provider openrouter`` needs only ``OPENROUTER_API_KEY``
    in the run env (``providers/pi/cli/models.py``).
    """
    from daytona import Image

    return (
        Image.debian_slim(python)
        .run_commands(
            "apt-get update && apt-get install -y git curl ca-certificates",
            f"curl -fsSL {node_setup} | bash -",
            "apt-get install -y nodejs",
            f"npm i -g {pi_pkg}",
        )
        .pip_install(pip_spec, "pyyaml")
    )
