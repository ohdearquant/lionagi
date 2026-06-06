# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""In-sandbox driver — runs INSIDE a Daytona sandbox, streaming persistence to the host.

The host uploads this file and execs ``python -m … sandbox_entry <spec.json>`` (or
uploads it as a script). It builds a lionagi coding agent against a local checkout
and runs it, but its persistence is *emit-only*: instead of writing to an
in-container ``state.db`` nobody reads, every reactive-bus message is serialized to
a ``@@LIONDB@@`` line on stdout that the host's
:class:`~lionagi.tools.sandbox_bridge.SandboxBridge` replays into the host
``state.db`` — the exact ``_on_message`` write sequence (ADR-0083).

The emit side rides the SAME transport persistence rides: ``route_message_persistence``
wires the branch's ``_persist_via_bus`` emit hook + a per-branch handler on the
session hook bus (ADR-0023b). We swap the StateDB write for a stdout emit; the
wiring is identical, so a sandboxed run produces host rows indistinguishable from a
local run.

Only stdlib + lionagi are available here. The wire schema lives in
``sandbox_protocol`` (pure stdlib) so it imports cleanly in the minimal sandbox.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from lionagi.hooks import route_message_persistence
from lionagi.tools.sandbox_protocol import (
    branch_event,
    encode_event,
    message_event,
    phase_event,
)

__all__ = (
    "stdout_emitter",
    "attach_persistence_emitter",
    "emit_phase",
    "main",
)

# An emitter takes a wire-event dict and delivers it (stdout in the sandbox, a
# list in tests). Keeping the encode inside the emitter lets a test capture
# either the raw event or the encoded line.
Emitter = Callable[[dict[str, Any]], None]


def stdout_emitter(ev: dict[str, Any]) -> None:
    """Default emitter: write one ``@@LIONDB@@`` line to stdout and flush."""
    sys.stdout.write(encode_event(ev))
    sys.stdout.flush()


def attach_persistence_emitter(
    session: Any,
    branch: Any,
    emit: Emitter = stdout_emitter,
    *,
    model: str | None = None,
    provider: str | None = None,
):
    """Stream ``branch``'s messages to ``emit`` as ``@@LIONDB@@`` wire events.

    Mirror image of the host ``SandboxBridge`` and the CLI ``_register_branch_hook``:
    registers through ``route_message_persistence`` (so the branch emits
    ``MESSAGE_ADD`` and a per-branch handler fires), but the handler EMITS instead
    of writing to StateDB. On the first message it emits a ``branch`` event
    (carrying the construction-time system message, which never flows through the
    hook — exactly as ``_ensure_branch_row`` inserts it separately); every message
    then emits a ``message`` event. Returns the registered handler for teardown.
    """
    state = {"branch_sent": False}

    async def on_message(msg: Any) -> None:
        if not state["branch_sent"]:
            state["branch_sent"] = True
            system = branch.system
            emit(
                branch_event(
                    branch.to_dict(mode="db"),
                    system_msg=system.to_dict(mode="db") if system is not None else None,
                    model=model,
                    provider=provider,
                )
            )
        emit(message_event(str(branch.id), msg.to_dict(mode="db")))

    return route_message_persistence(session, branch, on_message)


def emit_phase(phase: str, emit: Emitter = stdout_emitter) -> None:
    """Emit a ``phase`` event — sets ``sessions.current_phase`` on the host."""
    emit(phase_event(phase))


async def main() -> int:
    """Run a single coding agent in-sandbox, emitting persistence to stdout.

    Mirrors the SWE-bench driver's agent construction
    (``benchmarks/orchestration/suites/swebench/_sandbox_entry.py``) but emits the
    full ``@@LIONDB@@`` persistence stream instead of lossy ``@@SIG@@`` summaries,
    and uses no in-container DB. Spec keys: ``repo_path``, ``model`` (e.g.
    ``openrouter/deepseek/deepseek-v4-flash`` for pi), ``instruction``, ``provider``
    (optional), ``effort``, ``max_extensions``, ``env`` (provider keys), ``result_path``.
    """
    import json
    import os
    import time
    from pathlib import Path

    spec = json.loads(Path(sys.argv[1]).read_text())
    # Provider keys travel in the spec file, not env/argv — session commands do not
    # inherit the sandbox's creation-time env_vars (see daytona.py).
    for k, v in (spec.get("env") or {}).items():
        os.environ[k] = v

    repo = spec["repo_path"]
    model = spec["model"]
    provider = spec.get("provider")
    instruction = spec["instruction"]
    effort = spec.get("effort")
    max_ext = int(spec.get("max_extensions", 30))
    result_path = Path(spec.get("result_path", f"{repo}/../result.json"))

    from lionagi import Session
    from lionagi.agent import AgentConfig
    from lionagi.agent.factory import create_agent

    emit_phase("setup")
    config = AgentConfig.coding(
        name=spec.get("agent_name", "sandbox-coder"),
        model=model,
        effort=effort,
        cwd=repo,
        yolo=True,
        max_extensions=max_ext,
    )
    if spec.get("system_prompt"):
        config.system_prompt = spec["system_prompt"]
    branch = await create_agent(config)
    # Wrap in a Session so the hook bus exists, then attach the emit-only persistence.
    session = Session(default_branch=branch)
    attach_persistence_emitter(session, branch, model=model, provider=provider)

    status = "ok"
    final = ""
    t0 = time.monotonic()
    try:
        emit_phase("executing")
        result = await branch.ReAct(
            {"instruction": instruction}, tools=True, max_extensions=max_ext
        )
        final = str(result)
    except Exception as e:  # noqa: BLE001 — a failed agent run is data, not a crash
        status = f"error: {type(e).__name__}: {e}"
    finally:
        emit_phase("done")

    out = {"status": status, "final": final, "wall_seconds": time.monotonic() - t0, "model": model}
    result_path.write_text(json.dumps(out, default=str))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
