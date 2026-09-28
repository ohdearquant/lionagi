"""solve(): what the record keeps when the backend dies mid-run."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))
from run_swe import solve  # noqa: E402

from hub.harness import CodexError
from hub.tools import SandboxDead

DIFF = "diff --git a/pkg/x.py b/pkg/x.py\n--- a/pkg/x.py\n+++ b/pkg/x.py\n@@ -1 +1 @@\n-old\n+new\n"


class Box:
    async def diff(self) -> str:
        return DIFF


class DiesOnRoundTwo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, messages) -> str:
        if self.calls:
            raise CodexError("codex exited 1: unexpected status 401 (stream: /x/last.jsonl)")
        self.calls.append(
            {
                "duration_api_ms": 5,
                "attempts": 1,
                "total_cost_usd": None,
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 2},
                "notes": [],
            }
        )
        return "Reading the code first.\n"  # a turn that runs nothing and declares nothing


def test_the_tdd_flag_swaps_the_no_tests_sentence_and_the_other_flags_append():
    from run_swe import CRITERIA, DELIBERATE, FIXER, NO_TESTS, TDD, guidance

    assert guidance() == FIXER.system and NO_TESTS in guidance() and TDD not in guidance()
    with_tdd = guidance(tdd=True)
    assert TDD in with_tdd and NO_TESTS not in with_tdd and with_tdd.startswith(FIXER.system[:40])
    assert guidance(criteria=True, deliberate=True, tdd=True).endswith(CRITERIA + DELIBERATE)


def test_the_thinking_flag_maps_to_the_reasoning_field():
    from run_swe import _reasoning

    assert _reasoning("off") == {"enabled": False}
    assert _reasoning("on") is None
    assert _reasoning("2000") == {"max_tokens": 2000}
    assert _reasoning("low") == {"effort": "low"}
    with pytest.raises(ValueError, match="--thinking takes"):
        _reasoning("maybe")


def test_a_backend_failure_keeps_the_rounds_that_ran_and_the_fix_on_disk(tmp_path: Path):
    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}
    rec = asyncio.run(solve(row, "m", 5, tmp_path, "codex", box=Box(), backend=DiesOnRoundTwo()))
    assert rec["error"].startswith("CodexError: codex exited 1")
    assert rec["rounds"] == 1 and len(rec["calls"]) == 1 and rec["calls"][0]["attempts"] == 1
    assert rec["tokens"] == {"input": 10, "cache_read": 3, "cache_create": 0, "output": 2}
    assert rec["model_patch"] == DIFF  # graded whatever ended the loop
    assert "watch" in rec and rec["outcome"] is None
    on_disk = json.loads((tmp_path / "pkg__pkg-1.json").read_text())
    assert on_disk["model_patch"] == DIFF and on_disk["error"] == rec["error"]
    assert (tmp_path / "progress.log").read_text().count("pkg__pkg-1 round") == 2


class ToolBox(Box):
    """A box the tools can use: every command succeeds, nothing is ever edited."""

    dead = None

    async def exec(self, argv: list[str], *, stdin=None, timeout: float = 120.0, env=None) -> dict:
        return {"rc": 0, "stdout": "a.py\n", "stderr": "", "elapsed": 0.0}

    async def run(self, cmd: str, *, timeout: float = 120.0, env=None) -> tuple[int, str]:
        return 0, "a.py\n"

    async def read(self, path: str) -> str:
        return "x = 1\n"

    async def write(self, path: str, text: str) -> None:
        pass


class Turns:
    """A backend answering from a script; each turn also says what OpenRouter would about the call."""

    max_tokens = 100

    def __init__(self, *turns: tuple[str, dict]):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def __call__(self, messages) -> str:
        text, envelope = self.turns.pop(0)
        usage = {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "output_tokens": 2,
            **envelope.pop("usage", {}),
        }
        self.calls.append(
            {"duration_api_ms": 1, "attempts": 1, "total_cost_usd": None, "usage": usage, **envelope}
        )
        return text


def test_a_turn_cut_at_the_cap_or_blank_is_named_in_the_next_notice(tmp_path: Path):
    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}
    backend = Turns(
        (
            '<lact a1>bash(command="ls")</lact>',
            {"finish_reason": "length", "usage": {"reasoning_output_tokens": 40}},
        ),
        ("   ", {"finish_reason": None, "message": {"content": "   "}}),
        ('<lact a3>bash(command="ls")</lact>', {"finish_reason": "stop"}),
    )
    rec = asyncio.run(solve(row, "m", 3, tmp_path, "codex", box=ToolBox(), backend=backend))
    record = (tmp_path / "pkg__pkg-1.record.txt").read_text()
    assert "cut off at the output cap of 100 tokens, 40 of them reasoning" in record
    assert "came back with no readable text (2 output tokens, finish_reason=None)" in record
    assert rec["calls"][0]["finish"] == "length" and "message" not in rec["calls"][0]
    assert rec["calls"][1]["message"] == {"content": "   "}
    assert rec["outcome"] == "Exhausted:rounds" and rec["rounds"] == 3


class DyingBox(ToolBox):
    """The first command kills the box, as a Daytona container with no IP does."""

    async def exec(self, argv: list[str], *, stdin=None, timeout: float = 120.0, env=None) -> dict:
        self.dead = "failed to resolve container IP after 3 attempts"
        raise SandboxDead(self.dead)

    async def diff(self) -> str:
        if self.dead:
            raise SandboxDead(self.dead)
        return ""


def test_a_dead_box_ends_the_run_at_the_next_round_as_an_infrastructure_error(tmp_path: Path):
    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}
    backend = Turns(*[('<lact a1>bash(command="true")</lact>', {"finish_reason": "stop"})] * 5)
    rec = asyncio.run(solve(row, "m", 5, tmp_path, "codex", box=DyingBox(), backend=backend))
    assert rec["error"] == "SandboxDead: failed to resolve container IP after 3 attempts"
    assert rec["rounds"] == 1 and len(backend.turns) == 4  # one turn ran, the box was dead, no more
    assert rec["diff_error"].startswith("SandboxDead") and rec["model_patch"] == ""


class FilesBox(ToolBox):
    """A box with real files; reads and writes yield like network calls do."""

    def __init__(self) -> None:
        self.files = {"a.py": "one\ntwo\n"}

    async def read(self, path: str) -> str:
        await asyncio.sleep(0)
        return self.files[path]

    async def write(self, path: str, text: str) -> None:
        await asyncio.sleep(0)
        self.files[path] = text


def test_two_edits_to_one_file_in_one_turn_both_land(tmp_path: Path):
    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}
    box = FilesBox()
    turn = (
        '<lact e1>edit(path="a.py", old="one", new="ONE")</lact>\n'
        '<lact e2>edit(path="a.py", old="two", new="TWO")</lact>'
    )
    asyncio.run(
        solve(row, "m", 1, tmp_path, "codex", box=box, backend=Turns((turn, {"finish_reason": "stop"})))
    )
    assert box.files["a.py"] == "ONE\nTWO\n"  # the second edit carried the first, not the original


class Costs:
    """A backend whose calls say what they cost, one figure per turn: None where nothing said."""

    def __init__(self, *costs):
        self.costs, self.calls = list(costs), []

    async def __call__(self, messages) -> str:
        cost = self.costs.pop(0)
        if cost == "raise":
            raise RuntimeError("the backend failed before anything was billed")
        self.calls.append(
            {"duration_api_ms": 1, "attempts": 1, "total_cost_usd": cost, "usage": {"output_tokens": 30_000}}
        )
        return "Reading the code first.\n"


def test_missing_usage_is_incomplete_not_zero(tmp_path: Path):
    import run_set

    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}

    def run(kind, *costs):
        return asyncio.run(solve(row, "m", len(costs), tmp_path, kind, box=ToolBox(), backend=Costs(*costs)))

    # with no cap: a meter that never answers
    unmetered = run("codex", None, None)
    assert len(unmetered["calls"]) == 2
    assert unmetered["cost_usd"] is None and unmetered["cost_complete"] is False
    mixed = run("openrouter", 0.10, None)  # the known subtotal, beside the flag that it is partial
    assert mixed["cost_usd"] == 0.10 and mixed["cost_complete"] is False
    known = run("openrouter", 0.10, 0.20)
    assert known["cost_usd"] == pytest.approx(0.30) and known["cost_complete"] is True
    nothing = run("openrouter", "raise")  # no paid work started: zero known spend, and complete
    assert nothing["cost_usd"] == 0.0 and nothing["cost_complete"] is True and nothing["error"]
    # the run's summary adds what is known and says where it is not
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in (unmetered, mixed, known, nothing)))
    text = run_set.summary(ledger)
    assert "cost $0.40 known" in text and "unknown or partial on 2 of 4 rows" in text


class FailedEnvelopeThenRaise(Costs):
    """A backend that, like OpenRouter and Codex, lands a failed call's envelope on `calls` and raises."""

    async def __call__(self, messages) -> str:
        if self.costs == ["fail"]:
            self.calls.append({"error": "HTTP 400: bad", "attempts": 1, "total_cost_usd": 0.05, "usage": {}})
            raise RuntimeError("HTTP 400: bad")
        return await super().__call__(messages)


def test_a_failed_envelope_is_not_counted_as_a_round(tmp_path: Path):
    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}
    backend = FailedEnvelopeThenRaise(0.10, "fail")
    rec = asyncio.run(solve(row, "m", 5, tmp_path, "openrouter", box=ToolBox(), backend=backend))
    assert rec["rounds"] == 1 and len(rec["calls"]) == 2  # the failed call is spend, not a round
    assert rec["calls"][1]["error"] == "HTTP 400: bad" and rec["cost_usd"] == pytest.approx(0.15)


class DiffBox(FilesBox):
    """Like FilesBox, and its diff is empty until a file really changed."""

    async def diff(self) -> str:
        return DIFF if self.files["a.py"] != "one\ntwo\n" else ""


def test_an_edit_beside_the_finish_runs_before_the_finish_is_judged(tmp_path: Path):
    row = {"instance_id": "pkg__pkg-1", "problem_statement": "x is wrong"}
    box = DiffBox()
    out = 'OUT{done: {summary: "fixed", files: ["a.py"]}}'
    turns = [
        (f'<lact e1>edit(path="a.py", old="one", new="ONE")</lact>\n{out}', {"finish_reason": "stop"}),
        # the finish beside the edit is refused (unverified); a check in the next round, then it lands
        (f'<lact b1>bash(command="python -m pytest -q")</lact>\n{out}', {"finish_reason": "stop"}),
        (out, {"finish_reason": "stop"}),
    ]
    rec = asyncio.run(solve(row, "m", 3, tmp_path, "codex", box=box, backend=Turns(*turns)))
    assert box.files["a.py"] == "ONE\ntwo\n"  # the edit ran before the finish was judged
    record = (tmp_path / "pkg__pkg-1.record.txt").read_text()
    assert "the source changed in this round; nothing has run against that change" in record
    assert rec["outcome"] == "Success" and rec["rounds"] == 2 and rec["model_patch"] == DIFF
