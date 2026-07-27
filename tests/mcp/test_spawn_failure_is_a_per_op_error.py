# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A submit whose child could not be started raises ``SpawnError``, which is a
``RuntimeError``. Dispatch caught ``OpError`` and the schema-projection errors
and nothing else, so this one escaped and took the entire batch down with it —
including the ops beside it that had already succeeded.

That was reachable before any of this: ``_record_spawn_failure`` raises it for
every ``Popen`` failure, deliberately for every exception rather than an errno
family. A caller who batched a submit alongside anything else lost the lot, and
the response said nothing about which run failed or why.

It is now a per-op error carrying the run_id, so the batch keeps its other
results and the caller has the id whose log holds the cause.

WHY THERE IS NO STARTUP WATCH HERE

A first version of this fix watched the freshly spawned child for a few seconds
and converted an immediate non-zero exit into a refused submit. It was removed
before merge, on a measurement rather than an opinion.

Ten real children spawned to die on their own arguments (`li agent -a
<nonexistent-profile>`), timed end to end on a machine at load average 76:

    2.08  2.43  2.53  2.55  2.81  3.35  3.64  3.67  3.82  5.52   seconds

A fixed window has to sit above the slowest of those to catch the class it
exists for, and every healthy submit pays whatever that window is. At three
seconds it would have missed four of these ten while taxing every good submit;
at six it would catch them and tax every good submit twice as much. The
distribution is a property of the machine's load, not of the defect, so no
constant is right on both counts.

The thing it was reaching for already exists on the read side: ``status()``
reports ``possibly_orphaned`` for a process that is gone with no end recorded,
and returns ``log_tail`` in the same response, which is where the cause has been
the whole time. A caller probing once after submit learns this without anyone
paying for a window.
"""

from __future__ import annotations

import asyncio
import json
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

    def __call__(self, *a: Any, **kw: Any) -> Any:
        raise self.exc


@pytest.fixture
def spawn_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs.config, "JOBS_DIR", tmp_path, raising=False)
    monkeypatch.setattr(jobs.subprocess, "Popen", _FailingPopen(OSError(13, "Permission denied")))


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
    assert op["error"]["kind"] == "invalid_input"
    assert op["error"]["detail"]["run_id"]


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


def test_the_record_says_failed_rather_than_running(spawn_refused):
    """A record that reads as running against a process that never existed is
    the failure this whole area is about. The run_id in the error is only useful
    if the record it names tells the truth."""
    result = _batch(_submit_op(query=["claude", "hi"]))
    run_id = result["ops"][0]["error"]["detail"]["run_id"]

    st = jobs.status(run_id)
    assert st["terminal"] is True
    assert st["outcome"] == "failed"
