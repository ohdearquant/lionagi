# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``flow.submit(graph=...)`` checks a written graph before any job record exists.

Every refusal happens in the submit, so a refused graph leaves no job directory and nothing
on ``job.list``. The command line and the submit read a file with the same loader, so one
file gets one answer on both paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from lionhub.serve.mcp import dispatch, jobs


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def spawn_op(op: str, args: dict) -> dict:
    return {"op": op, "args": args, "schema_fingerprint": call(help=op)["schema_fingerprint"]}


class _RecordingPopen:
    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.pid = os.getpid()

    def __call__(self, argv: list[str], *a: Any, **kw: Any) -> Any:
        # Profile lookup asks git for the repository root; the project here is no
        # repository, so answer that lookup as a machine without git would.
        if argv and argv[0] == "git":
            raise FileNotFoundError("git")
        self.argv = list(argv)
        return self

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def world(monkeypatch, tmp_path: Path) -> dict[str, Any]:
    """A project declaring the profile ``worker``, a jobs root under tmp_path, no real spawn."""
    home = tmp_path / "home"
    (home / ".lionagi" / "agents").mkdir(parents=True)
    project = tmp_path / "project"
    agents = project / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text("---\nmodel: claude_code/sonnet\n---\nWork.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    jobs_root = tmp_path / "jobs"
    monkeypatch.setattr(jobs.config, "JOBS_DIR", jobs_root, raising=False)
    popen = _RecordingPopen()
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    monkeypatch.chdir(project)
    return {"project": project, "jobs": jobs_root, "popen": popen, "tmp": tmp_path}


def _node(node_id: str, needs: list[str] | None = None, agent: str = "worker") -> dict:
    return {"id": node_id, "agent": agent, "prompt": f"do {node_id}", "needs": needs or []}


def _graph(tmp: Path, document: Any, name: str = "g.json") -> str:
    path = tmp / name
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return str(path)


def _no_job_left(world) -> None:
    root = world["jobs"]
    assert not root.exists() or list(root.iterdir()) == [], "a refused graph left a job directory"
    assert world["popen"].argv is None, "a refused graph reached the spawn"
    assert jobs.list_jobs() == []


_REFUSED = {
    "empty": ({"nodes": []}, "empty 'nodes' list"),
    "repeated-id": ({"nodes": [_node("a"), _node("a")]}, "'a' is repeated"),
    "unsafe-id": ({"nodes": [_node("../x")]}, "not a safe path component"),
    "unknown-need": ({"nodes": [_node("a", ["ghost"])]}, "names no node"),
    "self-need": ({"nodes": [_node("a", ["a"])]}, "needs itself"),
    "cycle": ({"nodes": [_node("a", ["b"]), _node("b", ["a"])]}, "has a cycle"),
    "unknown-agent": ({"nodes": [_node("a", agent="no-such")]}, "resolves to no profile"),
    "too-many-nodes": (
        {"nodes": [_node(f"n{i}") for i in range(201)]},
        "a graph may have at most 200",
    ),
    "unknown-node-key": (
        {"nodes": [{**_node("a"), "color": "red"}]},
        "a node does not take: color",
    ),
    "unknown-top-key": ({"nodes": [_node("a")], "x": 1}, "a graph does not take: x"),
    "repeated-name": ('{"nodes": [], "nodes": []}', "repeats a name inside an object"),
}


@pytest.mark.parametrize("dry_run", [False, True], ids=["submit", "dry-run"])
@pytest.mark.parametrize("case", sorted(_REFUSED))
def test_each_refusal_happens_before_any_job_exists(world, case, dry_run):
    document, needle = _REFUSED[case]
    args = {"graph": _graph(world["tmp"], document), "cwd": str(world["project"])}
    if dry_run:
        args["dry_run"] = True
    answer = call(ops=[spawn_op("flow.submit", args)])["ops"][0]

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert needle in answer["error"]["message"], answer
    _no_job_left(world)


def test_max_ops_is_a_refusal_at_submit(world):
    args = {
        "graph": _graph(world["tmp"], {"nodes": [_node("a"), _node("b")]}),
        "cwd": str(world["project"]),
        "max_ops": 1,
    }
    answer = call(ops=[spawn_op("flow.submit", args)])["ops"][0]
    assert answer["ok"] is False, answer
    assert "more than --max-ops 1" in answer["error"]["message"]
    _no_job_left(world)


@pytest.mark.parametrize(
    ("extra", "flag"),
    [
        ({"file": "/spec.yaml"}, "-f/--file"),
        ({"reactive": "all"}, "--reactive all"),
        ({"resume": "20260101T000000-abcdef"}, "--resume"),
        ({"with_synthesis": True}, "--with-synthesis"),
        ({"bare": True}, "--bare"),
    ],
)
def test_a_prompt_driven_option_beside_a_graph_is_refused(world, extra, flag):
    args = {"graph": _graph(world["tmp"], {"nodes": [_node("a")]}), **extra}
    answer = call(ops=[spawn_op("flow.submit", args)])["ops"][0]
    assert answer["ok"] is False, answer
    assert f"--graph cannot be combined with {flag}" in answer["error"]["message"], answer
    _no_job_left(world)


def test_play_submit_takes_no_graph(world):
    schema = call(help="play.submit")
    assert "graph" not in schema.get("schema", schema).get("properties", {})
    # named among the refusals, with its reason
    assert "run a written graph with flow.submit" in json.dumps(schema)


def _flow_namespace(argv: list[str]) -> argparse.Namespace:
    from lionhub.serve.cli.orchestrate import add_orchestrate_subparser

    parser = argparse.ArgumentParser(prog="li", add_help=False)
    flow = add_orchestrate_subparser(parser.add_subparsers(dest="command"))["flow"]
    return flow.parse_args(argv[argv.index("flow") + 1 :])


def test_an_accepted_graph_reaches_the_run_through_its_copy(world):
    path = _graph(world["tmp"], {"nodes": [_node("a"), _node("b", ["a"])]})
    answer = call(ops=[spawn_op("flow.submit", {"graph": path, "cwd": str(world["project"])})])[
        "ops"
    ][0]

    assert answer["ok"] is True, answer
    run_dir = world["jobs"] / answer["result"]["run_id"]
    copy = run_dir / "graph.json"
    assert copy.read_bytes() == Path(path).read_bytes()
    assert oct(copy.stat().st_mode & 0o777) == oct(0o600)

    ns = _flow_namespace(world["popen"].argv)
    assert ns.graph == path
    assert ns.graph_submit_snapshot == str(copy)
    assert ns.graph_submit_resolved == path
    assert not ns.query, "a graph needs no prompt"


@pytest.mark.parametrize("case", ["repeated-name", "cycle", "unknown-agent", "unsafe-id"])
def test_one_file_gets_the_same_answer_from_the_command_line(world, monkeypatch, case):
    """The refusal text of ``li o flow --graph`` is the submit's, a repeated name included."""
    import lionhub.serve.cli.orchestrate as orchestrate_mod

    document, _ = _REFUSED[case]
    path = _graph(world["tmp"], document, name=f"{case}.json")
    answer = call(ops=[spawn_op("flow.submit", {"graph": path, "cwd": str(world["project"])})])[
        "ops"
    ][0]
    ns = _flow_namespace(["flow", "--graph", path, "--cwd", str(world["project"])])
    logged: list[str] = []
    monkeypatch.setattr(orchestrate_mod, "log_error", logged.append)

    assert orchestrate_mod._load_checked_graph(ns) is None
    assert logged == [answer["error"]["message"]], (logged, answer)
