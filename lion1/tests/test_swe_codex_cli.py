"""The codex-in-the-box arm against a fake box: install, run, harvest; the key stays out of every artifact."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))
import codex_cli  # noqa: E402
from codex_cli import LAST, STDERR, STREAM, cost_of, parse_stream, solve  # noqa: E402

ROW = {"instance_id": "x__x-1", "problem_statement": "It breaks."}
DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"


def stream_text() -> str:
    ev = lambda **e: json.dumps(e)  # noqa: E731
    return "\n".join(
        [
            ev(type="thread.started", thread_id="t"),
            ev(type="turn.started"),
            ev(type="item.completed", item={"type": "command_execution", "command": "ls", "exit_code": 0}),
            ev(
                type="item.completed",
                item={"type": "command_execution", "command": "pytest -q", "exit_code": 0},
            ),
            ev(type="item.completed", item={"type": "agent_message", "text": "Fixed."}),
            ev(
                type="turn.completed",
                usage={"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200},
            ),
        ]
    )


class FakeBox:
    def __init__(self, rc: int = 0):
        self.rc = rc
        self.cmds: list[str] = []
        self.envs: list[dict | None] = []
        self.files: dict[str, str] = {}

    async def run(self, cmd: str, *, timeout: float = 120.0, env: dict | None = None) -> tuple[int, str]:
        self.cmds.append(cmd)
        self.envs.append(env)
        if "curl -fsSL" in cmd:
            return 0, "codex-cli 0.153.4\n"
        if f"{codex_cli.BIN} exec" in cmd:
            self.files[STREAM] = stream_text()
            self.files[STDERR] = ""
            self.files[LAST] = "Fixed."
            return self.rc, ""
        return 0, ""

    async def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write(self, path: str, text: str) -> None:
        self.files[path] = text

    async def diff(self) -> str:
        return DIFF


def test_a_run_is_harvested_into_the_ledger_shape_and_the_key_rides_only_in_the_exec_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    box = FakeBox()
    rec = asyncio.run(solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box, time_budget=100))
    assert rec["outcome"] == "Success" and rec["codex_version"] == "codex-cli 0.153.4"
    assert rec["rounds"] == 2 and rec["commands"] == ["ls", "pytest -q"]
    assert rec["tokens"] == {"input": 600, "cache_read": 400, "cache_create": 0, "output": 200}
    assert rec["cost_usd"] == cost_of(
        "deepseek/deepseek-v4.1-flash",
        {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200},
    )
    assert abs(rec["cost_usd"] - (600 * 0.15e-6 + 400 * 0.003e-6 + 200 * 0.6e-6)) < 1e-12
    assert rec["model_patch"] == DIFF and rec["last_message"] == "Fixed."
    run_cmd = next(c for c in box.cmds if f"{codex_cli.BIN} exec" in c)
    assert "timeout -s KILL 100" in run_cmd and "--dangerously-bypass-approvals-and-sandbox" in run_cmd
    assert box.envs[box.cmds.index(run_cmd)] == {"OPENROUTER_API_KEY": "sk-or-test-key"}
    # the key is nowhere but that environment: not in the config, the prompt, the record, or the run command
    everything = "\n".join(box.files.values()) + "\n".join(box.cmds) + (tmp_path / "x__x-1.json").read_text()
    assert "sk-or-test-key" not in everything
    assert (tmp_path / "x__x-1.codex.jsonl").read_text() == stream_text()
    assert 'model = "deepseek/deepseek-v4.1-flash"' in box.files[f"{codex_cli.HOME}/config.toml"]
    assert (
        box.files[codex_cli.PROMPT_FILE].startswith("Resolve this GitHub issue")
        and "It breaks." in box.files[codex_cli.PROMPT_FILE]
    )


def test_a_run_killed_at_the_budget_is_exhausted_and_its_diff_is_still_the_patch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    rec = asyncio.run(
        solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=FakeBox(rc=137), time_budget=5)
    )
    assert rec["outcome"] == "Exhausted:time" and rec["model_patch"] == DIFF and rec["rounds"] == 2


def test_an_unpriced_model_has_no_cost_and_a_failed_turn_is_named():
    assert cost_of("someone/else", {"input_tokens": 1}) is None
    failed = '{"type":"turn.failed","error":{"message":"401 no"}}'
    parsed = parse_stream(failed + '\n{"type":"turn.completed","usage":{"input_tokens":3}}')
    assert (
        parsed["failures"] == ["401 no"]
        and parsed["usage"] == {"input_tokens": 3}
        and parsed["commands"] == []
    )


class TimedBox(FakeBox):
    """Records the timeout each command was handed."""

    def __init__(self):
        super().__init__()
        self.timeouts: list = []

    async def run(self, cmd, *, timeout=120.0, env=None):
        self.timeouts.append(timeout)
        return await super().run(cmd, timeout=timeout, env=env)


def test_time_budget_zero_means_no_deadline(tmp_path, monkeypatch):
    import argparse

    import run_set

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for none in (None, 0, 0.0):
        box = TimedBox()
        rec = asyncio.run(solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box, time_budget=none))
        execs = [(c, t) for c, t in zip(box.cmds, box.timeouts, strict=True) if f"{codex_cli.BIN} exec" in c]
        assert len(execs) == 1, (none, rec.get("error"))
        assert "timeout -s KILL" not in execs[0][0] and execs[0][1] is None and rec["outcome"] == "Success"
    box = TimedBox()
    asyncio.run(solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box, time_budget=100))
    execs = [(c, t) for c, t in zip(box.cmds, box.timeouts, strict=True) if f"{codex_cli.BIN} exec" in c]
    assert "timeout -s KILL 100 " in execs[0][0] and execs[0][1] == 220  # the control: a set deadline
    # `--time-budget 0` ("0 never") reaches the arm as no deadline, not as 1200 s; a cost cap on this
    # arm is refused before the run (tests/test_swe_run_set.py)
    seen: dict = {}

    async def fake_codex(row, model, out_dir, **kw):
        seen.update(kw)
        return {"instance_id": row["instance_id"], "model_patch": ""}

    class Started:
        async def start(self):
            return self

        async def stop(self):
            pass

    monkeypatch.setattr(run_set, "testbed", lambda *a, **k: Started())
    monkeypatch.setattr(run_set, "solve_codex_cli", fake_codex)
    args = argparse.Namespace(
        where="daytona",
        cpu=2,
        memory=2,
        run_id="t",
        harness="codex-cli",
        model="m",
        time_budget=0.0,
        unaided=False,
        keep_images=False,
    )
    asyncio.run(
        run_set.one(
            {"instance_id": "b", "image": "img"}, args, tmp_path, tmp_path / "l.jsonl", asyncio.Lock()
        )
    )
    assert "time_budget" in seen and seen["time_budget"] is None


class GatewayTimeoutBox(FakeBox):
    """codex ran and wrote its stream in the box; the host's exec call came back as a 504. The stop
    probe's answer is explicit: 0 when no codex process is left, 1 when one survived the kills."""

    def __init__(self, stop_rc: int):
        super().__init__()
        self.stop_rc, self.diffs = stop_rc, 0

    async def run(self, cmd, *, timeout=120.0, env=None):
        if f"{codex_cli.BIN} exec" in cmd:
            self.cmds.append(cmd)
            usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}
            self.files[STREAM] = json.dumps({"type": "turn.completed", "usage": usage})
            self.files[STDERR] = ""
            self.files[LAST] = ""
            raise RuntimeError("DaytonaTimeoutError: Failed to execute command: 504 Gateway Time-out")
        if "/proc/" in cmd:  # the stop probe
            self.cmds.append(cmd)
            return self.stop_rc, ""
        return await super().run(cmd, timeout=timeout, env=env)

    async def diff(self):
        self.diffs += 1
        return DIFF


def test_a_transport_error_harvests_the_stream_and_takes_no_patch_whatever_stop_answers(
    tmp_path, monkeypatch
):
    # STOP 0 once took the patch; a command codex started outlives codex and matches nothing STOP
    # looks for, so its rc is recorded, never read as a stop
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for stop_rc in (0, 1):
        box = GatewayTimeoutBox(stop_rc=stop_rc)
        rec = asyncio.run(solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box))
        assert rec["execution_quiesced"] is False and rec["model_patch"] == "" and box.diffs == 0
        assert rec["stop_rc"] == stop_rc and codex_cli.STOP in box.cmds
        assert "504 Gateway Time-out" in rec["error"] and rec.get("exit_code", "unset") is None
        assert rec["tokens"]["output"] == 200 and rec["cost_usd"] is not None  # from the stream it left
        assert rec["usage_complete"] is False and rec["outcome"] == "Error:transport"


@pytest.mark.parametrize("returncode", [0, 1])
def test_missing_stream_usage_stays_unknown(tmp_path, monkeypatch, returncode):
    # a failed turn that left no usage is no complete $0
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-fake-key")

    class MissingUsage(FakeBox):
        async def run(self, cmd, **kw):
            result = await super().run(cmd, **kw)
            if f"{codex_cli.BIN} exec" in cmd:
                self.files[STREAM] = "\n".join(
                    [
                        json.dumps({"type": "turn.started"}),
                        json.dumps({"type": "turn.failed", "error": {"message": "lost response"}}),
                    ]
                )
            return result

    row = asyncio.run(
        codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=MissingUsage(rc=returncode))
    )
    print(json.dumps(row, sort_keys=True))
    assert row["cost_usd"] is None and row["usage_complete"] is False


class StreamBox(FakeBox):
    """codex ran and left `events` as its stream."""

    def __init__(self, *events: dict):
        super().__init__()
        self.events = events

    async def run(self, cmd, *, timeout=120.0, env=None):
        result = await super().run(cmd, timeout=timeout, env=env)
        if f"{codex_cli.BIN} exec" in cmd:
            self.files[STREAM] = "\n".join(json.dumps(e) for e in self.events)
        return result


STARTED, LOST = {"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "lost"}}


def test_paid_work_short_of_a_usage_record_is_unknown_or_a_partial_subtotal(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}

    def solved(*events: dict) -> dict:
        return asyncio.run(solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=StreamBox(*events)))

    rec = solved(STARTED, LOST)  # nothing metered: the tokens are unknown too, not zeros
    assert rec["tokens"] is None and rec["calls"][0]["output"] is None and rec["calls"][0]["cost_usd"] is None
    rec = solved(
        STARTED, {"type": "turn.completed", "usage": usage}, STARTED, LOST
    )  # the last turn unmetered
    assert (
        rec["cost_usd"] == cost_of("deepseek/deepseek-v4.1-flash", usage) and rec["usage_complete"] is False
    )
    assert rec["tokens"]["output"] == 200 and rec["calls"][0]["cost_usd"] == rec["cost_usd"]


def test_a_provider_reporting_zero_usage_is_a_complete_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    zero = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    box = StreamBox(STARTED, {"type": "turn.completed", "usage": zero})
    rec = asyncio.run(solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box))
    assert rec["cost_usd"] == 0.0 and rec["usage_complete"] is True and rec["tokens"]["output"] == 0
