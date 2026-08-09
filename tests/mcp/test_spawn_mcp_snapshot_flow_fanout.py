# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""An orchestration run's MCP server set is chosen by the submission too.

A flow or fanout spawns many workers, so "whatever each provider CLI discovers
for itself" is the one answer nobody asked for. These tests hold the flow and
fanout verbs to the same contract the agent verb already keeps: the caller may
name a config or refuse one, the resolved set is snapshotted into the run's own
directory, and the handle reports what the child actually reads.

Popen is doubled so no real `li` process is spawned; the tests read the argv the
engine built and resolve it the way the child would.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lionagi.cli._mcp_resolve import resolve_spawn_mcp_servers
from lionagi.mcp import config, dispatch, jobs

ORCHESTRATION_KINDS = ("flow", "fanout")


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    return tmp_path


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


@pytest.fixture
def submit_dir(monkeypatch, tmp_path):
    """A submitting directory holding its own .mcp.json, with no config above it.

    The search walks to the filesystem root, so an ancestor's real .mcp.json
    would otherwise decide these tests.
    """
    d = tmp_path / "submit"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def _child_config(argv: list[str]) -> str | None:
    """The config the child's parser would settle on, in either spelling.

    argparse keeps the last occurrence, so a line carrying two of these does not
    fail — it quietly picks one. Reading it the same way is what lets a test say
    which file the child actually opens rather than which one appears first.
    """
    seen: list[str] = []
    for i, tok in enumerate(argv):
        if tok == "--mcp-config" and i + 1 < len(argv):
            seen.append(argv[i + 1])
        elif tok.startswith("--mcp-config="):
            seen.append(tok.split("=", 1)[1])
    return seen[-1] if seen else None


def _capture_popen(monkeypatch) -> dict:
    captured: dict = {}
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda argv, **kw: (captured.update(argv=argv), _FakeProc())[1],
    )
    return captured


def _spawn(verb: str, args: dict, *, playbook: str | None = None) -> dict:
    """Drive the real spawn verb, fingerprint fetched the way a caller must.

    A verb that takes a playbook is projected again once one is named, so its
    fingerprint is a function of that argument and help has to be asked for the
    same way the call will be made.
    """
    target = verb if playbook is None else {"verb": verb, "playbook": playbook}
    fingerprint = asyncio.run(dispatch.request(help=target))["schema_fingerprint"]
    answer = asyncio.run(
        dispatch.request(ops=[{"op": verb, "args": args, "schema_fingerprint": fingerprint}])
    )
    op = answer["ops"][0]
    assert op["ok"], op
    return op["result"]


@pytest.mark.parametrize("kind", ORCHESTRATION_KINDS)
def test_the_run_keeps_the_server_set_the_submission_resolved(
    sandbox, submit_dir, monkeypatch, kind
):
    """The source config is replaced after submit; the run still gets S1.

    A flow may plan for minutes before it builds its first worker, which is
    exactly the window in which an edit to the ambient file would otherwise
    change the tools the run was submitted with.
    """
    source = submit_dir / ".mcp.json"
    source.write_text(json.dumps({"mcpServers": {"s1": {"command": "one"}}}))

    captured = _capture_popen(monkeypatch)
    handle = jobs.submit(kind, ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    # The submission is assembled and the child is spawned. Now the file moves.
    source.write_text(json.dumps({"mcpServers": {"s2": {"command": "two"}}}))

    child_sees = _child_config(captured["argv"])
    assert child_sees == handle["mcp_config"]
    seen = resolve_spawn_mcp_servers(child_sees, launch_dir=submit_dir)
    assert set(seen.servers) == {"s1"}
    assert handle["mcp_config_source"] == str(source)


@pytest.mark.parametrize("kind", ORCHESTRATION_KINDS)
def test_the_snapshot_lands_in_front_of_the_positional_sentinel(
    sandbox, submit_dir, monkeypatch, kind
):
    """flow and fanout take their instruction as a positional behind `--`.

    An option appended past that sentinel is not an option at all: it arrives as
    two more words of the prompt, and the run starts with no servers while every
    field on the handle says it has them.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"s1": {"command": "one"}}}))

    captured = _capture_popen(monkeypatch)
    jobs.submit(kind, ["-m", "x"], prompt="do the thing", cwd=str(submit_dir))

    argv = captured["argv"]
    assert "--" in argv, argv
    assert argv.index("--mcp-config") < argv.index("--"), argv


@pytest.mark.parametrize("verb", ("flow.submit", "fanout.submit"))
def test_a_caller_who_names_a_config_gets_that_config_and_a_handle_that_says_so(
    sandbox, submit_dir, monkeypatch, verb
):
    """The named file is what the child opens, and what the handle reports.

    Both halves matter. A snapshot generated beside the caller's own choice puts
    two configs on the line, and the one the parser drops is the one the handle
    was naming — so the surface would be describing a file the run never reads.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))
    chosen = submit_dir / "chosen.json"
    chosen.write_text(json.dumps({"mcpServers": {"mine": {"command": "m"}}}))

    captured = _capture_popen(monkeypatch)
    handle = _spawn(
        verb,
        {"query": ["a-model"], "prompt": "hi", "mcp_config": str(chosen), "cwd": str(submit_dir)},
    )

    child_sees = _child_config(captured["argv"])
    assert child_sees == str(chosen)
    seen = resolve_spawn_mcp_servers(child_sees, launch_dir=submit_dir)
    assert set(seen.servers) == {"mine"}
    assert handle["mcp_config"] == str(chosen)
    assert handle["mcp_config_source"] == str(chosen)
    assert not (config.job_dir(handle["run_id"]) / "mcp-servers.json").exists()


@pytest.mark.parametrize("verb", ("flow.submit", "fanout.submit"))
def test_a_caller_who_asks_for_no_servers_is_not_handed_a_snapshot(
    sandbox, submit_dir, monkeypatch, verb
):
    """Asking for none is an answer, and the handle records whose answer it was.

    Resolving one anyway would put a config on the line beside the switch that
    turns configs off, and name it on a handle for a run told to ignore it.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))

    captured = _capture_popen(monkeypatch)
    handle = _spawn(
        verb, {"query": ["a-model"], "prompt": "hi", "no_mcp_config": True, "cwd": str(submit_dir)}
    )

    assert _child_config(captured["argv"]) is None
    assert handle["mcp_config"] is None
    assert handle["mcp_config_source"] is None
    assert handle["mcp_config_reason"] == "mcp_disabled_by_caller"
    assert not (config.job_dir(handle["run_id"]) / "mcp-servers.json").exists()


@pytest.mark.parametrize("verb", ("flow.submit", "fanout.submit"))
def test_the_snapshot_is_reported_when_the_caller_says_nothing(
    sandbox, submit_dir, monkeypatch, verb
):
    """Saying nothing is still the ambient config, snapshotted into the run, and
    the three fields on the handle name it instead of reporting null."""
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))

    captured = _capture_popen(monkeypatch)
    handle = _spawn(verb, {"query": ["a-model"], "prompt": "hi", "cwd": str(submit_dir)})

    snapshot = config.job_dir(handle["run_id"]) / "mcp-servers.json"
    assert _child_config(captured["argv"]) == str(snapshot)
    assert handle["mcp_config"] == str(snapshot)
    assert handle["mcp_config_source"] == str(submit_dir / ".mcp.json")
    assert handle["mcp_config_reason"] is None
    seen = resolve_spawn_mcp_servers(str(snapshot), launch_dir=submit_dir)
    assert set(seen.servers) == {"ambient"}


_PLAYBOOK = """\
name: probe
prompt: hello
"""


@pytest.fixture
def playbook(submit_dir):
    """A project-local playbook the name `probe` resolves to.

    The search starts from the current directory, which `submit_dir` has already
    made the submitting directory, so the playbook and the ambient config are
    found from the same place a real caller would have them.
    """
    books = submit_dir / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "probe.playbook.yaml").write_text(_PLAYBOOK)
    return "probe"


def test_a_play_caller_who_names_a_config_gets_that_config_and_a_handle_that_says_so(
    sandbox, submit_dir, playbook, monkeypatch
):
    """A play is a flow whose plan is written down, and it is spawned as one.

    `play.submit` runs `orchestrate flow` with the playbook named, so it reaches
    the same code that puts a config on the child's line — but by a different job
    kind, so nothing about the flow verb passing says this one does. Held to the
    same contract: the file the caller names is the file the child opens, and the
    handle reports that same file rather than a snapshot beside it.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))
    chosen = submit_dir / "chosen.json"
    chosen.write_text(json.dumps({"mcpServers": {"mine": {"command": "m"}}}))

    captured = _capture_popen(monkeypatch)
    handle = _spawn(
        "play.submit",
        {"playbook": playbook, "mcp_config": str(chosen), "cwd": str(submit_dir)},
        playbook=playbook,
    )

    child_sees = _child_config(captured["argv"])
    assert child_sees == str(chosen)
    seen = resolve_spawn_mcp_servers(child_sees, launch_dir=submit_dir)
    assert set(seen.servers) == {"mine"}
    assert handle["mcp_config"] == str(chosen)
    assert handle["mcp_config_source"] == str(chosen)
    assert not (config.job_dir(handle["run_id"]) / "mcp-servers.json").exists()


def test_the_orchestration_cli_accepts_the_flags_the_surface_renders(sandbox):
    """The child has to parse what the submission puts on its line.

    The surface renders these from the CLI's own parser, so a flag that the
    parser does not declare would be rendered by nobody — and a snapshot the
    engine prepends would make `li o flow` exit on an unrecognised argument
    before the run ever started.
    """
    from lionagi.mcp.projection import build_parser_for

    for path in ("orchestrate flow", "orchestrate fanout"):
        parsed = build_parser_for(path).parse_args(["--mcp-config", "/tmp/x.json", "--", "hi"])
        assert parsed.mcp_config == "/tmp/x.json"
        assert parsed.no_mcp_config is False

        refused = build_parser_for(path).parse_args(["--no-mcp-config", "--", "hi"])
        assert refused.no_mcp_config is True
