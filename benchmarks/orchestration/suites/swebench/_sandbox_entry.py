"""In-sandbox agent driver — runs INSIDE a Daytona sandbox, never on the host.

The host uploads this file, then execs ``python _sandbox_entry.py spec.json``.
It builds a lionagi coding agent against the local checkout, runs a ReAct loop,
and exchanges with the host over the reactive bus:

  - EMISSION OUT: a stdout sink serializes every Signal the branch emits
    (RunStart/RunEnd, ActionRequest/Response, StructuredOutput) to a
    ``@@SIG@@ {json}`` line. The host parses these live off the streamed stdout.
  - CONTROL IN: a background poller reads a control file the host writes into the
    sandbox (``branch.control(...)`` → the ReAct loop honors it at turn
    boundaries via the existing ``check_control`` seam).

Output: writes ``result.json`` (final text, git diff = model_patch, token usage)
to the path named in the spec. Only stdlib + lionagi are available here.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

SIG = "@@SIG@@ "


def _emit_line(obj: dict) -> None:
    sys.stdout.write(SIG + json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _summarize(event) -> dict:
    """Compact, transport-safe view of a Signal for the wire."""
    t = type(event).__name__
    data = getattr(event, "data", None)
    out: dict = {"t": t}
    cls = type(data).__name__ if data is not None else None
    if cls == "ActionRequest":
        out["fn"] = getattr(data, "function", None)
        args = getattr(data, "arguments", None)
        if isinstance(args, dict):
            # keep it short: action + first path-ish arg
            out["action"] = args.get("action")
            for k in ("path", "file_path", "command", "pattern"):
                if k in args:
                    out["arg"] = str(args[k])[:120]
                    break
    elif cls == "ActionResponse":
        o = getattr(data, "output", None)
        out["ok"] = isinstance(o, dict) and o.get("success", o.get("return_code") == 0)
    elif t in ("RunEnd", "RunFailed"):
        out["s"] = str(data)[:160]
    elif t == "StructuredOutput":
        out["s"] = str(data)[:160]
    return out


class StdoutSink:
    """Minimal observer: the branch calls ``emit(event)``; we serialize to stdout."""

    async def emit(self, event):
        try:
            _emit_line(_summarize(event))
        except Exception as e:  # never let telemetry kill the run
            _emit_line({"t": "SinkError", "s": str(e)[:120]})
        return []


def _collect_usage(branch) -> dict:
    """Sum provider-reported tokens across the branch's AssistantResponses.

    Mirrors harness/cost.py's normalization but inline (host has the full
    price table; here we only need raw token counts)."""
    inp = cached = out = calls = 0
    try:
        messages = list(branch.msgs.messages)
    except Exception:
        messages = []
    for m in messages:
        mr = getattr(m, "metadata", {}).get("model_response") if hasattr(m, "metadata") else None
        if not isinstance(mr, dict):
            continue
        u = mr.get("usage") if isinstance(mr.get("usage"), dict) else mr
        if not isinstance(u, dict):
            continue
        o = int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0)
        if "cached_input_tokens" in u:  # OpenAI/codex: input incl cached
            total_in = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
            c = int(u.get("cached_input_tokens", 0) or 0)
            i = max(0, total_in - c)
        else:  # Anthropic / generic
            i = int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0)
            c = int(u.get("cache_read_input_tokens", 0) or 0)
        if i or c or o:
            inp += i
            cached += c
            out += o
            calls += 1
    return {"input_tokens": inp, "cached_tokens": cached, "output_tokens": out, "n_calls": calls}


async def _control_poller(branch, control_path: Path, stop: asyncio.Event) -> None:
    """Translate a host-written control file into a branch control directive."""
    from lionagi.session.control import LoopDirective

    seen = ""
    while not stop.is_set():
        try:
            if control_path.exists():
                txt = control_path.read_text().strip()
                if txt and txt != seen:
                    seen = txt
                    dirv = {
                        "cancel": LoopDirective.CANCEL,
                        "break": LoopDirective.BREAK,
                    }.get(txt.lower())
                    if dirv is not None:
                        branch.control(dirv, reason="host control file")
                        _emit_line({"t": "ControlApplied", "s": txt})
        except Exception:  # noqa: S110 — best-effort control poll, never fatal
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    # Provider keys arrive via the spec file (not argv/env) — session commands do
    # NOT inherit the sandbox's creation-time env_vars, and lionagi falls back to
    # a "dummy" key when the var is missing. Set them before importing lionagi.
    import os

    for k, v in (spec.get("env") or {}).items():
        os.environ[k] = v
    _emit_line(
        {"t": "KeyCheck", "vars": {k: len(os.environ.get(k, "")) for k in (spec.get("env") or {})}}
    )

    repo = spec["repo_path"]
    model = spec["model"]
    instruction = spec["instruction"]
    max_ext = int(spec.get("max_extensions", 30))
    effort = spec.get("effort")
    result_path = Path(spec["result_path"])
    control_path = Path(spec.get("control_path", f"{repo}/../control"))

    from lionagi.agent import AgentConfig
    from lionagi.agent.factory import create_agent

    sys_prompt = (
        f"You are a software engineer fixing a bug in the repository at {repo}.\n"
        f"All file paths are under {repo}. When running shell commands with the "
        f"bash tool, set cwd to {repo} (or a subdirectory).\n"
        "Read the relevant code, make a MINIMAL targeted fix, and verify it. "
        "Do not rewrite unrelated code. Do not add new features."
    )
    config = AgentConfig.coding(
        name="swebench-coder",
        model=model,
        effort=effort,
        cwd=repo,
        yolo=True,
        max_extensions=max_ext,
    )
    config.system_prompt = sys_prompt + "\n\n" + config.system_prompt

    t0 = time.monotonic()
    branch = await create_agent(config)
    branch._observer = StdoutSink()

    stop = asyncio.Event()
    poller = asyncio.create_task(_control_poller(branch, control_path, stop))

    status = "ok"
    final = ""
    try:
        result = await branch.ReAct(
            {"instruction": instruction},
            tools=True,
            max_extensions=max_ext,
        )
        final = str(result)
    except Exception as e:  # noqa: BLE001 — a failed agent run is data
        status = f"error: {type(e).__name__}: {e}"
        _emit_line({"t": "EntryError", "s": status[:200]})
    finally:
        stop.set()
        await asyncio.gather(poller, return_exceptions=True)

    # model_patch = git diff of the working tree vs the base_commit. Exclude
    # build noise (__pycache__/*.pyc) so the patch is source-only.
    subprocess.run(["git", "add", "-A"], cwd=repo, check=False)  # noqa: S603,S607
    diff = subprocess.run(  # noqa: S603
        ["git", "diff", "--cached", "--", ".", ":(exclude)**/__pycache__/**"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout

    out = {
        "status": status,
        "final": final,
        "diff": diff,
        "usage": _collect_usage(branch),
        "wall_seconds": time.monotonic() - t0,
        "model": model,
    }
    result_path.write_text(json.dumps(out))
    _emit_line({"t": "Done", "s": status, "diff_bytes": len(diff)})
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
