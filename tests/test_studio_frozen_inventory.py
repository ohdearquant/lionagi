"""The old ``lionagi.studio`` import paths, frozen, each with the module it must resolve to.

ADR-0136 Amendment 5 G4. The inventory is every ``lionagi.studio`` module path at the
amendment's base (development-line commit ``c83e53d7``, 72 module files), every one added
before the move (none: between that base and the move, 25 module files left for the hub and
none was added), and every Studio row of ``lionagi._moved``. The moved-module tests in
``tests/test_moved_modules.py`` derive their old paths from the packages as they stand, so a
module lost in a later change, or a longer row forgotten, would leave both sides of their
comparison at once. This list does not follow the packages: a path leaves it only by an edit
here, in the change that removes the rows at 2.0.

Each old path resolves to its destination, in ``lionhub`` or in ``lionagi_studio``, as the same
module object. The three parent packages G2 keeps in core are the only exceptions: they import
as the kept packages from their own files.
"""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path

import pytest

from lionagi import _moved

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]

KEPT_PARENTS = ("lionagi.studio", "lionagi.studio.operator", "lionagi.studio.services")

FROZEN_STUDIO_PATHS: dict[str, str] = {
    "lionagi.studio.__main__": "lionagi_studio.__main__",
    "lionagi.studio._host": "lionhub.serve.daemon._host",
    "lionagi.studio._timezone_rules": "lionhub.orchestration.schedule._timezone_rules",
    "lionagi.studio._traceback_dump": "lionhub.serve.daemon._traceback_dump",
    "lionagi.studio.app": "lionagi_studio.app",
    "lionagi.studio.cli": "lionagi_studio.cli",
    "lionagi.studio.config": "lionagi_studio.config",
    "lionagi.studio.daemon_log": "lionhub.serve.daemon.daemon_log",
    "lionagi.studio.launch_access": "lionhub.serve.daemon.launch_access",
    "lionagi.studio.operator._common": "lionagi_studio.operator._common",
    "lionagi.studio.operator.application_mcp": "lionagi_studio.operator.application_mcp",
    "lionagi.studio.operator.cancel_run": "lionagi_studio.operator.cancel_run",
    "lionagi.studio.operator.catalog": "lionagi_studio.operator.catalog",
    "lionagi.studio.operator.conversation_metadata": (
        "lionagi_studio.operator.conversation_metadata"
    ),
    "lionagi.studio.operator.coordinator": "lionagi_studio.operator.coordinator",
    "lionagi.studio.operator.engine": "lionagi_studio.operator.engine",
    "lionagi.studio.operator.permission_mcp": "lionagi_studio.operator.permission_mcp",
    "lionagi.studio.operator.redact": "lionhub.serve.daemon.services.redact",
    "lionagi.studio.operator.rename_session": "lionagi_studio.operator.rename_session",
    "lionagi.studio.operator.resume_run": "lionagi_studio.operator.resume_run",
    "lionagi.studio.operator.run_control": "lionagi_studio.operator.run_control",
    "lionagi.studio.operator.run_detail": "lionagi_studio.operator.run_detail",
    "lionagi.studio.operator.run_findings": "lionagi_studio.operator.run_findings",
    "lionagi.studio.operator.run_progress": "lionagi_studio.operator.run_progress",
    "lionagi.studio.operator.store": "lionagi_studio.operator.store",
    "lionagi.studio.operator.types": "lionagi_studio.operator.types",
    "lionagi.studio.reconnect": "lionhub.serve.daemon.reconnect",
    "lionagi.studio.registry": "lionagi_studio.registry",
    "lionagi.studio.scheduler": "lionhub.orchestration.scheduler",
    "lionagi.studio.services._db": "lionhub.orchestration.state_store",
    "lionagi.studio.services._io": "lionhub.serve.daemon.services._io",
    "lionagi.studio.services._path_safety": "lionhub.serve.daemon.services._path_safety",
    "lionagi.studio.services._schedule_validation": "lionhub.orchestration.schedule.validation",
    "lionagi.studio.services._session_tail": "lionhub.serve.daemon.services._session_tail",
    "lionagi.studio.services._sse": "lionhub.serve.daemon.services._sse",
    "lionagi.studio.services.admin": "lionhub.serve.daemon.services.admin",
    "lionagi.studio.services.agents": "lionagi_studio.services.agents",
    "lionagi.studio.services.approvals": "lionagi_studio.services.approvals",
    "lionagi.studio.services.artifact_verification": (
        "lionhub.serve.daemon.services.artifact_verification"
    ),
    "lionagi.studio.services.attention": "lionagi_studio.services.attention",
    "lionagi.studio.services.casts": "lionagi_studio.services.casts",
    "lionagi.studio.services.db_maintenance": "lionhub.orchestration.scheduler.db_maintenance",
    "lionagi.studio.services.definitions": "lionagi_studio.services.definitions",
    "lionagi.studio.services.engine_defs": "lionagi_studio.services.engine_defs",
    "lionagi.studio.services.engine_runs": "lionhub.serve.daemon.services.engine_runs",
    "lionagi.studio.services.hooks_library": "lionagi_studio.services.hooks_library",
    "lionagi.studio.services.identity": "lionhub.serve.daemon.services.identity",
    "lionagi.studio.services.invocations": "lionhub.serve.daemon.services.invocations",
    "lionagi.studio.services.launches": "lionhub.serve.daemon.services.launches",
    "lionagi.studio.services.leo": "lionagi_studio.services.leo",
    "lionagi.studio.services.lifecycle": "lionhub.orchestration.scheduler.lifecycle",
    "lionagi.studio.services.mcp_servers": "lionagi_studio.services.mcp_servers",
    "lionagi.studio.services.operator": "lionagi_studio.services.operator",
    "lionagi.studio.services.operator_hooks": "lionagi_studio.services.operator_hooks",
    "lionagi.studio.services.pairing": "lionhub.serve.daemon.services.pairing",
    "lionagi.studio.services.playbooks": "lionagi_studio.services.playbooks",
    "lionagi.studio.services.plugins": "lionagi_studio.services.plugins",
    "lionagi.studio.services.project_lookup": "lionhub.orchestration.project_store",
    "lionagi.studio.services.projects": "lionagi_studio.services.projects",
    "lionagi.studio.services.redaction": "lionagi_studio.services.redaction",
    "lionagi.studio.services.retention_archive": (
        "lionhub.orchestration.scheduler.retention_archive"
    ),
    "lionagi.studio.services.run_resume": "lionhub.serve.daemon.services.run_resume",
    "lionagi.studio.services.run_resume_worker": "lionhub.serve.daemon.services.run_resume_worker",
    "lionagi.studio.services.run_tags": "lionagi_studio.services.run_tags",
    "lionagi.studio.services.run_view": "lionhub.serve.daemon.services.run_view",
    "lionagi.studio.services.runs": "lionhub.serve.daemon.services.runs",
    "lionagi.studio.services.schedule_action_validation": (
        "lionhub.orchestration.schedule.action_validation"
    ),
    "lionagi.studio.services.schedule_declaration": "lionhub.orchestration.schedule.declaration",
    "lionagi.studio.services.schedule_execution_root": (
        "lionhub.orchestration.schedule.execution_root"
    ),
    "lionagi.studio.services.schedule_export": "lionhub.orchestration.schedule.export",
    "lionagi.studio.services.schedule_timing": "lionhub.orchestration.schedule.timing",
    "lionagi.studio.services.scheduler_state": "lionhub.orchestration.scheduler.scheduler_state",
    "lionagi.studio.services.schedules": "lionhub.serve.daemon.services.schedules",
    "lionagi.studio.services.sessions": "lionhub.serve.daemon.services.sessions",
    "lionagi.studio.services.shows": "lionagi_studio.services.shows",
    "lionagi.studio.services.signals": "lionhub.serve.daemon.services.signals",
    "lionagi.studio.services.skills": "lionagi_studio.services.skills",
    "lionagi.studio.services.stats": "lionagi_studio.services.stats",
    "lionagi.studio.services.task_applications": "lionhub.serve.daemon.services.task_applications",
    "lionagi.studio.services.teams": "lionagi_studio.services.teams",
    "lionagi.studio.services.terminal_reconciliation": (
        "lionhub.orchestration.scheduler.terminal_reconciliation"
    ),
    "lionagi.studio.services.workflow_compile": "lionagi_studio.services.workflow_compile",
    "lionagi.studio.services.workflow_defs": "lionagi_studio.services.workflow_defs",
    "lionagi.studio.services.workflow_run": "lionagi_studio.services.workflow_run",
}


def _import_quietly(name: str):
    # An old name warns once on first import; the moved-module tests own that warning.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return importlib.import_module(name)


def test_the_inventory_holds_the_base_paths_and_the_studio_rows():
    assert len(FROZEN_STUDIO_PATHS) + len(KEPT_PARENTS) == 87
    assert not set(KEPT_PARENTS) & set(FROZEN_STUDIO_PATHS)
    for old, new in FROZEN_STUDIO_PATHS.items():
        assert old.startswith("lionagi.studio."), old
        assert new.startswith(("lionhub.", "lionagi_studio.")), (old, new)


def test_every_studio_row_is_in_the_inventory():
    """A Studio row added later joins the inventory; one removed from the table stays in it."""
    inventoried = set(FROZEN_STUDIO_PATHS) | set(KEPT_PARENTS)
    rows = {
        row.old
        for row in _moved.MOVED
        if row.old == "lionagi.studio" or row.old.startswith("lionagi.studio.")
    }
    assert rows, "no Studio row read from lionagi._moved.MOVED"
    assert sorted(rows - inventoried) == []


@pytest.mark.parametrize(("old", "new"), sorted(FROZEN_STUDIO_PATHS.items()))
def test_each_old_studio_path_resolves_to_its_declared_module(old, new):
    assert _moved.resolve(old) == new
    assert _import_quietly(old) is _import_quietly(new)


@pytest.mark.parametrize("old", KEPT_PARENTS)
def test_each_kept_parent_imports_as_the_kept_package(old):
    module = _import_quietly(old)
    expected = ROOT / Path(*old.split(".")) / "__init__.py"
    assert Path(module.__file__).resolve() == expected.resolve()
    assert module is not _import_quietly(_moved.resolve(old))
