"""The codex-in-the-box arm against a fake box: install, run, harvest; the key stays out of every artifact."""

import asyncio
import json

import pytest

from tests._swe import BASE, DIFF, CodexBox, GatewayTimeoutBox, codex_cli, stream_text

ROW = {"instance_id": "x__x-1", "problem_statement": "It breaks."}


def test_a_run_is_harvested_into_the_ledger_shape_and_the_key_rides_only_in_the_exec_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    box = CodexBox()
    rec = asyncio.run(
        codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box, time_budget=100)
    )
    assert rec["outcome"] == "Success" and rec["codex_version"] == "codex-cli 0.153.4"
    assert rec["turns"] == 2 and rec["commands"] == ["ls", "pytest -q"]
    assert rec["tokens"] == {"input": 600, "cache_read": 400, "cache_create": 0, "output": 200}
    assert rec["cost_usd"] == codex_cli.cost_of(
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
        codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=CodexBox(rc=137), time_budget=5)
    )
    assert rec["outcome"] == "Exhausted:time" and rec["model_patch"] == DIFF and rec["turns"] == 2


def test_an_unpriced_model_has_no_cost_and_a_failed_turn_is_named():
    assert codex_cli.cost_of("someone/else", {"input_tokens": 1}) is None
    failed = '{"type":"turn.failed","error":{"message":"401 no"}}'
    parsed = codex_cli.parse_stream(failed + '\n{"type":"turn.completed","usage":{"input_tokens":3}}')
    assert (
        parsed["failures"] == ["401 no"]
        and parsed["usage"] == {"input_tokens": 3}
        and parsed["commands"] == []
    )


class TimedBox(CodexBox):
    """Records the timeout each command was handed."""

    def __init__(self):
        super().__init__()
        self.timeouts: list = []

    async def run(self, cmd, *, timeout=120.0, env=None):
        self.timeouts.append(timeout)
        return await super().run(cmd, timeout=timeout, env=env)


def test_time_budget_zero_means_no_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    for none in (None, 0, 0.0):
        box = TimedBox()
        rec = asyncio.run(
            codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box, time_budget=none)
        )
        execs = [(c, t) for c, t in zip(box.cmds, box.timeouts, strict=True) if f"{codex_cli.BIN} exec" in c]
        assert len(execs) == 1, (none, rec.get("error"))
        assert "timeout -s KILL" not in execs[0][0] and execs[0][1] is None and rec["outcome"] == "Success"
    box = TimedBox()
    asyncio.run(codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box, time_budget=100))
    execs = [(c, t) for c, t in zip(box.cmds, box.timeouts, strict=True) if f"{codex_cli.BIN} exec" in c]
    assert "timeout -s KILL 100 " in execs[0][0] and execs[0][1] == 220  # the control: a set deadline


@pytest.mark.parametrize("stop_rc", [0, 1, None])
def test_a_transport_error_harvests_the_stream_and_takes_no_patch_whatever_stop_answers(
    tmp_path, monkeypatch, stop_rc
):
    # STOP 0 once took the patch; a command codex started outlives codex and matches nothing STOP
    # looks for, so its rc is recorded, never read as a stop; a probe that failed proves nothing either
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    box = GatewayTimeoutBox(stop_rc=stop_rc)
    rec = asyncio.run(codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box))
    assert rec["execution_quiesced"] is False and rec["model_patch"] == "" and box.diffs == 0
    assert rec["stop_rc"] == stop_rc and codex_cli.STOP in box.cmds
    assert ("stop_error" in rec) is (stop_rc is None)
    assert "504 Gateway Time-out" in rec["error"] and rec.get("exit_code", "unset") is None
    assert rec["tokens"]["output"] == 200 and rec["cost_usd"] is not None  # from the stream it left
    assert rec["usage_complete"] is False and rec["outcome"] == "Error:transport"


class StreamBox(CodexBox):
    """codex ran, left `events` as its stream, and exited `rc`."""

    def __init__(self, *events: dict, rc: int = 0):
        super().__init__(rc)
        self.events = events

    async def run(self, cmd, *, timeout=120.0, env=None):
        result = await super().run(cmd, timeout=timeout, env=env)
        if f"{codex_cli.BIN} exec" in cmd:
            self.files[codex_cli.STREAM] = "\n".join(json.dumps(e) for e in self.events)
        return result


STARTED, LOST = {"type": "turn.started"}, {"type": "turn.failed", "error": {"message": "lost"}}


@pytest.mark.parametrize("rc", [0, 1])  # a process that failed says no more about its spend
def test_paid_work_short_of_a_usage_record_is_unknown_or_a_partial_subtotal(tmp_path, monkeypatch, rc):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}

    def solved(*events: dict) -> dict:
        return asyncio.run(
            codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=StreamBox(*events, rc=rc))
        )

    rec = solved(STARTED, LOST)  # nothing metered: the tokens are unknown too, not zeros
    assert rec["cost_usd"] is None and rec["usage_complete"] is False
    assert rec["tokens"] is None and rec["calls"][0]["output"] is None and rec["calls"][0]["cost_usd"] is None
    rec = solved(
        STARTED, {"type": "turn.completed", "usage": usage}, STARTED, LOST
    )  # the last turn unmetered
    assert (
        rec["cost_usd"] == codex_cli.cost_of("deepseek/deepseek-v4.1-flash", usage)
        and rec["usage_complete"] is False
    )
    assert rec["tokens"]["output"] == 200 and rec["calls"][0]["cost_usd"] == rec["cost_usd"]


def test_a_provider_reporting_zero_usage_is_a_complete_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    zero = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    box = StreamBox(STARTED, {"type": "turn.completed", "usage": zero})
    rec = asyncio.run(codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box))
    assert rec["cost_usd"] == 0.0 and rec["usage_complete"] is True and rec["tokens"]["output"] == 0
    assert rec["outcome"] == "Success"  # one metered turn is a reading, complete, and a success


def test_the_codex_prediction_is_the_diff_against_the_commit_it_was_handed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    class BaseBox(CodexBox):
        bases: list[str] = []

        async def diff(self, base: str = "HEAD") -> str:
            self.bases.append(base)
            return DIFF

    box = BaseBox()
    rec = asyncio.run(codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box))
    assert rec["base"] == BASE and box.bases == [BASE] and rec["model_patch"] == DIFF


@pytest.mark.parametrize(
    "stream", ["", "not json\n{\n", 'null\n1\n"x"\n', json.dumps({"type": "thread.started"}) + "\n"]
)
def test_a_stream_with_no_turn_is_no_reading_and_no_success(tmp_path, monkeypatch, stream):
    # exit 0 beside an empty, unparsable or turnless stream once read as Success at $0, complete
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    class NoTurn(CodexBox):
        async def run(self, cmd, **kw):
            result = await super().run(cmd, **kw)
            if f"{codex_cli.BIN} exec" in cmd:
                self.files[codex_cli.STREAM] = stream
            return result

    rec = asyncio.run(codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=NoTurn(rc=0)))
    assert rec["cost_usd"] is None and rec["tokens"] is None and rec["usage_complete"] is False
    assert rec["outcome"] == "Error:no-turn" and rec["exit_code"] == 0


@pytest.mark.parametrize("usage", [{"total_tokens": 1}, {"input_tokens": 5}, "1", None])
def test_a_usage_without_input_and_output_tokens_is_no_reading(tmp_path, monkeypatch, usage):
    # a total alone once read as a metered turn at $0 with zero tokens, complete
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    box = StreamBox(STARTED, {"type": "turn.completed", "usage": usage})
    rec = asyncio.run(codex_cli.solve(ROW, "deepseek/deepseek-v4.1-flash", tmp_path, box=box))
    assert rec["cost_usd"] is None and rec["tokens"] is None and rec["usage_complete"] is False
