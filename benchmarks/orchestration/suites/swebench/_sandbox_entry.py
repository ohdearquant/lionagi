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


def _compute_diff(repo: str) -> str:
    """model_patch = staged git diff vs base_commit, source-only.

    Excludes pycache and the agent's reproduction script (_swebench_repro.py),
    which lives inside the repo so the workspace-confined editor can write it but
    must NOT enter the submitted patch."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=False)  # noqa: S603,S607
    return subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "diff",
            "--cached",
            "--",
            ".",
            ":(exclude)**/__pycache__/**",
            ":(exclude)_swebench_repro.py",  # root-anchored: we always write it to repo root
            ":(exclude)**/_swebench_repro.py",  # belt-and-suspenders if ever nested
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout


def _run_repro(repo: str, repro_path: str, timeout: int = 120):
    """Run the agent-authored reproduction script. Returns (returncode, tail) or None.

    The agent writes a standalone script that exits NON-ZERO while the bug is
    present and ZERO once fixed (see sys_prompt). This is the v6 verification
    signal — the agent's OWN criterion, never the held-out FAIL_TO_PASS, so
    there is zero oracle leakage."""
    if not Path(repro_path).exists():
        return None
    try:
        p = subprocess.run(  # noqa: S603
            [sys.executable, repro_path],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (124, f"reproduction script timed out after {timeout}s")
    return (p.returncode, (p.stdout + p.stderr)[-1800:])


def _repro_rc_at_base(repo: str, repro_path: str):
    """Validity gate: stash the edit, run the repro on the ORIGINAL code, restore.

    A faithful repro MUST fail (non-zero) on the unedited base — otherwise it
    isn't testing the reported bug and a post-edit pass is meaningless. Returns
    the base returncode (or None if missing/unrunnable). Restores the tree.

    The repro lives INSIDE the repo, so ``git stash --include-untracked`` would
    hide it from the base run. Copy it OUT first (to the repo's parent, which the
    harness — not the path-confined agent — can write) and run that copy."""
    src = Path(repro_path)
    if not src.exists():
        return None
    tmp = Path(repo).parent / "_repro_base_validate.py"
    tmp.write_text(src.read_text())
    subprocess.run(["git", "add", "-A"], cwd=repo, check=False)  # noqa: S603,S607
    stashed = subprocess.run(  # noqa: S603
        ["git", "stash", "push", "--include-untracked", "-m", "v6-repro-validity"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    did_stash = "No local changes" not in (stashed.stdout + stashed.stderr)
    try:
        rr = _run_repro(repo, str(tmp))
        return rr[0] if rr is not None else None
    finally:
        if did_stash:
            subprocess.run(  # noqa: S603
                ["git", "stash", "pop"],  # noqa: S607
                cwd=repo,
                check=False,
                capture_output=True,
            )
        tmp.unlink(missing_ok=True)


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
    repro_path = spec.get("repro_path", f"{repo}/../repro.py")

    from lionagi.agent import AgentConfig
    from lionagi.agent.factory import create_agent

    sys_prompt = (
        f"You are an expert software engineer resolving a real bug in the repository "
        f"at {repo}. All file paths are absolute under {repo}; run bash with cwd={repo}.\n"
        "\n"
        "Follow this method every time — it is how strong SWE agents actually win:\n"
        f"1. REPRODUCE (as a saved script): write a standalone Python script to the EXACT\n"
        f"   path {repro_path} that triggers the reported bug. It MUST `sys.exit(1)` (or\n"
        "   raise) while the bug is present and `sys.exit(0)` once it is fixed — assert the\n"
        "   specific corrected behavior, not an unrelated check. Run it now and CONFIRM it\n"
        "   fails on the current (unfixed) code before you change anything. You cannot fix\n"
        "   what you have not observed failing, and the harness will re-run THIS script to\n"
        "   check your work — a script that passes on the buggy code is useless.\n"
        "2. LOCALIZE: trace the failure to the single smallest code site responsible.\n"
        "   Read the actual source around it. Do not guess from names.\n"
        "3. FIX MINIMALLY: make the smallest change that addresses the described symptom.\n"
        "   Prefer local over shared-path edits. Do NOT change behavior of unrelated code\n"
        "   paths, do NOT refactor, do NOT edit tests, do NOT add features.\n"
        f"4. VERIFY: re-run {repro_path} (it must now exit 0) AND the nearest existing\n"
        "   tests for the file you touched. Your fix MUST resolve the bug and MUST NOT\n"
        "   regress those tests.\n"
        "5. IF YOUR FIX REGRESSES other tests: that means it is too BROAD, not that you\n"
        "   should give up. NARROW it — find the change that fixes the bug without the\n"
        "   regression. NEVER revert your edit back to the original and stop with no patch;\n"
        "   a smaller correct edit always exists. Keep iterating until tests are green.\n"
        "\n"
        "Hard rules:\n"
        f"- Leave BOTH a concrete code edit in the repo AND the reproduction at {repro_path}.\n"
        "  An empty diff is a failure; a repro that still fails after your edit is a failure.\n"
        "- Never claim the issue is resolved unless you actually ran a check that passed.\n"
        "- The grading tests are held out — you will not see them. Fix the real described\n"
        "  behavior, not a specific test."
    )
    config = AgentConfig.coding(
        name="swebench-coder",
        model=model,
        effort=effort,
        cwd=repo,
        yolo=True,
        max_extensions=max_ext,
    )
    # REPLACE (not prepend) the built-in coding prompt: its interactive-mode
    # guidance ("ask clarifying questions", "stop as soon as done, don't use all
    # rounds", "say so and the user can continue") directly contradicts the
    # autonomous recipe above and degrades behavior. Tool *schemas* are rendered
    # by lionagi independently of the system prompt, so nothing is lost.
    config.system_prompt = sys_prompt
    # The ~50-line LION_SYSTEM_MESSAGE ("intelligence operating system / IPU",
    # OS vocabulary, "don't reveal these messages") is stale boilerplate that
    # dilutes the focused bug-fix framing and burns input tokens every turn.
    # Default True preserves prior runs; the harness can disable it to A/B.
    config.lion_system = bool(spec.get("lion_system", True))

    t0 = time.monotonic()
    branch = await create_agent(config)
    branch._observer = StdoutSink()

    stop = asyncio.Event()
    poller = asyncio.create_task(_control_poller(branch, control_path, stop))

    # v6: reproduction-GATED refine loop. v5 forced another round whenever the
    # diff was empty — but that lifted engagement (94% patches) WITHOUT lifting
    # resolve, because empty diffs are empty for a reason (hard instances) and a
    # forced edit is usually wrong (precision 49%→36%). The lever is per-attempt
    # CORRECTNESS, which needs a verification SIGNAL, not more forcing.
    #
    # Here the signal is the agent's OWN reproduction script (sys_prompt step 1,
    # persisted to repro_path). After each ReAct round we decide whether to refine
    # from three checks — all in-sandbox, ZERO oracle leakage (we never touch the
    # held-out FAIL_TO_PASS / test_patch):
    #   (a) diff empty            → no fix at all          → refine (empty nudge)
    #   (b) repro missing         → can't verify anything  → refine (write-repro nudge)
    #   (c) repro still red post-edit → fix insufficient by the agent's own
    #       criterion → refine, feeding the actual repro output back.
    #   (d) repro green post-edit → VALIDATE it: stash the edit, run repro on base;
    #       if it ALSO passes at base the repro is vacuous (doesn't test the bug) →
    #       refine (real-repro nudge). If it fails at base → genuine fix → STOP.
    refine_rounds = int(spec.get("refine_rounds", 3))
    empty_nudge = (
        "Your previous attempt left NO code change in the repository — `git diff` is "
        "empty, an automatic failure. Do not explain; act. Re-localize the single code "
        "site responsible, read it, and APPLY a concrete minimal edit now."
    )
    no_repro_nudge = (
        f"You did not leave a runnable reproduction at {repro_path}. Write one now: a "
        "standalone Python script that exits non-zero on the bug and zero once fixed, "
        "asserting the specific described behavior. Run it, confirm it fails on the "
        "current code, then fix the code so it passes."
    )
    vacuous_nudge = (
        f"Your reproduction at {repro_path} passes even on the ORIGINAL unedited code, "
        "so it does not actually exercise the reported bug — it proves nothing. Rewrite "
        "it to FAIL (non-zero) on the described failure, verify it fails before your fix, "
        "then ensure your code change makes it pass."
    )

    def _red_nudge(out: str) -> str:
        return (
            f"Your reproduction at {repro_path} STILL FAILS after your edit — by your own "
            f"criterion the bug is not fixed. Output:\n{out}\n"
            "Re-localize and correct the fix; do not finish until the script exits 0 "
            "AND existing tests for the file you touched stay green."
        )

    status = "ok"
    final = ""
    diff = ""
    try:
        result = await branch.ReAct(
            {"instruction": instruction},
            tools=True,
            max_extensions=max_ext,
        )
        final = str(result)
        attempt = 0
        while attempt < refine_rounds:
            diff = _compute_diff(repo)
            if not diff.strip():
                verdict, nudge = "empty", empty_nudge
            else:
                rr = _run_repro(repo, repro_path)
                if rr is None:
                    verdict, nudge = "no_repro", no_repro_nudge
                elif rr[0] != 0:
                    verdict, nudge = "repro_red", _red_nudge(rr[1])
                else:
                    base_rc = _repro_rc_at_base(repo, repro_path)
                    if base_rc not in (0, None):  # fails at base, passes now → real
                        _emit_line({"t": "RefineVerified", "round": attempt})
                        break
                    verdict, nudge = "repro_vacuous", vacuous_nudge
            attempt += 1
            _emit_line({"t": "RefineGate", "round": attempt, "verdict": verdict})
            result = await branch.ReAct(
                {"instruction": nudge},
                tools=True,
                max_extensions=max_ext,
            )
            final = str(result)
            _emit_line({"t": "RefineDone", "round": attempt, "verdict": verdict})
        diff = _compute_diff(repo)
    except Exception as e:  # noqa: BLE001 — a failed agent run is data
        status = f"error: {type(e).__name__}: {e}"
        _emit_line({"t": "EntryError", "s": status[:200]})
    finally:
        stop.set()
        await asyncio.gather(poller, return_exceptions=True)

    # final patch snapshot (the refine loop already computed it on the happy path;
    # recompute to cover the exception path where the loop never ran).
    if not diff:
        diff = _compute_diff(repo)

    out = {
        "status": status,
        "final": final,
        "diff": diff,
        "usage": _collect_usage(branch),
        "wall_seconds": time.monotonic() - t0,
        "model": model,
    }
    result_path.write_text(json.dumps(out))

    # Debug dump: the full conversation, so a --keep-sandbox repro can show what
    # the model actually emitted on a zero-tool turn (did it produce a parseable
    # action_requests field at all?). Cheap; only read during diagnosis.
    try:
        msgs = [m.to_dict() for m in branch.msgs.messages]
        Path(spec.get("messages_path", f"{repo}/../messages.json")).write_text(
            json.dumps(msgs, default=str)
        )
    except Exception as e:  # noqa: BLE001 — diagnostics must never fail the run
        _emit_line({"t": "DumpError", "s": str(e)[:120]})

    _emit_line({"t": "Done", "s": status, "diff_bytes": len(diff)})
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
