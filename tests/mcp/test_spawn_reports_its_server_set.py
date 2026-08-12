# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A run names the servers LionAGI declared, without opening the snapshot.

The set was already in hand when the snapshot was written and was simply not
recorded, so a caller asking what LionAGI declared had to open the record on disk.
These tests pin that value in each way the question can be answered, and keep
"declared none" distinct from "LionAGI did not inspect the caller's config." A
provider may merge its own configuration, so none of these assertions claim to
describe the effective tool surface.

Popen is doubled so no real `li` process is spawned.
"""

from __future__ import annotations

import json

import pytest

from lionagi.mcp import config, jobs


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
    """A submitting directory with no MCP config above it.

    The search walks to the filesystem root, so an ancestor's real .mcp.json
    would otherwise decide these tests.
    """
    d = tmp_path / "submit"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


@pytest.fixture
def no_spawn(monkeypatch):
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc())


def test_a_resolved_set_is_reported_by_name_and_sorted(sandbox, submit_dir, no_spawn):
    """The names the run resolved, on the handle, without opening the snapshot."""
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    assert handle["declared_mcp_servers"] == ["khive", "lion"]
    # The snapshot on disk is the child's copy and has to agree with what the
    # handle claims; a handle describing a set the child was not given would be
    # worse than no handle at all.
    written = json.loads((sandbox / "jobs" / handle["run_id"] / "mcp-servers.json").read_text())
    assert sorted(written["mcpServers"]) == handle["declared_mcp_servers"]


def test_submit_handle_names_the_snapshot_set_as_declared_not_effective(
    sandbox, submit_dir, no_spawn
):
    """The handle must not imply this is the provider's effective server set."""
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    assert handle["declared_mcp_servers"] == ["khive", "lion"]
    assert handle["mcp_config_servers"] == handle["declared_mcp_servers"]


def test_status_carries_the_same_answer_as_the_handle(sandbox, submit_dir, no_spawn):
    """The point of the change: a finished run answers without a filesystem dig.

    The submit handle is a one-shot. Anyone investigating afterwards -- which is
    when the question actually gets asked -- reaches for status.
    """
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))
    st = jobs.status(handle["run_id"])

    for field in (
        "mcp_config",
        "mcp_config_source",
        "mcp_config_reason",
        "declared_mcp_servers",
        "mcp_config_servers",
    ):
        assert st[field] == handle[field], field
    assert st["declared_mcp_servers"] == ["khive", "lion"]


def test_status_backfills_declared_name_from_the_deprecated_record_key(
    sandbox, submit_dir, no_spawn
):
    """Existing records gain the truthful name without a migration."""
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )
    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))
    record_path = sandbox / "jobs" / handle["run_id"] / "job.json"
    record = json.loads(record_path.read_text())
    assert record.pop("declared_mcp_servers") == ["khive", "lion"]
    record_path.write_text(json.dumps(record))

    st = jobs.status(handle["run_id"])

    assert st["declared_mcp_servers"] == ["khive", "lion"]
    assert st["mcp_config_servers"] == st["declared_mcp_servers"]


def test_new_records_persist_the_declared_name_as_canonical(sandbox, submit_dir, no_spawn):
    """A restarted server need not recover the new contract from an alias."""
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )
    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    record = json.loads((sandbox / "jobs" / handle["run_id"] / "job.json").read_text())

    assert record["declared_mcp_servers"] == ["khive", "lion"]
    assert record["mcp_config_servers"] == record["declared_mcp_servers"]


def test_caller_asking_for_no_servers_reports_an_empty_set_not_null(sandbox, submit_dir, no_spawn):
    """`[]` is an answer. The caller settled it and the answer was none."""
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"lion": {"command": "li"}}}))

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir), no_mcp_config=True)

    assert handle["declared_mcp_servers"] == []
    assert handle["mcp_config_servers"] == handle["declared_mcp_servers"]
    assert handle["mcp_config_reason"] == "mcp_disabled_by_caller"
    assert jobs.status(handle["run_id"])["declared_mcp_servers"] == []


def test_a_caller_named_config_reports_null_because_this_run_never_read_it(
    sandbox, submit_dir, no_spawn, tmp_path
):
    """Null is the other answer: no set was resolved, so none can be named.

    The caller's file belongs to the caller and this run does not open it, so
    reporting names from it would be a claim about a file that may have changed
    by the time the child reads it.
    """
    theirs = tmp_path / "theirs.json"
    theirs.write_text(json.dumps({"mcpServers": {"whatever": {"command": "w"}}}))

    handle = jobs.submit(
        "agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir), mcp_config=str(theirs)
    )

    assert handle["declared_mcp_servers"] is None
    assert handle["mcp_config_reason"] == "mcp_config_named_by_caller"


def test_none_and_cannot_say_are_not_the_same_value(sandbox, submit_dir, no_spawn, tmp_path):
    """The distinction the whole field exists for, asserted directly.

    LionAGI made an empty declaration for one run and did not inspect the
    caller-owned config for the other. Those are not the same fact.
    """
    theirs = tmp_path / "theirs.json"
    theirs.write_text(json.dumps({"mcpServers": {"whatever": {"command": "w"}}}))

    settled = jobs.submit(
        "agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir), no_mcp_config=True
    )
    unasked = jobs.submit(
        "agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir), mcp_config=str(theirs)
    )

    assert settled["declared_mcp_servers"] == []
    assert unasked["declared_mcp_servers"] is None
    assert settled["declared_mcp_servers"] != unasked["declared_mcp_servers"]


def test_no_config_found_reports_null_and_says_where_it_looked(sandbox, submit_dir, no_spawn):
    """Nothing to resolve is "cannot say", and the reason names the search."""
    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    assert handle["declared_mcp_servers"] is None
    assert handle["mcp_config_reason"].startswith("no_mcp_config_found_at_or_above:")


def test_a_config_declaring_no_servers_reports_an_empty_set(sandbox, submit_dir, no_spawn):
    """The second way an answer of none is reached, and the one that was wrong.

    A config was found and it declares no servers. The question was asked and
    answered, so it reports `[]`. The resolver returns a null server map both for
    this and for finding no config at all, and only its reason tells them apart,
    so reading the map alone reported "cannot say" about a file that had said so
    explicitly.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    assert handle["declared_mcp_servers"] == []
    assert handle["mcp_config_reason"] == "mcp_config_declares_no_servers"
    # The file that answered is named, so a reader is not sent looking for a
    # config that was never consulted.
    assert handle["mcp_config_source"] == str(submit_dir / ".mcp.json")
    assert jobs.status(handle["run_id"])["declared_mcp_servers"] == []


def test_declaring_none_and_finding_no_config_are_different_answers(
    sandbox, submit_dir, no_spawn, tmp_path
):
    """The distinction the fix restores, asserted against its neighbour.

    Both come back from the resolver with a null server map. One of them is a
    settled answer of none and the other is genuinely nothing to report, and a
    reader must be able to tell which without knowing the resolver's internals.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    declared_none = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    (submit_dir / ".mcp.json").unlink()
    nothing_found = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    assert declared_none["declared_mcp_servers"] == []
    assert nothing_found["declared_mcp_servers"] is None


@pytest.mark.parametrize("kind", ["flow", "fanout"])
def test_other_kinds_report_their_server_set_too(sandbox, submit_dir, no_spawn, kind):
    """Resolution is not agent-only, so neither is the reporting.

    One shared submit path serves every kind. A test that only ever calls `agent`
    establishes the field for one caller and says nothing about the others.
    """
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )

    handle = jobs.submit(kind, ["-m", "x"], prompt="do a thing", cwd=str(submit_dir))

    assert handle["declared_mcp_servers"] == ["khive", "lion"]
    assert jobs.status(handle["run_id"])["declared_mcp_servers"] == ["khive", "lion"]


def test_a_record_written_before_the_field_existed_reads_as_cannot_say(
    sandbox, submit_dir, no_spawn
):
    """Old records answer null rather than crashing or claiming an empty set.

    A pre-existing record has no key for this. Absent has to mean "cannot say",
    which is the truth about it -- reading it as `[]` would turn every run that
    predates the field into a run that reported having no servers.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"lion": {"command": "li"}}}))
    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))
    run_id = handle["run_id"]

    record_path = sandbox / "jobs" / run_id / "job.json"
    record = json.loads(record_path.read_text())
    # Positive control: the key is there to begin with, so its removal is what
    # the assertion below is reading and not a path that never had it.
    assert record.pop("declared_mcp_servers") == ["lion"]
    assert record.pop("mcp_config_servers") == ["lion"]
    record_path.write_text(json.dumps(record))

    assert jobs.status(run_id)["declared_mcp_servers"] is None
    assert jobs.status(run_id)["mcp_config_servers"] is None
