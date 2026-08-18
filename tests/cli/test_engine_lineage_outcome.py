from __future__ import annotations

import argparse
import functools
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


def _parse(argv: list[str]) -> argparse.Namespace:
    from lionagi.cli.engine import add_engine_subparser

    parser = argparse.ArgumentParser(prog="li")
    add_engine_subparser(parser.add_subparsers(dest="command"))
    return parser.parse_args(argv)


def test_engine_invocation_defaults_from_environment_and_explicit_flag_wins(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LIONAGI_INVOCATION_ID", "inv-from-env")
    inherited = _parse(["engine", "run", "research", "topic"])
    explicit = _parse(["engine", "run", "research", "topic", "--invocation", "inv-explicit"])

    assert inherited.invocation_id == "inv-from-env"
    assert explicit.invocation_id == "inv-explicit"


async def test_cli_engine_persists_canonical_lineage_and_bounded_outcome(
    monkeypatch, tmp_path, capsys
) -> None:
    import lionagi.cli.engine as engine_mod
    import lionagi.state.db as db_mod
    from lionagi.engines.engine import EngineResult
    from lionagi.state.db import StateDB

    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        await db.create_invocation({"id": "inv-engine", "skill": "engine", "started_at": 90.0})

    monkeypatch.setattr(db_mod, "StateDB", functools.partial(StateDB, db_path))
    monkeypatch.setattr(engine_mod, "progress", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine_mod, "warn", lambda *_a, **_kw: None)
    fixed = SimpleNamespace(hex="engine-run-fixed")
    monkeypatch.setattr(engine_mod.uuid, "uuid4", lambda: fixed)

    sentinel = "sk-input-must-not-enter-outcome-1234567890"
    run_handle = SimpleNamespace(run_id="span-fixed")
    result = EngineResult(
        "result text that may echo " + sentinel,
        events_by_type=lambda _kind: [],
        skipped=["stage-a"],
        degraded=True,
        degrade_reason="budget",
        run=run_handle,
    )

    class FakeEngine:
        _total_agent_failure = False
        _emission_failures: list[str] = []
        model = None
        models: dict[str, str] = {}

        async def run(self, _spec, **_kwargs):
            return result

    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda *_a: FakeEngine)
    args = argparse.Namespace(
        command="engine",
        engine_command="run",
        kind="research",
        spec=sentinel,
        test_cmd=None,
        export_dir=None,
        model=None,
        effort=None,
        max_depth=None,
        max_agents=None,
        session_id=None,
        invocation_id="inv-engine",
        no_persist=False,
        dedup_repo=None,
        dedup_cwd=None,
    )

    assert await engine_mod._do_engine_run(args) == 0
    capsys.readouterr()

    async with StateDB(db_path) as db:
        session = await db.get_session("engine-run-fixed")
        row = await db.get_engine_run("engine-run-fixed")
        children = await db.list_sessions_for_invocation("inv-engine")

    assert session is not None
    assert session["invocation_kind"] == "engine"
    assert session["invocation_id"] == "inv-engine"
    assert [child["id"] for child in children] == ["engine-run-fixed"]
    assert row is not None
    assert row["signal_session_id"] == "engine-run-fixed"
    assert row["parent_session_id"] is None
    assert row["invocation_id"] == "inv-engine"
    assert row["error"] is None
    assert row["outcome_json"]["version"] == 1
    assert row["outcome_json"]["degraded"] is True
    assert row["outcome_json"]["degrade_reason"] == "budget"
    assert row["outcome_json"]["skipped"] == ["stage-a"]
    encoded = json.dumps(row["outcome_json"])
    assert sentinel not in encoded
    assert len(encoded.encode()) <= engine_mod.ENGINE_OUTCOME_BYTE_CAP


async def test_workflow_engine_nodes_return_distinct_span_ids(monkeypatch) -> None:
    import lionagi.cli.engine as engine_mod
    from lionagi.engines.engine import EngineResult
    from lionagi.studio.services.workflow_compile import make_engine_operation

    counter = 0

    class FakeEngine:
        _total_agent_failure = False

        def __init__(self, **_kwargs):
            nonlocal counter
            counter += 1
            self.span_id = f"engine-span-{counter}"

        async def run(self, _spec, **_kwargs):
            return EngineResult(
                "ok",
                events_by_type=lambda _kind: [],
                skipped=[],
                degraded=False,
                run=SimpleNamespace(run_id=self.span_id),
            )

    monkeypatch.setattr(engine_mod, "_import_engine_class", lambda *_a: FakeEngine)
    operation = make_engine_operation(MagicMock())

    first = await operation(context={"input": "one"}, engine_kind="research")
    second = await operation(context={"input": "two"}, engine_kind="research")

    assert first["engine_span_id"] == "engine-span-1"
    assert second["engine_span_id"] == "engine-span-2"
    assert first["engine_span_id"] != second["engine_span_id"]
