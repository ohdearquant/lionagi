# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Verify that ADR-0118's schema citations name the bodies doing the work."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORD = ROOT / "docs/adr/ADR-0118-declared-entity-schema-as-single-authority.md"
DB = "lionagi/state/db.py"


def test_schema_citations_point_to_the_column_sets_and_schema_work() -> None:
    record = RECORD.read_text(encoding="utf-8")
    tree = ast.parse((ROOT / DB).read_text(encoding="utf-8"))
    assignments = {
        target.id: statement.value
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    column_sets = ("SESSION", "INVOCATION", "SHOW", "PLAY", "BRANCH")
    for table in column_sets:
        name = f"_{table}_COLUMNS"
        assert f"`{DB}::{name}`" in record
        value = assignments[name]
        assert isinstance(value, ast.Call) and ast.unparse(value.func) == "frozenset", name

    state_db = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StateDB"
    )
    methods = {
        method.name: ast.unparse(method)
        for method in state_db.body
        if isinstance(method, ast.AsyncFunctionDef)
    }

    def cited_body(name: str) -> str:
        assert f"`{DB}::StateDB.{name}`" in record
        return methods[name]

    apply_schema = cited_body("_apply_schema")
    assert apply_schema.index("await self._apply_schema_changes(conn)") < apply_schema.index(
        "await self._stamp_schema_version(conn)"
    )
    changes = cited_body("_apply_schema_changes")
    assert changes.index("await self._reconcile_columns(conn)") < changes.index(
        "await conn.run_sync(metadata.create_all)"
    )
    reconcile = cited_body("_reconcile_columns")
    assert "inspect(c).has_table(t)" in reconcile
    assert "inspect(c).get_columns(t)" in reconcile
    assert "_log.error(" in reconcile
    assert any(isinstance(node, ast.Raise) for node in ast.walk(ast.parse(reconcile)))
    stamp = cited_body("_stamp_schema_version")
    assert "INSERT INTO schema_meta" in stamp
    assert "{'version': SCHEMA_VERSION}" in stamp

    schedules = cited_body("_rebuild_legacy_schedules_check")
    assert "_schedules_table.to_metadata(" in schedules
    for wrapper in (
        "_drop_legacy_action_kind_check",
        "_drop_legacy_schedules_command_check",
        "_drop_legacy_schedules_trigger_type_check",
    ):
        assert "await self._rebuild_legacy_schedules_check(" in methods[wrapper], wrapper
    assert "_definitions_table.to_metadata(" in cited_body("_drop_legacy_definitions_kind_check")

    rebuild = cited_body("_sqlite_rebuild_table")
    assert rebuild.index("SELECT sql FROM sqlite_master") < rebuild.index(
        "for sql in objects:\n        await conn.exec_driver_sql(sql)"
    )
    assert "await self._sqlite_rebuild_table(" in cited_body("_rebuild_legacy_sessions_table")
