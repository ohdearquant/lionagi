# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A submit whose child could not be started raises ``SpawnError`` (a
``RuntimeError``) carrying the run_id. Dispatch used to catch only ``OpError``
and the schema-projection errors, so this one escaped and took the whole batch
down with it, including ops beside it that had already succeeded. It is now a
per-op error, so the batch keeps its other results and the caller has the id
whose log holds the cause. See docs/internals/mcp.md
(spawn-failure-per-op-error) for why there is deliberately no startup watch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from lionagi.mcp import dispatch, jobs


def _submit_op(**args: Any) -> dict[str, Any]:
    """An agent.submit op carrying a fingerprint read from the live schema.

    Read rather than written down: a hardcoded one goes stale the next time the
    schema moves, and the op is then refused for the wrong reason — which still
    looks like a failed op to any assertion that only checks ``ok``.
    """
    from lionagi.mcp.verbs import VERBS

    verb = VERBS["agent.submit"]
    schema = dispatch.verb_schema(verb)
    return {
        "op": "agent.submit",
        "schema_fingerprint": dispatch.schema_fingerprint(schema),
        "args": args,
    }


def _batch(*ops: dict[str, Any]) -> dict[str, Any]:
    """Run ops through the surface a caller actually reaches, so the result
    shape under test is the one that goes over the wire."""
    return asyncio.run(dispatch.request(ops=list(ops)))


class _FailingPopen:
    """Stand in for a spawn the platform refuses. Every exception, not an errno
    family — an argument the exec cannot carry raises ValueError with no errno
    anywhere in it, and that is the case that stranded a run."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *a: Any, **kw: Any) -> Any:
        self.calls.append(kw)
        raise self.exc


@pytest.fixture
def spawn_refused(monkeypatch, tmp_path):
    popen = _FailingPopen(OSError(13, "Permission denied"))
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    return popen


def test_a_spawn_failure_raises_spawn_error_carrying_the_run(spawn_refused):
    with pytest.raises(jobs.SpawnError) as exc_info:
        jobs.submit("agent", ["--model", "claude"], prompt="hi", no_mcp_config=True)

    assert exc_info.value.run_id
    assert exc_info.value.record["status"] == "failed"


def test_the_batch_keeps_its_other_results(spawn_refused):
    """The point of the change. Before, one refused spawn in a batch discarded
    every result beside it, including ops that had already completed."""
    result = _batch(
        {"op": "server.info", "args": {}},
        _submit_op(query=["claude", "hi"]),
    )

    ops = result["ops"]
    assert ops[0]["ok"] is True
    assert ops[1]["ok"] is False


def test_the_refusal_names_the_run_whose_log_holds_the_cause(spawn_refused):
    result = _batch(_submit_op(query=["claude", "hi"]))

    op = result["ops"][0]
    assert op["ok"] is False
    assert op["error"]["detail"]["run_id"]


def test_a_refused_spawn_is_unavailable_rather_than_bad_input(spawn_refused):
    """The kind is a closed vocabulary a caller branches on. The arguments were
    already accepted by the schema; what failed is this machine's ability to
    start a process. A caller told its input was wrong will rewrite the request
    and send it again, which is the one response that cannot help here."""
    result = _batch(_submit_op(query=["claude", "hi"]))

    assert result["ops"][0]["error"]["kind"] == "unavailable"


def test_the_error_is_json_serialisable(spawn_refused):
    """It travels over MCP as JSON. An exception object smuggled into detail
    would fail at the transport, after the caller was told it had an answer."""
    result = _batch(_submit_op(query=["claude", "hi"]))

    json.dumps(result)


def test_a_value_error_spawn_failure_is_handled_the_same_way(monkeypatch, tmp_path):
    """The errno families are not the boundary. This is the case that has no
    errno at all and stranded a run reading "running" forever."""
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", _FailingPopen(ValueError("embedded null byte")))

    result = _batch(_submit_op(query=["claude", "hi"]))

    assert result["ops"][0]["ok"] is False
    assert result["ops"][0]["error"]["detail"]["run_id"]


def test_a_working_directory_that_is_not_there_is_the_callers_to_fix(monkeypatch, tmp_path):
    """`unavailable` is the only kind that tells a caller to come back later, and
    a path that was never there will not be there later either. It reached that
    kind because the spawn was the first thing to look at the directory."""
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path, raising=False)

    result = _batch(_submit_op(query=["claude", "hi"], cwd=str(tmp_path / "not-a-directory")))

    error = result["ops"][0]["error"]
    assert error["kind"] == "invalid_input"
    assert "not a directory" in error["message"]


def test_no_run_is_recorded_for_a_directory_that_is_not_there(monkeypatch, tmp_path):
    """The refusal happens before anything is written. A run record for a run
    that was never going to start is a row every later reader has to explain."""
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path, raising=False)

    result = _batch(_submit_op(query=["claude", "hi"], cwd=str(tmp_path / "not-a-directory")))

    assert "run_id" not in (result["ops"][0]["error"].get("detail") or {})
    assert list(tmp_path.iterdir()) == []


def test_a_tilde_means_the_same_thing_on_every_verb_that_takes_a_cwd(spawn_refused):
    """One server, one argument name, one meaning. A roster read resolved under
    `~/x` while a submit handed the tilde to a spawn that cannot chdir to it, so
    the same string was a working directory on one verb and a refusal on the
    other."""
    result = _batch(_submit_op(query=["claude", "hi"], cwd="~"))

    # Past the check, into the spawn — which this fixture refuses for its own
    # reasons. What matters is which directory the platform was asked for.
    assert result["ops"][0]["error"]["kind"] == "unavailable"
    assert spawn_refused.calls[-1]["cwd"] == str(Path("~").expanduser())


def test_the_record_says_failed_rather_than_running(spawn_refused):
    """A record that reads as running against a process that never existed is
    the failure this whole area is about. The run_id in the error is only useful
    if the record it names tells the truth."""
    result = _batch(_submit_op(query=["claude", "hi"]))
    run_id = result["ops"][0]["error"]["detail"]["run_id"]

    st = jobs.status(run_id)
    assert st["terminal"] is True
    assert st["outcome"] == "failed"
