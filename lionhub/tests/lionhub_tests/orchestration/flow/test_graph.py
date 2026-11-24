# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A written graph: what the loader accepts, what the checks refuse, and the run's copy."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from lionhub.orchestration.flow.graph import (
    GRAPH_COPY_FILENAME,
    MAX_GRAPH_BYTES,
    FlowGraphError,
    check_graph,
    graph_flag_refusal,
    load_graph,
    load_graph_record,
    write_graph_copy,
)


def _node(node_id: str, needs: list[str] | None = None, agent: str = "worker", **extra) -> dict:
    return {"id": node_id, "agent": agent, "prompt": f"do {node_id}", "needs": needs or [], **extra}


def _write(path: Path, document) -> str:
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return str(path)


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A project declaring one profile, ``worker``, and no real ``~/.lionagi`` in reach."""
    home = tmp_path / "home"
    (home / ".lionagi" / "agents").mkdir(parents=True)
    root = tmp_path / "project"
    agents = root / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text("---\nmodel: claude_code/sonnet\n---\nWork.\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return root


def test_a_three_node_graph_becomes_a_plan_by_position(tmp_path, project):
    path = _write(
        tmp_path / "g.json",
        {"nodes": [_node("a"), _node("b", ["a"]), _node("c", ["b", "a"], exit_criteria="done")]},
    )
    source = load_graph(path)
    check_graph(source, cwd=project)

    fields, agent_ids, dep_indices = source.plan()
    assert agent_ids == ["a", "b", "c"]
    assert dep_indices == [[], [0], [1, 0]]
    assert fields[2] == {"task": "do c", "assignee": "worker", "exit_criteria": "done"}
    assert source.sha256 == hashlib.sha256(Path(path).read_bytes()).hexdigest()
    assert source.record() == {"sha256": source.sha256, "path": path, "resolved": path}


def test_a_need_named_twice_is_one_dependency(tmp_path, project):
    source = load_graph(
        _write(tmp_path / "g.json", {"nodes": [_node("a"), _node("b", ["a", "a"])]})
    )
    check_graph(source, cwd=project)
    assert source.plan()[2] == [[], [0]]


@pytest.mark.parametrize(
    ("document", "rule", "needle"),
    [
        ({"nodes": []}, "empty", "empty 'nodes' list"),
        ({"nodes": [_node("a"), _node("a")]}, "repeated-id", "'a' is repeated"),
        ({"nodes": [_node("../escape")]}, "unsafe-id", "position 0"),
        ({"nodes": [_node("a", ["ghost"])]}, "unknown-need", "'ghost', which names no node"),
        ({"nodes": [_node("a", ["a"])]}, "self-need", "'a' needs itself"),
        (
            {"nodes": [_node("a", ["c"]), _node("b", ["a"]), _node("c", ["b"])]},
            "cycle",
            "a -> c -> b -> a",
        ),
        ({"nodes": [_node("a", agent="no-such-profile")]}, "unknown-agent", "'no-such-profile'"),
        (
            {"nodes": [_node(f"n{i}") for i in range(201)]},
            "too-many-nodes",
            "201 nodes; a graph may have at most 200",
        ),
    ],
)
def test_each_rule_refuses_and_names_the_node(tmp_path, project, document, rule, needle):
    source = load_graph(_write(tmp_path / "g.json", document))
    with pytest.raises(FlowGraphError) as caught:
        check_graph(source, cwd=project)
    assert caught.value.rule == rule
    assert needle in str(caught.value)


def test_two_hundred_nodes_are_accepted_and_never_truncated(tmp_path, project):
    source = load_graph(
        _write(tmp_path / "g.json", {"nodes": [_node(f"n{i}") for i in range(200)]})
    )
    check_graph(source, cwd=project)
    assert len(source.plan()[1]) == 200


def test_more_nodes_than_max_ops_is_refused(tmp_path, project):
    source = load_graph(_write(tmp_path / "g.json", {"nodes": [_node("a"), _node("b")]}))
    check_graph(source, cwd=project, max_ops=2)
    with pytest.raises(FlowGraphError) as caught:
        check_graph(source, cwd=project, max_ops=1)
    assert caught.value.rule == "max-ops"


def test_a_profile_resolves_from_the_run_directory_not_this_process(tmp_path, project, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    source = load_graph(_write(tmp_path / "g.json", {"nodes": [_node("a")]}))
    check_graph(source, cwd=project)
    with pytest.raises(FlowGraphError, match="resolves to no profile"):
        check_graph(source, cwd=elsewhere)


@pytest.mark.parametrize(
    ("document", "needle"),
    [
        ({"nodes": [_node("a", color="red")]}, "node 'a' has key(s) a node does not take: color"),
        ({"nodes": [_node("a")], "name": "g"}, "top-level key(s) a graph does not take: name"),
        ({"graph": []}, "top-level key(s) a graph does not take: graph"),
        ({}, "has no 'nodes' list"),
        ({"nodes": {}}, "'nodes' must be a list"),
        ({"nodes": ["a"]}, "node at position 0 is not an object"),
        ({"nodes": [{"id": "a", "agent": "worker", "prompt": "p"}]}, "node 'a' has no needs"),
        ({"nodes": [{**_node("a"), "prompt": " "}]}, "'prompt' to be a non-empty string"),
        ({"nodes": [{**_node("a"), "needs": "b"}]}, "'needs' to be a list of ids"),
        ({"nodes": [{**_node("a"), "exit_criteria": 3}]}, "'exit_criteria' to be a string"),
    ],
)
def test_the_shape_is_refused_before_any_rule(tmp_path, document, needle):
    with pytest.raises(FlowGraphError) as caught:
        load_graph(_write(tmp_path / "g.json", document))
    assert caught.value.rule == "schema"
    assert needle in str(caught.value)


@pytest.mark.parametrize(
    ("text", "rule", "needle"),
    [
        ('{"nodes": [], "nodes": []}', "json", "repeats a name inside an object"),
        ('{"nodes": [{"id": NaN}]}', "json", "uses NaN"),
        ("[]", "json", "must hold a JSON object at the top"),
        ("{", "json", "is not valid JSON"),
        ("nodes: []", "json", "is not valid JSON"),
    ],
)
def test_the_file_is_read_under_the_h2_rules(tmp_path, text, rule, needle):
    with pytest.raises(FlowGraphError) as caught:
        load_graph(_write(tmp_path / "g.json", text))
    assert caught.value.rule == rule
    assert needle in str(caught.value)


def test_a_relative_path_a_link_and_an_oversized_file_are_refused(tmp_path, monkeypatch):
    real = _write(tmp_path / "g.json", {"nodes": [_node("a")]})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FlowGraphError) as caught:
        load_graph("g.json")
    assert caught.value.rule == "absolute-path"

    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(FlowGraphError) as caught:
        load_graph(str(link))
    assert caught.value.rule == "no-symlink"

    big = tmp_path / "big.json"
    big.write_text('{"nodes": []' + " " * MAX_GRAPH_BYTES + "}")
    with pytest.raises(FlowGraphError) as caught:
        load_graph(str(big))
    assert caught.value.rule == "size"


def test_a_submit_snapshot_records_the_named_path_not_its_own(tmp_path):
    snapshot = _write(tmp_path / "graph.json", {"nodes": [_node("a")]})
    source = load_graph(snapshot, given_path="/work/g.json", resolved="/real/work/g.json")
    assert source.record()["path"] == "/work/g.json"
    assert source.record()["resolved"] == "/real/work/g.json"


@pytest.mark.parametrize(
    ("options", "flag"),
    [
        ({"playbook": "review"}, "-p/--playbook"),
        ({"file": "/spec.yaml"}, "-f/--file"),
        ({"resume": "run-1"}, "--resume"),
        ({"with_synthesis": True}, "--with-synthesis"),
        ({"with_synthesis": "claude_code/sonnet"}, "--with-synthesis"),
        ({"bare": True}, "--bare"),
        ({"reactive": "all"}, "--reactive all"),
        ({"reactive": "critic"}, "--reactive critic"),
    ],
)
def test_options_a_graph_cannot_take_are_named(options, flag):
    refusal = graph_flag_refusal(**options)
    assert refusal is not None
    assert refusal.startswith(f"--graph cannot be combined with {flag}")


def test_reactive_off_and_no_options_are_accepted():
    assert graph_flag_refusal() is None
    assert graph_flag_refusal(reactive="off", with_synthesis=False, bare=False) is None


def test_the_copy_is_owner_only_and_holds_the_accepted_bytes(tmp_path):
    source = load_graph(_write(tmp_path / "g.json", {"nodes": [_node("a")]}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    copy = write_graph_copy(run_dir, source)

    assert copy == run_dir / GRAPH_COPY_FILENAME
    assert copy.read_bytes() == source.data
    assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600
    # Written twice with the same bytes: kept. A different file there: refused.
    assert write_graph_copy(run_dir, source) == copy
    copy.write_text("{}")
    with pytest.raises(FlowGraphError) as caught:
        write_graph_copy(run_dir, source)
    assert caught.value.rule == "copy"


def test_a_resume_reads_the_copy_and_refuses_one_that_changed(tmp_path):
    original = tmp_path / "g.json"
    source = load_graph(_write(original, {"nodes": [_node("a")]}))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_graph_copy(run_dir, source)
    original.write_text(json.dumps({"nodes": [_node("z")]}))  # an edit after submit

    resumed = load_graph_record(source.record(), run_dir)
    assert resumed.sha256 == source.sha256
    assert [node.id for node in resumed.nodes] == ["a"]
    assert resumed.record() == source.record()

    (run_dir / GRAPH_COPY_FILENAME).write_text(json.dumps({"nodes": [_node("b")]}))
    with pytest.raises(FlowGraphError) as caught:
        load_graph_record(source.record(), run_dir)
    assert caught.value.rule == "digest"

    (run_dir / GRAPH_COPY_FILENAME).unlink()
    with pytest.raises(FlowGraphError) as caught:
        load_graph_record(source.record(), run_dir)
    assert caught.value.rule == "copy"


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"sha256": "0" * 64, "path": "/g.json"},
        {"sha256": "0" * 64, "path": "/g.json", "resolved": "/g.json", "extra": 1},
        {"sha256": "XYZ", "path": "/g.json", "resolved": "/g.json"},
        {"sha256": "0" * 64, "path": "g.json", "resolved": "/g.json"},
    ],
)
def test_a_malformed_record_is_refused(tmp_path, record):
    with pytest.raises(FlowGraphError) as caught:
        load_graph_record(record, tmp_path)
    assert caught.value.rule == "record"
