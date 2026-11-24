"""Pytest plugin: run a test session with lionhub refused, and record which modules need it.

Load it with ``-p scripts.hubless_pytest_plugin`` and nothing else: importing this module puts a
finder at the front of ``sys.meta_path`` that refuses ``lionhub`` and every module under it, so
the session runs as core would with lionhub not installed. The refusal raises
``ModuleNotFoundError`` rather than reporting the module absent, because a source checkout also
has a ``lionhub/`` directory that a later finder would import as a namespace package. A plain
``importlib.util.find_spec("lionhub")`` therefore raises here where an absent package returns
``None``.

Each test report carries how many refusals happened while its test ran. With the environment
variable named by ``REPORT_ENV`` set, the controlling process writes one JSON report there when
the session ends; ``scripts/check_core_without_hub.py`` reads it.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.abc import MetaPathFinder
from pathlib import Path

import pytest

from scripts.check_core_without_hub import (
    REFUSAL_MARK,
    REFUSALS_PROPERTY,
    REFUSED_ROOT,
    REPORT_ENV,
)


def _is_refused(name: str) -> bool:
    return name == REFUSED_ROOT or name.startswith(REFUSED_ROOT + ".")


class _RefuseHub(MetaPathFinder):
    """Refuse every import of the hub package, counting each refusal."""

    def __init__(self) -> None:
        self.refusals = 0

    def find_spec(self, fullname, path=None, target=None):
        if not _is_refused(fullname):
            return None
        self.refusals += 1
        raise ModuleNotFoundError(f"No module named {fullname!r} ({REFUSAL_MARK})", name=fullname)


def _install() -> _RefuseHub:
    loaded = sorted(name for name in sys.modules if _is_refused(name))
    if loaded:
        raise RuntimeError(
            f"{loaded[0]} was imported before scripts.hubless_pytest_plugin loaded, so this run "
            "cannot refuse it; load the plugin with -p before anything imports the hub"
        )
    finder = _RefuseHub()
    sys.meta_path.insert(0, finder)
    return finder


FINDER = _install()

_START = pytest.StashKey[int]()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    item.stash[_START] = FINDER.refusals
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    start = item.stash.get(_START, FINDER.refusals)
    report.user_properties.append((REFUSALS_PROPERTY, FINDER.refusals - start))


def _caused_by_hub(report) -> bool:
    for name, value in getattr(report, "user_properties", None) or ():
        if name == REFUSALS_PROPERTY and value:
            return True
    return REFUSAL_MARK in str(getattr(report, "longrepr", "") or "")


def _module_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0] or "<session>"


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if hasattr(config, "workerinput"):
        return
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter is not None else {}
    modules: dict[str, dict[str, int]] = {}
    seen: dict[str, set[str]] = {}
    for outcome in ("passed", "failed", "error", "skipped", "xfailed", "xpassed"):
        for report in stats.get(outcome, ()):
            nodeid = getattr(report, "nodeid", None)
            if nodeid is None:
                continue
            module = _module_of(nodeid)
            entry = modules.setdefault(module, {"tests": 0, "failures": 0, "hub_failures": 0})
            if "::" in nodeid and nodeid not in seen.setdefault(module, set()):
                seen[module].add(nodeid)
                entry["tests"] += 1
            if outcome in ("failed", "error"):
                entry["failures"] += 1
                if _caused_by_hub(report):
                    entry["hub_failures"] += 1
    document = {
        "refused": REFUSED_ROOT,
        "exit_code": int(exitstatus),
        "modules": dict(sorted(modules.items())),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    os.replace(partial, target)
