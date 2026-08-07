# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared capture functions for the V0 public-surface contract.

Every function here returns a JSON-serializable structure describing one
observable public surface (HTTP routes, OpenAPI, CLI parser tree, MCP
projections, machine-mode classification, or public imports). The frozen
snapshots under ``tests/contracts/data/`` were produced by calling these
functions once, before any consolidation edit; ``test_public_surfaces.py``
calls them again and diffs the live result against the frozen one.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(__file__).resolve().parent / "data"


def _stable(value: Any) -> Any:
    """A JSON-safe, deterministic representation of an arbitrary runtime value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_stable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in value.items()}
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    if type(value).__name__ == "DefaultPlaceholder":
        return {"default_placeholder": _stable(value.value)}
    if type(value).__name__ == "Depends":
        dep = value.dependency
        return {"depends": _stable(dep), "use_cache": getattr(value, "use_cache", None)}
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if qualname is not None:
        return f"{module}.{qualname}" if module else qualname
    return type(value).__name__


# ── HTTP routes + OpenAPI ────────────────────────────────────────────────────

# FastAPI strips a Starlette path-converter suffix (e.g. "{tag:path}") down to
# "{tag}" when it renders the OpenAPI document, so a route's raw `.path` must
# be normalized the same way before it is used as an `openapi["paths"]` key.
_PATH_CONVERTER_RE = re.compile(r"\{(\w+):[^}]*\}")

_OPENAPI_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _openapi_path_key(path: str) -> str:
    return _PATH_CONVERTER_RE.sub(r"{\1}", path)


def _route_responses(
    path: str | None, methods: list[str] | None, openapi_paths: dict[str, Any]
) -> dict[str, Any]:
    """Per-status-code ``responses`` for one route, read from the live OpenAPI
    document — ``APIRoute.responses`` itself is always empty in this codebase
    (the ``responses=`` kwarg only reaches FastAPI's OpenAPI generation, not
    the route object), so this is the only place the data actually lives."""
    if not path or not methods:
        return {}
    path_item = openapi_paths.get(_openapi_path_key(path), {})
    for method in methods:
        op = path_item.get(method.lower())
        if op is not None:
            return _stable(op.get("responses", {}))
    return {}


def _normalize_operation(op: Any) -> dict[str, Any] | None:
    """Stable projection of one OpenAPI operation object.

    ``operationId`` is deliberately excluded: it derives from the handler's
    Python qualified name and changes whenever a handler is absorbed into a
    new module, which the design explicitly treats as an internal detail, not
    an external route field. Every other field is preserved verbatim.
    """
    if not isinstance(op, dict):
        return None
    result: dict[str, Any] = {}
    tags = op.get("tags")
    if tags is not None:
        result["tags"] = sorted(tags)
    summary = op.get("summary")
    if summary is not None:
        result["summary"] = summary
    description = op.get("description")
    if description is not None:
        result["description"] = description
    parameters = op.get("parameters")
    if parameters is not None:
        result["parameters"] = [
            _stable(p) for p in sorted(parameters, key=lambda p: p.get("name", ""))
        ]
    request_body = op.get("requestBody")
    if request_body is not None:
        result["requestBody"] = _stable(request_body)
    responses = op.get("responses")
    if responses is not None:
        result["responses"] = _stable(responses)
    if op.get("deprecated"):
        result["deprecated"] = True
    security = op.get("security")
    if security is not None:
        result["security"] = _stable(security)
    return result


def _normalize_openapi(openapi: dict[str, Any]) -> dict[str, Any]:
    """Full, stable, JSON-serializable projection of the OpenAPI document:
    every path's operations (parameters, request/response schemas) and every
    named component schema in full, not just path/schema name lists."""
    paths: dict[str, Any] = {}
    for path_name in sorted(openapi.get("paths", {})):
        path_item = openapi["paths"][path_name]
        operations: dict[str, Any] = {}
        for method in sorted(k for k in path_item if k in _OPENAPI_HTTP_METHODS):
            normalized = _normalize_operation(path_item[method])
            if normalized is not None:
                operations[method] = normalized
        if operations:
            paths[path_name] = operations
    schemas = openapi.get("components", {}).get("schemas", {})
    return {
        "openapi": openapi.get("openapi"),
        "info": _stable(openapi.get("info")),
        "paths": paths,
        "path_count": len(paths),
        "schemas": _stable(schemas),
        "schema_count": len(schemas),
    }


def capture_http() -> dict[str, Any]:
    from lionagi.studio.app import create_app

    app = create_app()
    openapi = app.openapi()
    openapi_paths = openapi.get("paths", {})

    routes: list[dict[str, Any]] = []
    for ordinal, r in enumerate(app.routes):
        path = getattr(r, "path", None)
        methods = sorted(getattr(r, "methods", None) or []) if getattr(r, "methods", None) else None
        routes.append(
            {
                "ordinal": ordinal,
                "path": path,
                "methods": methods,
                "name": getattr(r, "name", None),
                "response_model": _stable(getattr(r, "response_model", None)),
                "dependencies": _stable(getattr(r, "dependencies", None) or []),
                "response_class": _stable(getattr(r, "response_class", None)),
                "responses": _route_responses(path, methods, openapi_paths),
                "status_code": getattr(r, "status_code", None),
                "tags": sorted(getattr(r, "tags", None) or []) if getattr(r, "tags", None) else [],
                "summary": getattr(r, "summary", None),
                "description": getattr(r, "description", None) or None,
                "include_in_schema": getattr(r, "include_in_schema", None),
                "handler_identity": {
                    "module": getattr(getattr(r, "endpoint", None), "__module__", None),
                    "qualname": getattr(getattr(r, "endpoint", None), "__qualname__", None),
                },
            }
        )
    return {
        "count": len(routes),
        "routes": routes,
        "openapi": _normalize_openapi(openapi),
    }


# ── CLI registry + parser tree ───────────────────────────────────────────────


def _action_to_dict(a: argparse.Action) -> dict[str, Any]:
    return {
        "option_strings": list(a.option_strings),
        "dest": a.dest,
        "required": getattr(a, "required", None),
        "nargs": a.nargs if isinstance(a.nargs, (int, str, type(None))) else str(a.nargs),
        "default": a.default
        if isinstance(a.default, (int, str, float, bool, type(None)))
        else str(a.default),
        "choices": sorted(a.choices) if a.choices else None,
        "help": a.help,
    }


def _parser_to_dict(p: argparse.ArgumentParser) -> dict[str, Any]:
    actions = [_action_to_dict(a) for a in p._actions]
    subparsers: dict[str, Any] = {}
    for a in p._actions:
        if hasattr(a, "choices") and a.choices and hasattr(a, "_name_parser_map"):
            for name, sub in a.choices.items():
                subparsers[name] = _parser_to_dict(sub)
    return {"prog": p.prog, "actions": actions, "subparsers": subparsers}


def capture_cli() -> dict[str, Any]:
    from lionagi._auto import build_cli_parser, iter_cli_seeds

    seeds = iter_cli_seeds()
    top_level = [
        {
            "name": seed.name,
            "help": seed.help,
            "aliases": sorted(seed.aliases),
        }
        for seed in seeds
    ]
    name_map: dict[str, str] = {}
    for seed in seeds:
        for token in (seed.name, *seed.aliases):
            name_map[token] = seed.name
    per_command: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for seed in seeds:
        try:
            build = build_cli_parser(seed)
            selected_parser = build.selected_parser
            if selected_parser is None:
                per_command[seed.name] = _parser_to_dict(build.parser)
            elif isinstance(selected_parser, dict):
                per_command[seed.name] = {k: _parser_to_dict(v) for k, v in selected_parser.items()}
            else:
                per_command[seed.name] = _parser_to_dict(selected_parser)
        except Exception as e:  # noqa: BLE001 — one unbuildable parser must not hide the rest
            errors[seed.name] = f"{type(e).__name__}: {e}"
    return {
        "registry_order": [s.name for s in seeds],
        "registry_count": len(seeds),
        "name_map": dict(sorted(name_map.items())),
        "name_map_count": len(name_map),
        "top_level": top_level,
        "per_command_detail": per_command,
        "build_errors": errors,
    }


def _run_cli(argv: list[str], timeout: float = 20.0) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "lionagi.cli.main", *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "argv": argv,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
    }


def _run_cli_env(
    argv: list[str],
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Same as :func:`_run_cli`, but lets the caller vary the subprocess's
    environment and working directory -- used by the differential-capture
    check below to prove a committable case's output does not depend on
    either."""
    env = {**os.environ, **(env_overrides or {})}
    proc = subprocess.run(
        [sys.executable, "-m", "lionagi.cli.main", *argv],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return {
        "argv": argv,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
    }


_DIFFERENTIAL_FAKE_HOME = "/tmp/lionagi-differential-fake-home"
_DIFFERENTIAL_FAKE_TMPDIR = "/tmp/lionagi-differential-fake-tmp"
_DIFFERENTIAL_FAKE_USER = "lionagi-differential-fake-user"


def differential_capture(argv: list[str], timeout: float = 20.0) -> list[dict[str, Any]]:
    """Capture *argv* three times: once under the ambient environment and
    working directory, once under a deliberately different HOME / TMPDIR /
    USER and a different working directory, and once more after a wall-clock
    gap crossing a one-second boundary. A stream that reads anything from
    the environment, the current directory, or the clock necessarily differs
    across these runs; genuinely static argparse usage/error text does not.
    This replaces guessing at what a leaked value looks like (a pattern list,
    a vocabulary of "known-safe" words) with a check on the property that
    actually matters: does the output depend on the machine at all."""
    fake_cwd = Path(_DIFFERENTIAL_FAKE_TMPDIR)
    fake_cwd.mkdir(parents=True, exist_ok=True)
    runs = [
        _run_cli_env(argv, timeout=timeout),
        _run_cli_env(
            argv,
            env_overrides={
                "HOME": _DIFFERENTIAL_FAKE_HOME,
                "TMPDIR": _DIFFERENTIAL_FAKE_TMPDIR,
                "USER": _DIFFERENTIAL_FAKE_USER,
                "LOGNAME": _DIFFERENTIAL_FAKE_USER,
                "USERNAME": _DIFFERENTIAL_FAKE_USER,
            },
            cwd=fake_cwd,
            timeout=timeout,
        ),
    ]
    time.sleep(1.05)
    runs.append(_run_cli_env(argv, timeout=timeout))
    return runs


def known_machine_identity() -> frozenset[str]:
    """Literal values that identify *this* machine or checkout: hostname,
    real username, home directory, and this repo's own checkout path.

    ``differential_capture`` above cannot catch a value that is constant on
    this machine but still identifying -- a hostname baked into a banner
    line does not vary between two runs on the same box. This closes that
    gap by redacting known values rather than guessing at shapes: it is
    redaction of an identified secret, not a pattern or vocabulary guess.
    """
    values = {
        socket.gethostname(),
        getpass.getuser(),
        str(Path.home()),
        str(REPO_ROOT),
    }
    return frozenset(v for v in values if v)


SPECIALIZED_CASES: tuple[tuple[str, ...], ...] = (
    ("--help",),
    ("wait",),
    ("skill", "list"),
    ("agent", "status"),
    ("monitor", "run"),
    ("doctor", "--machine"),
    ("bogus-unknown-command",),
    # ── play: pre-parser sugar for `li o flow -p NAME` (main.py:189-269) ────
    ("play",),  # no NAME: usage printed, exit 1
    ("play", "list"),  # lists playbooks read from disk; volatile content
    ("play", "nonexistent"),  # unresolvable playbook; volatile (lists available names)
    ("play", "--help"),  # no NAME before the flag: "NAME is required", exit 1
    # ── orchestrate flow / fanout: standalone intermixed-arg parse
    #    (main.py:396-425) ────────────────────────────────────────────────
    ("o", "flow", "--help"),
    ("o", "fanout", "--help"),
    ("o", "flow"),  # no prompt/file/playbook: exit 1
    ("o", "fanout"),  # no prompt: exit 1
    # ── schedule: standalone parse + quick-create + did-you-mean
    #    (main.py:427-457) ───────────────────────────────────────────────
    ("schedule", "--help"),
    ("schedule",),  # no subcommand: argparse required-subparser error, exit 2
    ("schedule", "list", "--bogus"),  # unrecognized flag, no synonym match, exit 2
    (
        "schedule",
        "create",
        "capture-test",
        "--every",
        "15m",
    ),  # legacy create + did-you-mean synonym (--every -> --interval), exit 2
    ("schedule", "create", "agent", "capture-test"),  # typed quick-create, missing
    # --profile/trigger: its own argparse subparser rejects before any network
    # call, exit 2
    (
        "schedule",
        "create",
        "command",
        "capture-test",
        "--every",
        "15m",
    ),  # quick-create validation error (missing trailing --), exit 1
)


def capture_specialized() -> list[dict[str, Any]]:
    return [_run_cli(list(argv)) for argv in SPECIALIZED_CASES]


# ── MCP available paths / catalog / projections / errors ────────────────────

# Negative cases spanning every distinct SchemaProjectionError class this
# codebase currently raises: empty path, unknown top-level command, an
# unresolved-subcommand path one level deep, an unresolved-subcommand path at
# the root, and the one unsupported-argparse-type case (`mirror --since` uses
# a custom `_since_window` type with no scalar JSON counterpart).
_MCP_NEGATIVE_CASES: tuple[str, ...] = (
    "",
    "nonexistent-command",
    "dispatch nonexistent-subcommand",
    "state",
    "mirror",
)


def _classify_projection_error(exc: Exception) -> str:
    """Bucket a SchemaProjectionError by its ``.reason`` text so the fixture
    records *why* a path failed, not just that it did."""
    reason = getattr(exc, "reason", None)
    if reason is None:
        return "other"
    if reason.startswith("path stops at an unresolved subcommand"):
        return "unresolved_subcommand"
    if "has no scalar JSON counterpart" in reason:
        return "unsupported_argparse_type"
    if reason == "empty command path":
        return "empty_command_path"
    if reason == "no such command":
        return "no_such_command"
    if reason == "no such command path":
        return "no_such_command_path"
    return "other"


def _seed_alias_map() -> dict[str, tuple[str, ...]]:
    from lionagi._auto import iter_cli_seeds

    return {seed.name: seed.aliases for seed in iter_cli_seeds() if seed.aliases}


def _aliases_for_path(path: str, alias_by_head: dict[str, tuple[str, ...]]) -> list[str]:
    """Alternative spellings of *path* reachable through a top-level CLI
    alias (e.g. ``o`` for ``orchestrate``), derived from the live seed table
    rather than a hardcoded name list."""
    parts = path.split()
    if not parts:
        return []
    head_aliases = alias_by_head.get(parts[0])
    if not head_aliases:
        return []
    return sorted(" ".join([alias, *parts[1:]]) for alias in head_aliases)


def capture_mcp() -> dict[str, Any]:
    from lionagi.mcp import dispatch as mcp_dispatch
    from lionagi.mcp import projection as mcp_projection
    from lionagi.mcp.verbs import ABSENT

    available = list(mcp_projection.available_paths())
    catalog = mcp_dispatch.catalog()
    alias_by_head = _seed_alias_map()

    # Every available path, not a 7-path sample: each entry carries its full
    # projected schema plus any alias spellings; each failure carries the
    # exception type, its classified reason, and the message.
    projections: dict[str, Any] = {}
    proj_errors: dict[str, dict[str, str]] = {}
    for path in available:
        try:
            result = mcp_projection.project(path)
            entry = result.to_dict()
            aliases = _aliases_for_path(path, alias_by_head)
            if aliases:
                entry["aliases"] = aliases
            projections[path] = entry
        except Exception as e:  # noqa: BLE001 — one unprojectable path must not hide the rest
            proj_errors[path] = {
                "kind": type(e).__name__,
                "class": _classify_projection_error(e),
                "message": str(e),
            }

    errors: list[dict[str, Any]] = []
    for bad_path in _MCP_NEGATIVE_CASES:
        try:
            mcp_projection.project(bad_path)
            errors.append({"input": bad_path, "kind": None, "class": None, "message": None})
        except Exception as e:  # noqa: BLE001
            errors.append(
                {
                    "input": bad_path,
                    "kind": type(e).__name__,
                    "class": _classify_projection_error(e),
                    "message": str(e),
                }
            )

    # The catalog's ABSENT verbs, in full (name, summary, reason, cli_path) —
    # not just folded into a verb_count delta.
    absent_verbs = [
        {"name": a.name, "summary": a.summary, "reason": a.reason, "cli_path": a.cli_path}
        for a in sorted(ABSENT, key=lambda a: a.name)
    ]

    return {
        "available_paths": available,
        "available_path_count": len(available),
        "catalog": {
            "verb_count": catalog["verb_count"],
            "available_count": catalog["available_count"],
            "max_ops": catalog["max_ops"],
            "verb_names": sorted(v["verb"] for v in catalog["verbs"]),
            "available_verb_names": sorted(v["verb"] for v in catalog["verbs"] if v["available"]),
        },
        "projections": projections,
        "projection_count": len(projections),
        "projection_errors": proj_errors,
        "projection_error_count": len(proj_errors),
        "absent_verbs": absent_verbs,
        "absent_verb_count": len(absent_verbs),
        "errors": errors,
    }


# ── Machine classification ──────────────────────────────────────────────────

MACHINE_CASES: tuple[tuple[str, ...], ...] = (
    ("handshake", "--machine"),
    ("doctor", "--machine"),
    ("runs", "--machine"),
    ("lifecycle", "--machine"),
    ("monitor", "--machine"),
    ("agent", "--machine"),
    ("bogus-unknown-command", "--machine"),
    ("--machine",),
)


def capture_machine() -> list[dict[str, Any]]:
    cases = []
    for argv in MACHINE_CASES:
        result = _run_cli(list(argv))
        parsed_ok = None
        try:
            parsed_ok = (
                json.loads(result["stdout"].strip()).get("ok") if result["stdout"].strip() else None
            )
        except Exception:  # noqa: BLE001
            parsed_ok = "unparseable"
        cases.append({**result, "envelope_ok": parsed_ok})
    return cases


# ── Fresh-process import-laziness trace ──────────────────────────────────────

# Self-contained subprocess payload: import only the named seed via the
# registry's own `load_cli_command`, then report which *other* seeds' modules
# leaked in, whether the HTTP registry got realized as a side effect, and
# which seed(s) ended up in `_cli_realized`. Comparing against
# `{s.module for s in iter_cli_seeds() if s.module != seed.module}` handles
# shared modules (studio/schedule -> lionagi.studio.cli;
# handshake/runs/lifecycle -> lionagi.cli.machine) for free: a shared
# module's string is removed from the "other" set together with the selected
# seed's own module, so loading `studio` never flags `lionagi.studio.cli`
# even though `schedule` also maps to it.
_IMPORT_TRACE_SCRIPT = (
    "import sys, json\n"
    "from lionagi._auto import load_cli_command, seed_for, iter_cli_seeds, _http, _cli_realized\n"
    "name = sys.argv[1]\n"
    "seed = seed_for(name)\n"
    "other_modules = {s.module for s in iter_cli_seeds() if s.module != seed.module}\n"
    "before = set(sys.modules)\n"
    "load_cli_command(seed)\n"
    "after = set(sys.modules)\n"
    "new_lionagi = sorted(m for m in (after - before) if m.startswith('lionagi.'))\n"
    "leaked = sorted(other_modules & set(new_lionagi))\n"
    "result = {\n"
    "    'seed': name,\n"
    "    'module': seed.module,\n"
    "    'new_lionagi_module_count': len(new_lionagi),\n"
    "    'other_seed_modules_imported': leaked,\n"
    "    'other_seed_modules_imported_count': len(leaked),\n"
    "    'http_registry_realized': len(_http) > 0,\n"
    "    'http_registry_count': len(_http),\n"
    "    'cli_realized_names': sorted(_cli_realized.keys()),\n"
    "}\n"
    "print(json.dumps(result))\n"
)


def _run_import_trace(seed_name: str, timeout: float = 20.0) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_TRACE_SCRIPT, seed_name],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return {
            "seed": seed_name,
            "_error": f"exit {proc.returncode}: {proc.stderr.strip()[-2000:]}",
        }
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        return {
            "seed": seed_name,
            "_error": f"unparseable output: {e}: stdout={proc.stdout!r} stderr={proc.stderr!r}",
        }


def capture_import_laziness() -> dict[str, Any]:
    """Fresh-process ``sys.modules`` traces proving selected-only CLI loading.

    For each of the 21 canonical CLI seeds, launches a fresh subprocess that
    imports only that seed through the registry (``load_cli_command``), never
    the whole CLI. In-process checks (``capture_imports`` below) cannot prove
    this: pytest itself has already imported most of the tree by the time a
    test runs, so only a fresh interpreter can show what one seed selection
    actually pulls in.
    """
    from lionagi._auto import iter_cli_seeds

    seeds = [s.name for s in iter_cli_seeds()]
    traces = {name: _run_import_trace(name) for name in seeds}
    return {
        "seed_count": len(seeds),
        "seed_names": seeds,
        "traces": traces,
    }


# ── Import surfaces ──────────────────────────────────────────────────────────

COMPAT_MODULES: tuple[str, ...] = (
    "lionagi.protocols.types",
    "lionagi.tools.types",
    "lionagi.operations.parse",
    "lionagi.service.connections.cli_endpoint",
    "lionagi.dispatch.revival",
)


def capture_imports() -> dict[str, Any]:
    import importlib
    import warnings

    import lionagi

    root_all = list(lionagi.__all__)
    symbols: dict[str, Any] = {}
    for name in root_all:
        try:
            val = getattr(lionagi, name)
            symbols[name] = {
                "type": type(val).__name__,
                "module": getattr(val, "__module__", None),
                "qualname": getattr(val, "__qualname__", None),
            }
        except Exception as e:  # noqa: BLE001
            symbols[name] = {"error": f"{type(e).__name__}: {e}"}

    lazy_map = getattr(lionagi, "_LAZY_MAP", None) or {}
    lazy_map_keys = sorted(lazy_map.keys())

    compat: dict[str, Any] = {}
    for m in COMPAT_MODULES:
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                mod = importlib.import_module(m)
                compat[m] = {
                    "ok": True,
                    "dir": sorted(n for n in dir(mod) if not n.startswith("_")),
                    "warning_categories": sorted({wi.category.__name__ for wi in w}),
                }
        except Exception as e:  # noqa: BLE001
            compat[m] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return {
        "root_all": sorted(root_all),
        "root_all_count": len(root_all),
        "symbols": symbols,
        "lazy_map_keys": lazy_map_keys,
        "lazy_map_key_count": len(lazy_map_keys),
        "compat_modules": compat,
        "import_laziness": capture_import_laziness(),
    }


def capture_all() -> dict[str, Any]:
    return {
        "http": capture_http(),
        "cli": capture_cli(),
        "specialized": capture_specialized(),
        "mcp": capture_mcp(),
        "machine": capture_machine(),
        "imports": capture_imports(),
    }
