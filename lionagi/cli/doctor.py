# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li doctor` — environment/install preflight checks for the lionagi CLI."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from lionagi._auto import CliDeclaration, auto_register

__all__ = (
    "add_doctor_subparser",
    "run_doctor",
    "collect_checks",
)

# ── check inputs ─────────────────────────────────────────────────────────────

# Subsystems whose import already exercises most of the dependency graph —
# an ImportError here surfaces the same root cause `li --version` hits.
_IMPORT_PROBES = (
    "lionagi",
    "lionagi.session.branch",
    "lionagi.cli.main",
    "lionagi.service",
    "lionagi.operations",
)

# Small, explicit subset of pyproject.toml [project] dependencies whose
# import name differs enough from the package name to be worth spelling out.
_CORE_DEPS: dict[str, str] = {
    "pydantic": "pydantic",
    "aiohttp": "aiohttp",
    "sqlalchemy": "sqlalchemy",
    "aiosqlite": "aiosqlite",
    "psutil": "psutil",
}

_STUDIO_HEALTH_URL_DEFAULT = "http://127.0.0.1:8765/api/admin/health"

_SYMBOLS = {"ok": "✓", "warn": "!", "fail": "✗", "unknown": "?"}

# Statuses that must not be read as a passing check. `unknown` is here because a
# check that could not be run has established nothing, and reporting it as a pass
# is the failure mode this file exists to prevent.
_NOT_PASSING = ("fail", "unknown")


def _result(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def _looks_editable(location: str | None) -> bool:
    """True if *location* sits under a source tree with a pyproject.toml."""
    if not location:
        return False
    path = Path(location).resolve()
    for parent in (path, *path.parents):
        if (parent / "pyproject.toml").is_file():
            return True
    return False


def _check_version() -> dict[str, str]:
    try:
        import lionagi
        from lionagi.version import __version__
    except Exception as exc:  # noqa: BLE001 — report root cause, not just ImportError
        return _result("fail", f"could not import lionagi: {type(exc).__name__}: {exc}")
    location = getattr(lionagi, "__file__", None)
    detail = f"lionagi {__version__} at {location}"
    # Editability is informational only: wheel installs are intentionally
    # non-editable and perfectly healthy.
    if _looks_editable(location):
        detail += " (editable install)"
    return _result("ok", detail)


def _check_python() -> dict[str, str]:
    detail = f"Python {sys.version.split()[0]} — prefix {sys.prefix}"
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        return _result("warn", detail + " (not running inside a virtualenv)")
    return _result("ok", detail)


def _check_imports() -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for mod in _IMPORT_PROBES:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 — surface the actual broken link
            results[mod] = _result("fail", f"{type(exc).__name__}: {exc}")
        else:
            results[mod] = _result("ok", "import ok")
    return results


def _check_core_deps() -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for dep_name, import_name in _CORE_DEPS.items():
        try:
            importlib.import_module(import_name)
        except Exception as exc:  # noqa: BLE001
            results[dep_name] = _result("fail", f"{type(exc).__name__}: {exc}")
        else:
            results[dep_name] = _result("ok", "importable")
    return results


def _check_studio_daemon(url: str | None = None, timeout: float = 1.5) -> dict[str, str]:
    """Optional check — the Studio daemon is not required for `li agent`/`li o flow`."""
    import urllib.error
    import urllib.request

    target = url or _STUDIO_HEALTH_URL_DEFAULT
    try:
        req = urllib.request.Request(target, method="GET")  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status == 200:
                return _result("ok", f"Studio daemon reachable at {target}")
            return _result("warn", f"Studio daemon at {target} returned HTTP {resp.status}")
    except Exception as exc:  # noqa: BLE001 — connection refused, timeout, DNS, etc.
        return _result(
            "warn",
            f"Studio daemon unreachable at {target} ({type(exc).__name__}: {exc}) — "
            "optional; scheduled/agent-spawn actions that route through it will fail "
            "until `li studio` is running.",
        )


def _check_lionagi_home(home: Path | None = None) -> dict[str, str]:
    from lionagi._paths import ensure_lionagi_dir

    if home is None:
        from lionagi._paths import LIONAGI_HOME

        home = LIONAGI_HOME
    runs_dir = home / "runs"
    try:
        ensure_lionagi_dir(runs_dir)
        probe = runs_dir / ".doctor-write-probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return _result("fail", f"{home} not writable: {exc}")
    return _result("ok", f"{home} writable (runs/ dir ok)")


def _check_code_identity() -> dict[str, str]:
    """Fail when the code that answered is not the code the environment implies.

    An install can be healthy in every other respect and still be serving a tree
    that stopped tracking its upstream commits ago — the process loaded that tree
    once, at startup, and nothing since has told anyone. This is the check that
    tells them.
    """
    from ._code_identity import code_identity

    try:
        identity = code_identity()
    except Exception as exc:  # noqa: BLE001 — an unanswerable check is unknown, not ok
        return _result("unknown", f"could not establish code identity: {type(exc).__name__}: {exc}")

    version = identity["version"]
    path = identity["package_path"]
    verbs = identity["verb_count"]
    where = f"lionagi {version} at {path}, {verbs} verbs registered"

    git = identity["git"]
    if git["status"] == "ok":
        position = git["commit_short"]
        if git["branch"]:
            position += f" on {git['branch']}"
        else:
            position += " (detached)"
        if git.get("dirty"):
            # A commit id next to a dirty tree reads as the whole story; it isn't.
            position += " with uncommitted changes"
        where += f", git {position}"
        # The position is the one read when this process started, not the tree's
        # position now, so it is quoted with the time it was true of.
        taken_at = identity.get("git_snapshot_taken_at")
        if taken_at:
            where += f" as read at {taken_at}"
    elif git["status"] == "not_a_git_checkout":
        where += ", not a git checkout"

    drift = identity["drift"]
    if drift["status"] == "drift":
        return _result("fail", f"{where} — " + "; ".join(drift["reasons"]))
    if drift["status"] == "unknown":
        return _result("unknown", f"{where} — " + "; ".join(drift["unknown"]))
    return _result("ok", where)


def collect_checks() -> dict[str, dict[str, str]]:
    """Run every check and return a flat name -> {status, detail} mapping."""
    checks: dict[str, dict[str, str]] = {}
    checks["lionagi_version"] = _check_version()
    checks["code_identity"] = _check_code_identity()
    checks["python"] = _check_python()
    for mod, result in _check_imports().items():
        checks[f"import:{mod}"] = result
    for dep, result in _check_core_deps().items():
        checks[f"dep:{dep}"] = result
    checks["studio_daemon"] = _check_studio_daemon()
    checks["lionagi_home"] = _check_lionagi_home()
    return checks


def add_doctor_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li doctor` with argparse."""
    p = subparsers.add_parser(
        "doctor",
        help="Check the lionagi CLI environment/install for common failure modes.",
        description=(
            "Environment preflight: install location + editability, Python/venv, "
            "the import chain `li --version` traverses, core dependency "
            "importability, Studio daemon reachability, and ~/.lionagi writability."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object of check name -> {status, detail} instead of plain text.",
    )


@auto_register(
    area="doctor", cli=CliDeclaration(seed="doctor", parser_factory=add_doctor_subparser)
)
def run_doctor(args: argparse.Namespace) -> int:
    checks = collect_checks()
    if getattr(args, "json", False):
        print(json.dumps(checks, indent=2))
    else:
        for name, result in checks.items():
            symbol = _SYMBOLS.get(result["status"], "?")
            print(f"{symbol} {name}: {result['detail']}")
    if any(result["status"] in _NOT_PASSING for result in checks.values()):
        return 1
    return 0
