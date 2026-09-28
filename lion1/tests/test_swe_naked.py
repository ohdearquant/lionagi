"""The naked arm: a harness failure after paid work still records the patch on disk and the spend."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))

import run_naked  # noqa: E402

ROW = {"instance_id": "x__x-1", "image": "img", "problem_statement": "It breaks."}
ENV = {
    "subtype": "error_max_turns",
    "num_turns": 3,
    "total_cost_usd": 0.25,
    "usage": {"input_tokens": 10, "output_tokens": 4},
}


def _fake_docker(monkeypatch):
    """`docker cp` puts a repository at the host path; every other docker call is a no-op."""
    calls = []

    async def docker(*argv, check=True):
        calls.append(argv)
        if argv[0] == "cp":
            host = Path(argv[2])
            env = {
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.com",
                "PATH": __import__("os").environ["PATH"],
            }
            subprocess.run(["git", "init", "-q"], cwd=host, env=env, check=True)
            (host / "a.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "a.py"], cwd=host, env=env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=host, env=env, check=True)
        return 0, ""

    monkeypatch.setattr(run_naked, "_docker", docker)
    return calls


class Harness:
    """Edits a.py in its cwd, records a paid envelope, and then raises (or returns) as `fail` says."""

    def __init__(self, *, model, tools, max_turns, cwd, timeout):
        self.cwd, self.calls = Path(cwd), []

    commit = False

    async def __call__(self, prompt: str) -> str:
        (self.cwd / "a.py").write_text("x = 2\n")
        if Harness.commit:  # the model committed its fix: HEAD moved past the commit it was handed
            env = {
                "GIT_AUTHOR_NAME": "m",
                "GIT_AUTHOR_EMAIL": "m@example.com",
                "GIT_COMMITTER_NAME": "m",
                "GIT_COMMITTER_EMAIL": "m@example.com",
                "PATH": __import__("os").environ["PATH"],
            }
            subprocess.run(["git", "commit", "-q", "-am", "fix"], cwd=self.cwd, env=env, check=True)
        self.calls.append(dict(ENV))
        if Harness.fail:
            raise TimeoutError("the harness hung after its last turn")
        return "done"


def test_a_harness_failure_after_paid_work_keeps_the_patch_and_the_spend(tmp_path, monkeypatch):
    _fake_docker(monkeypatch)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail = True
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert rec["error"] == "TimeoutError: the harness hung after its last turn"
    assert rec["cost_usd"] == 0.25 and rec["turns"] == 3 and rec["tokens"]["output"] == 4
    assert "+x = 2" in rec["model_patch"]
    assert json.loads((tmp_path / "x__x-1.json").read_text())["model_patch"] == rec["model_patch"]
    # the control: the same work, returned, records the same evidence and no error
    Harness.fail = False
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert "error" not in rec and rec["cost_usd"] == 0.25 and "+x = 2" in rec["model_patch"]
    assert rec["outcome"] == "error_max_turns" and rec["final_text"] == "done"


def test_a_commit_the_model_made_stays_in_the_naked_prediction(tmp_path, monkeypatch):
    _fake_docker(monkeypatch)
    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Harness)
    Harness.fail, Harness.commit = False, True
    try:
        rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    finally:
        Harness.commit = False
    assert "+x = 2" in rec["model_patch"] and len(rec["base"]) == 40


def test_a_failure_before_any_turn_records_the_error_alone(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch)

    class Early(Harness):
        async def __call__(self, prompt):
            raise RuntimeError("no key")

    monkeypatch.setattr(run_naked, "ClaudeCodeAgentic", Early)
    rec = asyncio.run(run_naked.solve(ROW, "m", 3, tmp_path))
    assert rec["error"] == "RuntimeError: no key" and "cost_usd" not in rec and rec["model_patch"] == ""
    assert calls[-1][:3] == ("container", "remove", "--force")  # the box is still cleaned up
