"""The codex CLI as the whole harness, inside the instance box: the other arm of the harness comparison.

Same model (through OpenRouter), same task text, same eval image, same grading as the LNDL arm;
what differs is who runs the loop. Here codex's own agent loop runs in the box with its shell tool
and no inner sandbox (the box is the sandbox), for at most `time_budget` seconds of wall clock; the
diff on disk at the end is the patch, whatever codex said. The record has the shape
`run_swe.solve` writes so the ledger and the summary read both arms alike: `rounds` counts the
commands codex ran, `tokens` and `cost_usd` come from its `turn.completed` usage priced at the
model's listed OpenRouter rates (codex reports tokens, not money); a turn started and never metered
leaves the record incomplete, and with no usage at all both are None, never zero. A `time_budget`
of None or 0 is no deadline: no kill in the box and no timeout on the exec call.

An exec call that fails in transport (a 504 from the box's API) says nothing about codex, which may
still be running in the box. The stream it left is read, the usage marked incomplete, and a probe
kills what it finds of codex, its rc kept as `stop_rc`. No probe proves the execution stopped (a
command codex started outlives it), so the record says `execution_quiesced: false` and carries no
patch, and nothing grades a tree that may still be written.

codex arrives as the static musl binary from its GitHub release, so the image needs nothing but
curl. The provider stanza rides in a private CODEX_HOME inside the box; the key is passed in the
exec environment only, never written to the box's disk, the stream, or any record.
"""

from __future__ import annotations

import json
import shlex
import time
from collections import Counter
from pathlib import Path
from typing import Any

from run_swe import PROMPT, UNAIDED
from sandbox import close_code_hosts

from hub.harness.openrouter import api_key

CODEX_VERSION = "0.153.4"
CODEX_URL = (
    "https://github.com/openai/codex/releases/download/rust-v{version}/codex-x86_64-unknown-linux-musl.tar.gz"
)
BIN = "/opt/codex/codex"
HOME = "/root/.codex-bench"
PROMPT_FILE = "/tmp/codex-prompt.txt"
# rc 0 once no codex process is left in the box, 1 when one survives three kills, 2 when the probe
# cannot see processes at all (no /proc, no grep: an empty answer there would read as stopped); the
# bracket keeps the pattern from matching the probe's own command line
STOP = (
    "[ -r /proc/$$/cmdline ] && command -v grep >/dev/null || exit 2; for i in 1 2 3 4; do "
    f"p=$(grep -l -a -E '[{BIN[0]}]{BIN[1:]}' /proc/[0-9]*/cmdline 2>/dev/null | cut -d/ -f3); "
    '[ -z "$p" ] && exit 0; [ $i = 4 ] || { kill -KILL $p 2>/dev/null; sleep 1; }; done; exit 1'
)
STREAM = "/tmp/codex-stream.jsonl"
STDERR = "/tmp/codex.err"
LAST = "/tmp/codex-last.txt"
CONFIG = """\
model = "{model}"
model_provider = "openrouter"
model_context_window = 200000
model_auto_compact_token_limit = 160000

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"
"""
GUIDANCE = (  # the LNDL arm's profile, minus what is LNDL-specific
    "\nWork in the repository at the current directory: locate the code the issue is about, reproduce "
    "the problem with a small script or an existing test when you can, make the minimal correct edit, "
    "then run the nearest existing tests. You need not add tests to the repository. Finish only when "
    "the fix is in place; never finish with the repository unchanged.\n"
)
# per token as listed by OpenRouter on 2026-09-19: prompt, completion, cached prompt; the model id's
# row is the default endpoint's rate.
PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek/deepseek-v4.1-flash": (0.15e-6, 0.6e-6, 0.003e-6),
}


async def install(box: Any) -> str:
    """The pinned codex binary at `BIN`; returns `codex --version`."""
    url = CODEX_URL.format(version=CODEX_VERSION)
    rc, out = await box.run(
        f"mkdir -p /opt/codex && curl -fsSL -o /tmp/codex.tgz {shlex.quote(url)} && "
        "tar -xzf /tmp/codex.tgz -C /opt/codex && "
        "bin=$(find /opt/codex -maxdepth 2 -type f -name 'codex*' ! -name '*.tgz' | head -1) && "
        f'[ -n "$bin" ] && {{ [ "$bin" = {BIN} ] || mv "$bin" {BIN}; }} && chmod +x {BIN} && {BIN} --version',
        timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"codex install failed ({rc}): {out[-300:]}")
    return out.strip().splitlines()[-1]


def parse_stream(text: str) -> dict:
    """What the `--json` stream says: steps, commands, usage summed over turns and the count of turns
    that carried it, failures."""
    kinds: Counter = Counter()
    usage: Counter = Counter()
    commands: list[str] = []
    failures: list[str] = []
    messages = metered = 0
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        kinds[f"{kind}/{item['type']}" if item.get("type") else kind] += 1
        if kind == "turn.completed":
            counts = {k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)}
            usage.update(counts)
            metered += bool(counts)  # an explicit zero is a reading; no numbers is none
        elif kind == "turn.failed":
            failures.append(str((event.get("error") or {}).get("message"))[:300])
        elif kind == "error":
            failures.append(str(event.get("message"))[:300])
        elif kind == "item.completed" and item.get("type") == "command_execution":
            commands.append(str(item.get("command"))[:200])
        elif kind == "item.completed" and item.get("type") == "agent_message":
            messages += 1
    return {
        "kinds": dict(kinds),
        "usage": dict(usage),
        "commands": commands,
        "failures": failures,
        "messages": messages,
        "metered": metered,
    }


def cost_of(model: str, usage: dict) -> float | None:
    if model not in PRICES:
        return None
    prompt, completion, cached = PRICES[model]
    read = usage.get("cached_input_tokens", 0)
    return (
        (usage.get("input_tokens", 0) - read) * prompt
        + read * cached
        + usage.get("output_tokens", 0) * completion
    )


async def solve(
    row: dict,
    model: str,
    out_dir: Path,
    *,
    box: Any,
    time_budget: float | None = 1200.0,
    unaided: bool = False,
) -> dict:
    iid = row["instance_id"]
    rec: dict = {"instance_id": iid, "model": model, "backend": "codex-cli", "harness": "codex-cli"}
    t0 = time.perf_counter()
    try:
        rec["codex_version"] = await install(box)  # the binary comes from github: before the hosts close
        if unaided:
            rec["unaided"] = await close_code_hosts(box)
        await box.write(f"{HOME}/config.toml", CONFIG.format(model=model))
        prompt = PROMPT.format(problem=row["problem_statement"]) + (UNAIDED if unaided else "") + GUIDANCE
        await box.write(PROMPT_FILE, prompt)
        deadline = f"timeout -s KILL {int(time_budget)} " if time_budget else ""
        cmd = (
            f"CODEX_HOME={HOME} {deadline}{BIN} exec --json --ephemeral "
            f"--skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -C /testbed -o {LAST} - "
            f"< {PROMPT_FILE} > {STREAM} 2> {STDERR}"
        )
        env = {"OPENROUTER_API_KEY": api_key()}
        rec["cost_usd"], rec["usage_complete"] = None, False  # paid work starts: unknown until read
        t1 = time.perf_counter()
        try:
            rc, _ = await box.run(cmd, timeout=time_budget + 120 if time_budget else None, env=env)
        except Exception as e:
            rc = None
            rec["error"] = f"{type(e).__name__}: {e}"
            # STOP kills what it finds; a command codex started outlives codex under a name STOP never
            # matches, so its rc is recorded and proves nothing about the execution
            rec["execution_quiesced"] = False
            try:
                rec["stop_rc"], _ = await box.run(STOP, timeout=60)
            except Exception as e2:
                rec["stop_rc"], rec["stop_error"] = None, f"{type(e2).__name__}: {e2}"
        rec["seconds_in_loop"] = time.perf_counter() - t1
        rec["exit_code"] = rc
        stream = await _read(box, STREAM)
        (out_dir / f"{iid}.codex.jsonl").write_text(stream)
        (out_dir / f"{iid}.codex.err").write_text(await _read(box, STDERR))
        parsed = parse_stream(stream)
        last = (await _read(box, LAST)).strip()
        rec["rounds"] = len(parsed["commands"])
        rec["steps"] = parsed["kinds"]
        rec["commands"] = parsed["commands"]
        rec["failures"] = parsed["failures"] or None
        rec["last_message"] = last[:2000]
        u = parsed["usage"]
        started = parsed["kinds"].get("turn.started", 0)
        unknown = started > 0 and not parsed["metered"]  # paid work and no usage record: no $0, no zeros
        read = u.get("cached_input_tokens", 0)
        rec["tokens"] = (
            None
            if unknown
            else {
                "input": u.get("input_tokens", 0) - read,
                "cache_read": read,
                "cache_create": u.get("cache_write_input_tokens", 0),
                "output": u.get("output_tokens", 0),
            }
        )
        rec["cost_usd"] = None if unknown else cost_of(model, u)
        tokens = rec["tokens"] or {}
        rec["calls"] = [
            {
                "api_ms": int(rec["seconds_in_loop"] * 1000),
                "provider": "openrouter",
                "input": tokens.get("input"),
                "cache_read": tokens.get("cache_read"),
                "output": tokens.get("output"),
                "reasoning": u.get("reasoning_output_tokens"),
                "cost_usd": rec["cost_usd"],
            }
        ]
        # the cap kills codex mid-turn, and that turn's `turn.completed` never arrives: the sums
        # above are short by one turn (measured: a capped run recorded no usage, its patch resolved);
        # a transport failure leaves the same hole, and so does a failed turn: whole only when every
        # turn started was metered
        rec["usage_complete"] = rc is not None and rc not in (124, 137) and parsed["metered"] >= started
        if rc is None:
            rec["outcome"] = "Error:transport"
        elif rc in (124, 137):
            rec["outcome"] = "Exhausted:time"
        elif rc == 0 and not parsed["failures"]:
            rec["outcome"] = "Success"
        else:
            rec["outcome"] = f"Error:{rc}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        rec["error"] = f"{rec['error']}; then {err}" if rec.get("error") else err
    finally:
        if rec.get("execution_quiesced") is False:
            rec["model_patch"] = ""  # codex may still be writing the tree: its diff is no patch
        else:
            try:
                rec["model_patch"] = await box.diff()
            except Exception as e:
                rec["model_patch"] = ""
                rec["diff_error"] = f"{type(e).__name__}: {e}"
        rec["seconds"] = time.perf_counter() - t0
        (out_dir / f"{iid}.json").write_text(json.dumps(rec, indent=1))
    return rec


async def _read(box: Any, path: str) -> str:
    try:
        return await box.read(path)
    except FileNotFoundError:
        return ""
