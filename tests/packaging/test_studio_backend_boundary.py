# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Local schedule validation and export with the Studio application's imports refused.

ADR-0138 names this test as the proof that they need no backend. Every command runs
as its own process through the command line, with a ``sitecustomize`` on its path that
refuses the application's modules and records, as the process exits, what it refused
and loaded. The commands are ``schedule validate`` and ``schedule export``, human and
machine, over valid and invalid cron, interval, command, flow and playbook
declarations, then over populated declaration-managed and legacy rows, including an
export that is partial because one legacy row cannot convert. Validation must leave
no file behind, a database included, and export must leave the rows as they were.
The Studio URL points at a closed port, so a command that needed the daemon fails.

It has two arms.

- Source arm, the default: the checkout's own packages, run as
  ``python -m lionhub.serve.cli``.
- Installed arm: when ``LIONAGI_STUDIO_WHEEL`` names a built wheel and
  ``LIONAGI_STUDIO_WHEEL_PYTHON`` the interpreter of a new environment that wheel was
  installed into with the ``studio`` extra, the commands run through that
  environment's ``li`` console script, from a directory outside the checkout, with no
  checkout path on ``sys.path``. ``lionagi`` and ``lionhub`` must load from that
  environment, and every package file on disk must hash to the digest the wheel's
  RECORD gives it. CI's ``studio-wheel`` job runs it.

The 1.x wheel bundles the application, so both arms refuse its imports with a
meta-path blocker rather than rely on its absence. The 2.0 arm, with the application
physically absent, remains a release check.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WHEEL_ENV = "LIONAGI_STUDIO_WHEEL"
WHEEL_PYTHON_ENV = "LIONAGI_STUDIO_WHEEL_PYTHON"
# The top-level packages the wheel ships (pyproject.toml, tool.hatch.build.targets.wheel).
WHEEL_PACKAGES = ("lionagi", "lionhub", "lionagi_studio")
# Nothing listens on the discard port, so a command that called the daemon would fail.
CLOSED_STUDIO_URL = "http://127.0.0.1:9"
REPORTS_ENV = "LIONAGI_BOUNDARY_REPORTS"

GUARD = """
import atexit, importlib.abc, json, os, sys

_BLOCKED = (
    "apps.studio",
    "lionagi_studio",
    "lionagi.studio.app",
    "lionagi.studio.routes",
    "lionhub.orchestration.scheduler.routes",
)
_refused = []


def _matches(name, prefixes):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


class _RefuseApplication(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _matches(fullname, _BLOCKED):
            _refused.append(fullname)
            raise ImportError("blocked application import: " + fullname)
        return None


sys.meta_path.insert(0, _RefuseApplication())


def _report():
    origins = {}
    for name in ("lionagi", "lionhub"):
        module = sys.modules.get(name)
        if module is not None:
            origins[name] = getattr(module, "__file__", None)
    record = {
        "refused": _refused,
        "loaded": sorted(n for n in sys.modules if _matches(n, _BLOCKED + ("fastapi",))),
        "origins": origins,
        "path": sys.path,
        "prefix": sys.prefix,
    }
    target = os.path.join(os.environ["LIONAGI_BOUNDARY_REPORTS"], str(os.getpid()) + ".json")
    with open(target, "w") as stream:
        json.dump(record, stream)


atexit.register(_report)
"""

CONTROL = """
try:
    import apps.studio
except ModuleNotFoundError as exc:
    print("APPS missing:" + str(exc.name))
except ImportError:
    print("APPS refused")
else:
    print("APPS imported")
try:
    import lionagi_studio
except ImportError:
    print("STUDIO refused")
else:
    print("STUDIO imported")
"""

ROWS = """
import asyncio, json, sys
from pathlib import Path

from lionagi.state.db import StateDB


async def main(mode):
    async with StateDB() as db:
        if mode == "seed":
            from lionhub.orchestration.schedule.declaration import (
                apply_schedule_set,
                parse_schedule_set,
            )

            manifest = Path(sys.argv[2])
            doc = parse_schedule_set(manifest.read_text(), source=str(manifest))
            result = await apply_schedule_set(db, doc, manifest.parent)
            plan = [(e.qualified_name, e.action, e.detail) for e in result.plan]
            assert all(action == "CREATE" for _, action, _ in plan), plan
            for row in json.loads(sys.argv[3]):
                await db.create_schedule(row)
        rows = await db.list_schedules(limit=1_000_000)
        print(json.dumps(sorted(rows, key=lambda r: r["id"]), sort_keys=True, default=str))


asyncio.run(main(sys.argv[1]))
"""

DISK = """
import base64, hashlib, json
from importlib import metadata
from pathlib import Path

packages = ("lionagi", "lionhub", "lionagi_studio")
dist = metadata.distribution("lionagi")
on_disk = {}
for entry in dist.files or ():
    if entry.hash is not None and str(entry).split("/", 1)[0] in packages:
        data = Path(entry.locate()).read_bytes()
        digest = hashlib.sha256(data).digest()
        on_disk[str(entry)] = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
site = Path(dist.locate_file("")).resolve()
unlisted = sorted(
    path.relative_to(site).as_posix()
    for top in packages
    for path in (site / top).rglob("*.py")
    if path.relative_to(site).as_posix() not in on_disk
)
print(json.dumps({"on_disk": on_disk, "unlisted": unlisted, "site": str(site)}))
"""

HEADER = """\
apiVersion: lionagi.io/v1alpha1
kind: ScheduleSet
metadata:
  name: {name}
  project: boundary
schedules:
"""
CRON = '      cron:\n        expression: "0 2 * * *"\n        timezone: America/New_York\n'
EVERY = "      every: 1h\n"
AGENT = "      kind: agent\n      profile: reviewer\n      prompt: review the latest changes\n"
# Each kind once valid and once refused at resolution, with the refusal's words.
DECLARATIONS = {
    "cron": {
        "valid": (CRON, AGENT),
        "invalid": (CRON.replace('"0 2', '"61 2'), AGENT),
        "trigger": "cron",
        "target": "agent",
        "refusal": "trigger.cron.expression is invalid",
    },
    "interval": {
        "valid": (EVERY, AGENT),
        "invalid": ("      every: soon\n", AGENT),
        "trigger": "every",
        "target": "agent",
        "refusal": "trigger.every must be a strict positive duration",
    },
    "command": {
        "valid": (EVERY, '      kind: command\n      executable: "true"\n'),
        "invalid": (EVERY, "      kind: command\n      executable: bin/true\n"),
        "trigger": "every",
        "target": "command",
        "refusal": "must not contain path separators",
    },
    "flow": {
        "valid": (EVERY, "      kind: flow\n      file: flows/nightly.yaml\n"),
        "invalid": (EVERY, "      kind: flow\n      file: flows/oversized.yaml\n"),
        "trigger": "every",
        "target": "flow",
        "refusal": "failed validation",
    },
    "playbook": {
        "valid": (EVERY, "      kind: playbook\n      name: health-audit\n"),
        "invalid": (EVERY, '      kind: playbook\n      name: "two words"\n'),
        "trigger": "every",
        "target": "playbook",
        "refusal": "contains characters not allowed",
    },
}
KINDS = sorted(DECLARATIONS)


def _member(name: str, trigger: str, target: str) -> str:
    return f"  {name}:\n    trigger:\n{trigger}    target:\n{target}    execution:\n      cwd: .\n"


def _legacy_rows(cwd: Path) -> list[dict]:
    """Two rows made before schedule sets existed: one converts, one cannot."""
    row = {
        "trigger_type": "cron",
        "effective_timezone": "America/New_York",
        "action_kind": "agent",
        "action_agent": "reviewer",
        "action_prompt": "check things",
        "action_cwd": str(cwd),
        "enabled": 1,
    }
    return [
        {**row, "id": "legacy-ready", "name": "legacy/nightly", "cron_expr": "0 2 * * *"},
        {
            **row,
            "id": "legacy-blocked",
            "name": "legacy/chained",
            "cron_expr": "0 3 * * *",
            # A chained follow-up has no declaration form, so this row blocks the export.
            "on_success": {"prompt": "notify done"},
        },
    ]


def _installed_arm() -> tuple[Path, Path] | None:
    """The wheel and interpreter of the installed arm, or None for the source arm."""
    wheel = os.environ.get(WHEEL_ENV)
    python = os.environ.get(WHEEL_PYTHON_ENV)
    if not wheel and not python:
        return None
    assert wheel and python, f"the installed arm needs both {WHEEL_ENV} and {WHEEL_PYTHON_ENV}"
    return Path(wheel), Path(python)


def _wheel_record(wheel: Path) -> dict[str, str]:
    """Each package file the wheel's RECORD lists, with its digest."""
    with zipfile.ZipFile(wheel) as archive:
        (record,) = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
        lines = archive.read(record).decode().splitlines()
    digests = {}
    for row in csv.reader(lines):
        path, digest = row[0], row[1]
        if digest and path.split("/", 1)[0] in WHEEL_PACKAGES:
            digests[path] = digest.split("=", 1)[1]
    return digests


def _files(*roots: Path) -> list[str]:
    return sorted(str(path) for root in roots for path in root.rglob("*") if path.is_file())


def _report_lines(text: str) -> tuple[list[list[str]], str]:
    """The (status, qualified name) of each export report line, and its summary line."""
    lines = text.splitlines()
    return [line.split()[:2] for line in lines[:-1]], lines[-1]


class _Runner:
    """Runs one process of the arm under test and reads back what its guard recorded."""

    def __init__(self, *, mode: str, interpreter: str, base: Path, work: Path):
        self.mode = mode
        self.interpreter = interpreter
        self.work = work
        self.home = base / "home"
        self.state = base / "state"
        self.reports = base / "reports"
        guard_dir = base / "guard"
        for directory in (self.home, self.state, self.reports, guard_dir):
            directory.mkdir()
        (guard_dir / "sitecustomize.py").write_text(GUARD)
        if mode == "source":
            self.cli = [interpreter, "-m", "lionhub.serve.cli"]
            path = [guard_dir, ROOT, ROOT / "lionhub" / "src"]
        else:
            console_script = Path(interpreter).parent / "li"
            assert console_script.is_file() and os.access(console_script, os.X_OK), console_script
            self.cli = [str(console_script)]
            path = [guard_dir]
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LIONAGI_") and key != "PYTHONPATH"
        }
        self.env.update(
            HOME=str(self.home),
            LIONAGI_HOME=str(self.state),
            LIONAGI_SCHEDULER_COMMAND_ALLOWLIST="true",
            LIONAGI_STUDIO_URL=CLOSED_STUDIO_URL,
            PYTHONPATH=os.pathsep.join(str(entry) for entry in path),
        )
        self.count = 0

    def run(self, argv: list[str]) -> tuple[int, str, str, dict]:
        self.count += 1
        reports = self.reports / str(self.count)
        reports.mkdir()
        result = subprocess.run(
            argv,
            cwd=self.work,
            env={**self.env, REPORTS_ENV: str(reports)},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        written = list(reports.iterdir())
        # One record per process proves the guard was loaded into it.
        assert len(written) == 1, (argv, written, result.stderr)
        report = json.loads(written[0].read_text())
        if self.mode == "installed":
            root = ROOT.resolve()
            for entry in report["path"]:
                where = Path(entry or self.work).resolve()
                assert where != root and root not in where.parents, (argv, entry)
        return result.returncode, result.stdout, result.stderr, report

    def li(self, *args: str) -> tuple[int, str, str]:
        code, out, err, report = self.run([*self.cli, *args])
        assert report["refused"] == [], (args, report["refused"])
        assert report["loaded"] == [], (args, report["loaded"])
        assert set(report["origins"]) == {"lionagi", "lionhub"}, (args, report["origins"])
        if self.mode == "installed":
            prefix = Path(report["prefix"]).resolve()
            for name, origin in report["origins"].items():
                where = Path(origin).resolve()
                assert prefix in where.parents, (name, where, prefix)
                assert ROOT.resolve() not in where.parents, (name, where)
        return code, out, err

    def python(self, script: str, *args: str) -> tuple[int, str, str, dict]:
        return self.run([self.interpreter, "-c", script, *args])


def test_fresh_wheel_schedule_validate_and_export_without_backend() -> None:
    installed = _installed_arm()
    if installed is None:
        mode = "source"
        interpreter = sys.executable
        scratch = ROOT / ".leg-scratch"
        scratch.mkdir(exist_ok=True)
        base = Path(tempfile.mkdtemp(prefix="studio-backend-boundary-", dir=scratch))
    else:
        mode = "installed"
        wheel, wheel_python = installed
        assert wheel.is_file(), wheel
        interpreter = str(wheel_python)
        base = Path(tempfile.mkdtemp(prefix="studio-backend-boundary-"))
        # Run outside the checkout, so nothing under it is importable from the cwd.
        assert ROOT.resolve() not in base.resolve().parents, base
    try:
        work = base / "work"
        (work / "flows").mkdir(parents=True)
        (work / "flows" / "nightly.yaml").write_text("workers: 2\n")
        (work / "flows" / "oversized.yaml").write_text("workers: 999\n")
        runner = _Runner(mode=mode, interpreter=interpreter, base=base, work=work)
        agents = runner.home / ".lionagi" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(
            "---\nmodel: anthropic/claude-sonnet-5\n---\nReview the latest changes.\n"
        )
        for kind, case in DECLARATIONS.items():
            for verdict in ("valid", "invalid"):
                (work / f"{verdict}-{kind}.yaml").write_text(
                    HEADER.format(name=f"{verdict}-{kind}") + _member(kind, *case[verdict])
                )
        managed = work / "managed.yaml"
        managed.write_text(
            HEADER.format(name="managed")
            + "".join(_member(kind, *DECLARATIONS[kind]["valid"]) for kind in KINDS)
        )

        # The guard refuses the application: the controls that the checks below rely on.
        code, out, err, report = runner.python(CONTROL)
        assert code == 0, err
        if mode == "source":
            assert out.splitlines() == ["APPS refused", "STUDIO refused"], out
            assert report["refused"] == ["apps.studio", "lionagi_studio"], report
        else:
            # The wheel ships no top-level `apps`: the import fails on `apps` itself.
            assert out.splitlines() == ["APPS missing:apps", "STUDIO refused"], out
            assert report["refused"] == ["lionagi_studio"], report

        # Validation: every kind, valid and invalid, human and machine, and no file left.
        untouched = _files(runner.home, runner.state, work)
        for kind, case in DECLARATIONS.items():
            valid = str(work / f"valid-{kind}.yaml")
            code, out, err = runner.li("schedule", "validate", valid)
            assert (code, out) == (0, f"VALID  boundary/valid-{kind}  1 schedule(s)\n"), (
                kind,
                code,
                out,
                err,
            )
            code, out, err = runner.li("--machine", "schedule", "validate", valid)
            data = json.loads(out)["data"]
            assert code == 0 and data["valid"] is True, (kind, code, out, err)
            assert data["owner_key"] == f"boundary/valid-{kind}"
            assert list(data["schedules"]) == [kind], data
            resolved = data["schedules"][kind]
            assert resolved["trigger"]["kind"] == case["trigger"], resolved
            assert resolved["target"]["kind"] == case["target"], resolved

            invalid = str(work / f"invalid-{kind}.yaml")
            code, out, err = runner.li("schedule", "validate", invalid)
            assert (code, out) == (1, ""), (kind, code, out, err)
            assert f"INVALID  boundary/{kind}  " in err and case["refusal"] in err, (kind, err)
            code, out, err = runner.li("--machine", "schedule", "validate", invalid)
            data = json.loads(out)["data"]
            assert code == 0 and data["valid"] is False, (kind, code, out, err)
            assert [error["name"] for error in data["errors"]] == [f"boundary/{kind}"], data
            assert case["refusal"] in data["errors"][0]["message"], data
            assert data["schedules"] == {}, data
        assert _files(runner.home, runner.state, work) == untouched
        assert not any(runner.state.iterdir()), list(runner.state.iterdir())

        # Populated rows: one declaration-managed set and two legacy rows, one unconvertible.
        code, out, err, report = runner.python(
            ROWS, "seed", str(managed), json.dumps(_legacy_rows(work))
        )
        assert code == 0, err
        assert report["refused"] == [] and report["loaded"] == [], report
        seeded = json.loads(out)
        assert (
            sorted((row["managed_by"] or "") for row in seeded) == [""] * 2 + ["declaration"] * 5
        ), [(row["name"], row["managed_by"]) for row in seeded]

        managed_report = base / "managed-report.txt"
        code, managed_yaml, err = runner.li("schedule", "export", "--report", str(managed_report))
        assert code == 0, (code, err)
        document = yaml.safe_load(managed_yaml)
        assert document["metadata"]["project"] == "boundary", document
        assert sorted(document["schedules"]) == KINDS, document
        assert _report_lines(managed_report.read_text()) == (
            [["READY", f"boundary/{kind}"] for kind in KINDS],
            "5 ready, 0 blocked",
        )
        code, out, err = runner.li("--machine", "schedule", "export")
        data = json.loads(out)["data"]
        assert code == 0, (code, err)
        assert data["documents"] == [{"project": "boundary", "yaml": managed_yaml}], data
        assert (data["ready_count"], data["blocked_count"]) == (5, 0), data
        assert [(line["qualified_name"], line["status"]) for line in data["report"]] == [
            (f"boundary/{kind}", "READY") for kind in KINDS
        ], data

        legacy_report = base / "legacy-report.txt"
        code, legacy_yaml, err = runner.li(
            "schedule", "export", "--legacy", "--report", str(legacy_report)
        )
        # A partial export still writes its document and report, and says so by exit code.
        assert code == 2, (code, err)
        document = yaml.safe_load(legacy_yaml)
        assert document["metadata"]["project"] == "legacy", document
        assert list(document["schedules"]) == ["nightly"], document
        assert _report_lines(legacy_report.read_text()) == (
            [["BLOCKED", "legacy/chained"], ["READY", "legacy/nightly"]],
            "1 ready, 1 blocked",
        )
        code, out, err = runner.li("--machine", "schedule", "export", "--legacy")
        data = json.loads(out)["data"]
        assert code == 0, (code, err)
        assert data["documents"] == [{"project": "legacy", "yaml": legacy_yaml}], data
        assert (data["ready_count"], data["blocked_count"]) == (1, 1), data
        assert [(line["qualified_name"], line["status"]) for line in data["report"]] == [
            ("legacy/chained", "BLOCKED"),
            ("legacy/nightly", "READY"),
        ], data

        # Export reads the rows and leaves every column of every row as it was.
        code, out, err, report = runner.python(ROWS, "read")
        assert code == 0, err
        assert json.loads(out) == seeded

        if installed is not None:
            # Every package file on disk is the one in the wheel this run was handed, not
            # an earlier build or a release of the same version.
            code, out, err, report = runner.python(DISK)
            assert code == 0, err
            disk = json.loads(out)
            assert Path(report["prefix"]).resolve() in Path(disk["site"]).parents, disk["site"]
            assert disk["on_disk"], "the installed distribution lists no package files"
            assert disk["on_disk"] == _wheel_record(installed[0])
            assert disk["unlisted"] == [], disk["unlisted"]
    finally:
        shutil.rmtree(base, ignore_errors=True)
