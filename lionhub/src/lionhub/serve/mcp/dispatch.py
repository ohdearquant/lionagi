# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Dispatch for the single tool: validate an op, run it, envelope the result.

The advertised tool describes only ``ops`` and ``help``, so the catalog
carries a full signature per verb (not a bare name) and a rejected op comes
back with the schema it was judged against — each costs one round-trip
instead of forcing a second help call. Every schema is generated from the
CLI's own parser at the moment it's asked for, so a flag renamed in the CLI
moves here with it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from lionagi import _lionagi_dir_trust as lionagi_dir_trust
from lionagi._provider_errors import NestedAgentRefusedError
from lionagi.ln import compute_hash, now_utc
from lionhub.orchestration.job import EXIT_CODE_ENVIRONMENT_ERROR
from lionhub.orchestration.spec_limits import MAX_SPEC_PROMPT_CHARS

from . import accounts, config, jobs, projection, roster
from .verbs import (
    ABSENT,
    MAX_OPS,
    VERBS,
    Verb,
)

__all__ = (
    "MACHINE_TIMEOUT_SECONDS",
    "MACHINE_OUTPUT_LIMIT",
    "OpError",
    "catalog",
    "verb_schema",
    "render_argv",
    "request",
)

# A machine command is a control-plane read; anything slower than this is a
# command that has stopped answering, not one still working.
MACHINE_TIMEOUT_SECONDS = 60.0

# The most a machine command may write on its result channel. Beyond it the
# result is an explicit overflow error rather than a truncated JSON document that
# would fail to parse with a misleading message.
MACHINE_OUTPUT_LIMIT = 1_000_000
_MACHINE_STDERR_TAIL = 2_000

_STARTED_AT = now_utc().isoformat()
_STARTED_MONOTONIC = time.time()


# Modules this server imports inside its functions. ``preload`` imports them before the server
# answers anything, so a later change to the installed source cannot leave the process holding
# some of its modules from before the change and failing to import the others.
_PRELOAD_MODULES = (
    "lionhub.tools.command_env",
    "lionhub.tools.command_env_metadata",
    "lionhub.orchestration.flow.spec",
    "lionagi._paths",
    "lionagi._provider_errors",
    "lionagi._types",
    "lionagi.adapters.pandas_",
    "lionagi.hooks",
    "lionhub.state.message_retry",
    "lionagi.hooks.bus",
    "lionagi.libs.file.process",
    "lionagi.libs.frontmatter",
    "lionagi.libs.schema.load_pydantic_model_from_schema",
    "lionagi.libs.schema.minimal_yaml",
    "lionagi.ln._proc",
    "lionagi.ln.concurrency",
    "lionagi.ln.concurrency.errors",
    "lionagi.operations.node",
    "lionagi.operations.run.stream_buffer",
    "lionagi.plugins",
    "lionagi.protocols.action.gate",
    "lionhub.providers.anthropic.accounts._router",
    "lionagi.service.accounts._scope",
    "lionagi.service.connections._chat_schemas",
    "lionagi.service.connections.registry",
    "lionagi.service.providers",
    "lionhub.providers.model_spec",
    "lionagi.service.token_calculator",
    "lionagi.session.prompts",
    "lionhub.state.cli_workers",
    "lionhub.state.compat",
    "lionhub.state.completion_evidence",
    "lionhub.state.db",
    "lionhub.state.engine",
    "lionhub.state.health",
    "lionhub.state.lifecycle.notify_settings",
    "lionhub.state.lifecycle.policy",
    "lionhub.state.reasons",
    "lionagi.version",
    "lionhub.agent",
    "lionhub.agent._email_in_headers",
    "lionhub.agent.casts.catalog",
    "lionhub.agent.casts.pack",
    "lionhub.agent.casts.pattern",
    "lionhub.agent.child_sandbox",
    "lionhub.agent.factory",
    "lionhub.agent.gate",
    "lionhub.agent.hooks",
    "lionhub.agent.nudge",
    "lionhub.agent.permissions",
    "lionhub.agent.settings",
    "lionhub.agent.subscription_accounts",
    "lionhub.hooks.external",
    "lionhub.hooks.hooks_from",
    "lionhub.orchestration",
    "lionhub.orchestration.console",
    "lionhub.orchestration.job",
    "lionhub.orchestration.mcp_config",
    "lionhub.orchestration.project_store",
    "lionhub.orchestration.run_persistence",
    "lionhub.orchestration.runs",
    "lionhub.orchestration.schedule.declaration",
    "lionhub.orchestration.schedule.execution_root",
    "lionhub.orchestration.schedule.export",
    "lionhub.orchestration.schedule.timing",
    "lionhub.orchestration.scheduler.db_maintenance",
    "lionhub.orchestration.scheduler.engine",
    "lionhub.orchestration.scheduler.github",
    "lionhub.orchestration.scheduler.lifecycle",
    "lionhub.orchestration.scheduler.liveness",
    "lionhub.orchestration.scheduler.settings",
    "lionhub.orchestration.scheduler.terminal_reconciliation",
    "lionhub.orchestration.scheduler.worker",
    "lionhub.orchestration.spawn",
    "lionhub.providers.anthropic.claude_code",
    "lionhub.providers.google.gemini_code",
    "lionhub.providers.openai._codex_hook_trust",
    "lionhub.providers.openai._codex_profile",
    "lionhub.providers.openai._codex_project_layers",
    "lionhub.providers.openai.codex",
    "lionhub.serve.cli",
    "lionhub.serve.cli._machine_envelope",
    "lionhub.serve.cli.machine",
    "lionhub.serve.cli.machine_schedule",
    "lionhub.serve.cli.main",
    "lionhub.serve.cli.orchestrate",
    "lionhub.serve.cli.orchestrate._common",
    "lionhub.serve.cli.orchestrate._control",
    "lionhub.serve.cli.orchestrate._github_pr",
    "lionhub.serve.cli.orchestrate._orchestration",
    "lionhub.serve.cli.team",
    "lionhub.serve.cli.wait",
    "lionhub.serve.daemon.reconnect",
    "lionhub.serve.daemon.services",
    "lionhub.serve.daemon.services._sse",
    "lionhub.serve.daemon.services.run_tags_store",
    "lionhub.serve.daemon.services.runs",
    "lionhub.serve.daemon.services.sessions",
    "lionhub.serve.daemon.services.signals",
    "lionhub.serve.diagnostics",
    "lionhub.serve.mcp._notify_hook",
    "lionhub.serve.mcp._run_detail",
    "lionhub.tools._seatbelt",
    "lionhub.tools.code.ast_search",
    "lionhub.tools.code.bash",
    "lionhub.tools.code.search",
    "lionhub.tools.coding",
    "lionhub.tools.context.context",
    "lionhub.tools.file.editor",
    "lionhub.tools.file.reader",
    "lionhub.tools.khive_injection",
    "lionhub.tools.sandbox",
)
_OWN_PACKAGES = ("lionagi", "lionhub")
_logger = logging.getLogger(__name__)
# Replaced in tests; exiting the process is the point in production.
_exit = os._exit


def _own_module(exc: BaseException) -> str | None:
    """The module *exc* failed to import when it is this server's own code, otherwise None."""
    module = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
    if not module or module.split(".", 1)[0] not in _OWN_PACKAGES:
        return None
    return module


def _stop_if_own_module_missing(exc: BaseException) -> None:
    """End the process when *exc* is a failure to import this server's own code.

    That is an ``ImportError`` naming one of its own modules, or a ``SyntaxError`` in one of
    its own source files. Either means the installed source no longer matches the modules
    this process loaded, so the server would answer with some verbs working and the rest
    refused. It exits instead, with ``EXIT_CODE_ENVIRONMENT_ERROR`` and a message naming the
    module or file, so the client starts a fresh server. Any other failure returns.
    """
    if isinstance(exc, SyntaxError):
        if _own_source(exc.filename):
            _exit_for_own_module(str(exc.filename), exc)
        return
    module = _own_module(exc)
    if module is None:
        return
    _exit_for_own_module(module, exc)


def _own_source(path: object) -> bool:
    """Whether *path* is a source file of this server's own packages."""
    if not isinstance(path, str) or not path:
        return False
    source = Path(path).resolve()
    for package in _OWN_PACKAGES:
        spec = find_spec(package)
        for root in (spec.submodule_search_locations or ()) if spec else ():
            if source.is_relative_to(Path(root).resolve()):
                return True
    return False


def _exit_for_own_module(module: str, exc: BaseException) -> None:
    sys.stderr.write(
        f"lion MCP server: its own module {module} cannot be imported "
        f"({type(exc).__name__}: {exc}). The installed source changed after this server "
        "started, or is broken; exiting so the client starts a fresh server.\n"
    )
    sys.stderr.flush()
    _exit(EXIT_CODE_ENVIRONMENT_ERROR)


def preload() -> None:
    """Import every module this server uses and build every verb's schema, before serving.

    A module of this server that fails to import here, or whose own source no longer
    parses, ends the process before it answers anything. A module that needs a package which is not
    installed, or that raises anything else while it is imported (a configuration value it
    rejects, a settings file it cannot read), stays as it was: the verbs that use it report
    that when they are called, and the server answers the rest.
    """
    for name in _PRELOAD_MODULES:
        try:
            import_module(name)
        except ImportError as exc:
            _stop_if_own_module_missing(exc)
        except Exception as exc:
            _stop_if_own_module_missing(exc)
            # The type only: an exception's text can echo a rejected setting's value.
            _logger.warning(
                "lion MCP server: module %s did not load (%s); the verbs that use it report why",
                name,
                type(exc).__name__,
            )
    catalog()


class OpError(Exception):
    """A refusal of one op, carrying the kind a caller may branch on."""

    def __init__(self, kind: str, message: str, detail: Any = None) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(message)


# ── schema assembly ──────────────────────────────────────────────────────────


def verb_schema(verb: Verb, *, playbook: str | None = None) -> dict[str, Any]:
    """The parameter schema *verb* is validated against, built now.

    A verb backed by a CLI path is projected from that command's real parser and
    then narrowed: the parameters the verb does not pass through are dropped, and
    the ones this server implements itself are merged over the result.
    """
    if verb.own_schema is not None:
        schema = json.loads(json.dumps(verb.own_schema))
        schema["title"] = verb.name
        schema["description"] = verb.summary
        return schema

    assert verb.cli_path is not None
    projected = projection.project(verb.cli_path, playbook=playbook)
    schema = projected.schema
    properties: dict[str, Any] = {}
    for name, spec in schema.get("properties", {}).items():
        if name in verb.refuses:
            continue
        if verb.admits is not None and name not in verb.admits:
            continue
        properties[name] = spec
    properties.update({name: dict(spec) for name, spec in verb.server_params.items()})
    if verb.name == "agent.submit" and "resume" in properties:
        resume = properties["resume"]
        properties["resume"] = {
            **resume,
            "description": (
                f"{resume['description']} A full run ID from an agent job in this "
                "server's home is also accepted when its branch has been saved."
            ),
        }

    required = [name for name in schema.get("required", []) if name in properties]
    required += [name for name in verb.requires if name not in required]
    unenforced = [
        name
        for name in schema.get("x-required-unenforced", [])
        if name in properties and name not in required
    ]
    order = [name for name in schema.get("x-positional-order", []) if name in properties]

    out: dict[str, Any] = {
        "type": "object",
        "title": verb.name,
        "description": verb.summary,
        "properties": properties,
        "additionalProperties": False,
        "x-cli-path": verb.cli_path,
    }
    if required:
        out["required"] = required
    if unenforced:
        out["x-required-unenforced"] = unenforced
    if order:
        out["x-positional-order"] = order
    if verb.refuses:
        out["x-refused"] = dict(verb.refuses)
    for key in ("x-mutually-exclusive", "x-playbook-arguments", "x-playbook-fingerprint"):
        if key in schema:
            out[key] = schema[key]
    if projected.playbook is not None:
        out["x-playbook"] = projected.playbook
        out["x-playbook-path"] = projected.playbook_path
    return out


def catalog() -> dict[str, Any]:
    """Every verb, with enough of a signature to write the common invocation.

    A verb whose ops must carry a ``schema_fingerprint`` gets it here (the
    schema is already built to read ``required`` off it, so the hash costs
    nothing extra). Where the schema depends on an argument, no fingerprint is
    quoted — it would never match — and the entry instead names the parameter
    it varies with.

    Every caller pays for this listing, so an entry states only what it cannot
    be read without. ``available`` and ``required`` are omitted at their
    defaults, and an unavailable verb names the ``cli_path`` that does run it
    rather than repeating the paragraph on why it is not served here — that
    paragraph is what ``help='<verb>'`` returns. A verb whose schema failed to
    build is the exception and keeps its reason inline: it reports a defect in
    this server rather than a deliberate exclusion, and it must not need a
    second call to be noticed.
    """
    entries: list[dict[str, Any]] = []
    for verb in VERBS.values():
        entry: dict[str, Any] = {"verb": verb.name, "summary": verb.summary}
        try:
            schema = verb_schema(verb)
            required = list(schema.get("required", []))
            if required:
                entry["required"] = required
            unenforced = list(schema.get("x-required-unenforced", []))
            if unenforced:
                entry["required_unenforced"] = unenforced
            if verb.executor == "spawn":
                _describe_fingerprint(entry, verb, schema)
        except Exception as exc:  # noqa: BLE001 — one unreadable parser must not hide the rest
            _stop_if_own_module_missing(exc)
            entry["available"] = False
            entry["reason"] = f"schema generation failed: {type(exc).__name__}: {exc}"
        entries.append(entry)
    for absent in ABSENT:
        entries.append(
            {
                "verb": absent.name,
                "available": False,
                "summary": absent.summary,
                "cli_path": absent.cli_path,
            }
        )
    available = [e for e in entries if e.get("available", True)]
    return {
        "verbs": entries,
        "verb_count": len(entries),
        "available_count": len(available),
        "max_ops": MAX_OPS,
        "help_usage": (
            "help='<verb>' returns that verb's full parameter schema, and for an "
            "unavailable one the reason it is not served here. "
            "help={'verb': '<verb>', 'playbook': '<name>', 'cwd': '<dir>'} resolves a "
            "playbook's own declared arguments into the schema, finding the playbook "
            "under 'cwd' as the submitted run will (this server's directory when "
            "omitted). An entry omits 'available' and "
            "'required' when they are true and empty. A schema_fingerprint must be "
            "repeated on that verb's ops as a sibling of 'args': "
            "{'op': 'agent.submit', 'args': {...}, 'schema_fingerprint': '<from this entry>'}; "
            "where the entry carries schema_fingerprint_varies_with instead, pass one of "
            "the parameters it names to help and send the fingerprint returned for that "
            "spelling. required_unenforced names parameters the parser will not refuse a "
            "call for omitting but the command cannot do its work without."
        ),
    }


# ── schema fingerprint ───────────────────────────────────────────────────────


def schema_fingerprint(schema: dict[str, Any]) -> str:
    """A short digest of a verb's schema, stable across processes.

    Derived from the schema's own content, so it changes exactly when the
    parameters a caller would have read change, and not when anything else about
    the build does.
    """
    body = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return compute_hash(body, max_bytes=None)[:16]


def _describe_fingerprint(
    entry: dict[str, Any],
    verb: Verb,
    schema: dict[str, Any],
    *,
    playbook: str | None = None,
) -> None:
    """Say what a fingerprint-gated verb's ops have to carry.

    A playbook-aware verb's fingerprint is a function of the playbook argument.
    When the playbook is optional, the argument-free schema is a real call and
    its fingerprint is quoted; when the verb requires one, quoting anything
    would hand the caller a string guaranteed to be refused.
    """
    varies = ["playbook"] if verb.playbook_aware else []
    if varies:
        entry["schema_fingerprint_varies_with"] = varies
    if playbook is None and any(name in verb.requires for name in varies):
        return
    entry["schema_fingerprint"] = schema_fingerprint(schema)


# The one statement of how a caller recovers from a stale_schema refusal. Spawn-verb help
# and both refusal branches carry this text; docs/reference/mcp-server.md states the rule.
_SPAWN_RETRY_RULE = (
    "Send schema_fingerprint beside 'args'. If the server returns stale_schema and your "
    "arguments match the schema it returns (error.schema, or error.schema_ref naming the "
    "op_index whose error already carries that schema in the same response), retry once "
    "with error.detail.schema_fingerprint."
)
# Help for a verb whose fingerprint depends on an argument help was not given.
_FINGERPRINT_NEEDS_ARGUMENT = (
    "This verb's fingerprint depends on the parameters in schema_fingerprint_varies_with; "
    "pass them to help to get the fingerprint its ops carry. "
)


def _require_fingerprint(
    name: str,
    verb: Verb,
    schema: dict[str, Any],
    supplied: Any,
    *,
    playbook: str | None = None,
    cwd: str | None = None,
) -> None:
    """Spawn ops carry the current fingerprint from help or the refusal.

    Establishes that the schema the caller validated against is the schema
    about to run (not, in general, that the caller itself read it — a
    fingerprint is a string and can be inherited). The refusal's remedy names
    the resolved playbook, since a pointer to the verb alone would send a
    re-fetching caller to the argument-free schema and back into this refusal.
    """
    if playbook is None and verb.playbook_aware and "playbook" in verb.requires:
        # help never hands out the argument-free schema's fingerprint (no
        # successful call carries it); let validation report the missing
        # playbook instead — the error the caller can actually act on.
        return
    current = schema_fingerprint(schema)
    if supplied == current:
        return
    if playbook is not None:
        # The playbook resolved under *cwd*, so the help call that reproduces this
        # fingerprint has to name the same directory.
        source: Any = {"verb": name, "playbook": playbook}
        if cwd is not None:
            source["cwd"] = cwd
    elif verb.playbook_aware:
        source = {"verb": name}
    else:
        source = name
    remedy = {"help": source, "schema_fingerprint": current}
    # Spelled as the whole op: `schema_fingerprint` inside `args` is silently
    # unread, so a misplaced key would repeat this refusal forever.
    shape = f"{{'op': {name!r}, 'args': {{...}}, 'schema_fingerprint': {current!r}}}"
    ask = f"help={source!r}"
    if supplied is None:
        raise OpError(
            "stale_schema",
            f"{name!r} requires a schema_fingerprint, as a sibling of 'args': {shape}. "
            f"{_SPAWN_RETRY_RULE} The schema alone: {ask}.",
            remedy,
        )
    raise OpError(
        "stale_schema",
        f"{name!r} was called with schema_fingerprint {supplied!r}, which is not the "
        f"current {current!r}; the parameters changed since that schema was read. "
        f"{_SPAWN_RETRY_RULE} Send: {shape}. The schema alone: {ask}.",
        remedy,
    )


# ── closed argument validation ───────────────────────────────────────────────

_JSON_TYPE_CHECK = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _describes(spec: dict[str, Any]) -> str:
    """How a branch of an ``anyOf`` reads in a refusal."""
    if "const" in spec:
        return f"the literal {json.dumps(spec['const'])}"
    return str(spec.get("type", "a value the schema describes"))


def _check_value(name: str, spec: dict[str, Any], value: Any) -> list[str]:
    problems: list[str] = []
    if "anyOf" in spec:
        # A flag legal bare (its value, or true for the bare form) — value
        # must satisfy one branch, checked explicitly rather than admitted.
        branches = spec["anyOf"]
        if any(not _check_value(name, branch, value) for branch in branches):
            return problems
        wanted = " or ".join(_describes(branch) for branch in branches)
        problems.append(f"{name!r} expects {wanted}, got {type(value).__name__}")
        return problems
    if "const" in spec:
        const = spec["const"]
        # `1 == True` in Python, so a bare-flag branch spelled `const: true` would
        # otherwise admit the integer 1 and render it as the flag.
        same_kind = isinstance(value, bool) == isinstance(const, bool)
        if not same_kind or value != const:
            problems.append(f"{name!r} expects {_describes(spec)}, got {value!r}")
        return problems
    expected = spec.get("type")
    check = _JSON_TYPE_CHECK.get(expected) if expected else None
    if check is not None and not check(value):
        problems.append(f"{name!r} expects {expected}, got {type(value).__name__}")
        return problems
    if expected in {"integer", "number"}:
        if "minimum" in spec and value < spec["minimum"]:
            problems.append(f"{name!r} must be at least {spec['minimum']}, got {value!r}")
        if "maximum" in spec and value > spec["maximum"]:
            problems.append(f"{name!r} must be at most {spec['maximum']}, got {value!r}")
    elif expected == "string":
        if "minLength" in spec and len(value) < spec["minLength"]:
            problems.append(f"{name!r} needs at least {spec['minLength']} character(s)")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            problems.append(f"{name!r} takes at most {spec['maxLength']} character(s)")
        if "pattern" in spec and re.search(spec["pattern"], value) is None:
            problems.append(f"{name!r} does not match the required pattern")
    elif expected == "array":
        item = spec.get("items", {})
        item_check = _JSON_TYPE_CHECK.get(item.get("type", ""))
        for index, element in enumerate(value):
            if item_check is not None and not item_check(element):
                problems.append(
                    f"{name}[{index}] expects {item['type']}, got {type(element).__name__}"
                )
            elif "enum" in item and element not in item["enum"]:
                problems.append(f"{name}[{index}] must be one of {item['enum']}, got {element!r}")
            elif isinstance(element, str):
                if "minLength" in item and len(element) < item["minLength"]:
                    problems.append(
                        f"{name}[{index}] needs at least {item['minLength']} character(s)"
                    )
                if "maxLength" in item and len(element) > item["maxLength"]:
                    problems.append(
                        f"{name}[{index}] takes at most {item['maxLength']} character(s)"
                    )
        if spec.get("uniqueItems") and len(
            {json.dumps(element, sort_keys=True) for element in value}
        ) < len(value):
            problems.append(f"{name!r} holds the same value more than once")
        if "minItems" in spec and len(value) < spec["minItems"]:
            problems.append(f"{name!r} needs at least {spec['minItems']} value(s)")
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            problems.append(f"{name!r} takes at most {spec['maxItems']} value(s)")
    # Membership is independent of the value's JSON type, so it is checked after
    # the type's own checks rather than as one more branch beside them.
    if "enum" in spec and value not in spec["enum"]:
        problems.append(f"{name!r} must be one of {spec['enum']}, got {value!r}")
    return problems


def _validate(schema: dict[str, Any], args: dict[str, Any], verb: Verb) -> None:
    """Refuse anything the schema does not describe, naming what was wrong."""
    properties = schema.get("properties", {})
    problems: list[str] = []

    for name, value in args.items():
        if name in properties:
            problems += _check_value(name, properties[name], value)
            continue
        reason = verb.refuses.get(name)
        if reason is not None:
            where = "on a background run" if verb.executor in ("job", "spawn") else "here"
            problems.append(f"{name!r} is not accepted {where}: {reason}")
        else:
            problems.append(f"unknown parameter {name!r} for {verb.name!r}")

    for name in schema.get("required", []):
        if name not in args:
            problems.append(f"missing required parameter {name!r}")

    for group in schema.get("x-mutually-exclusive", []):
        present = [n for n in group["parameters"] if n in args]
        if len(present) > 1:
            problems.append(f"{present} are mutually exclusive; pass one")

    if problems:
        raise OpError("invalid_input", "; ".join(problems), {"problems": problems})


# ── argv rendering ───────────────────────────────────────────────────────────


def _tokens(name: str, value: Any) -> list[str]:
    """The argv token(s) *value* becomes, refused if it cannot be one.

    A NUL byte can't appear in a command line at all (execve terminates on
    it), so it's refused here — before a job record exists — rather than
    failing the spawn later with a record nothing can terminalise.
    """
    if isinstance(value, bool):
        return [str(value).lower()]
    token = str(value)
    if "\0" in token:
        raise OpError(
            "invalid_input",
            f"{name!r} contains a NUL byte, which cannot appear in a command line",
        )
    return [token]


def _flag_tokens(name: str, flag: str, value: Any) -> list[str]:
    """One flag and its value, spelled so the value cannot become an option.

    ``--flag=value`` (not ``--flag value``) binds the two into one token, so a
    value like ``--machine`` can't be read as a switch by the parser or by
    anything scanning argv ahead of it.
    """
    token = _tokens(name, value)[0]
    if flag.startswith("--"):
        return [f"{flag}={token}"]
    # A short-only flag has no `=` form (`-f=x` parses as value `=x`); nothing
    # here is short-only today, so this refuses rather than inventing a spelling.
    if token.startswith("-"):
        raise OpError(
            "invalid_input",
            f"{name!r} cannot be passed a value starting with '-' because {flag} "
            "has no long form to bind it to",
        )
    return [flag, token]


def render_argv(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """The CLI tokens *args* becomes, spelled the way the projected parser reads."""
    properties = schema["properties"]
    flags: list[str] = []
    positional: dict[str, Any] = {}

    for name, value in args.items():
        spec = properties[name]
        if spec.get("x-server-owned"):
            continue
        if spec.get("x-positional"):
            positional[name] = value
            continue
        flag = spec["x-flag"]
        if spec.get("x-json-encoded"):
            flags += _flag_tokens(name, flag, json.dumps(value))
        elif spec.get("type") == "boolean":
            # store_false defaults to true; the flag belongs on the line only
            # when the value differs from the parser's own default.
            if bool(value) != bool(spec.get("default", False)):
                flags.append(flag)
        elif spec.get("type") == "array":
            for element in value:
                flags += _flag_tokens(name, flag, element)
        elif value is True and "anyOf" in spec:
            flags.append(flag)
        else:
            flags += _flag_tokens(name, flag, value)

    tail: list[str] = []
    for name in schema.get("x-positional-order", []):
        if name not in positional:
            continue
        value = positional[name]
        if isinstance(value, list):
            tail += [token for element in value for token in _tokens(name, element)]
        else:
            tail += _tokens(name, value)
    if not tail:
        return flags
    # `--` marks everything after it positional to the parser and any scan
    # ahead of it — needed since a prompt may legitimately begin with a dash.
    return [*flags, "--", *tail]


# ── executors ────────────────────────────────────────────────────────────────


_POSITIONAL_LIMITS = {
    "agent": 2,
    "flow": 2,
    "fanout": 2,
    "play": 2,
}


def _refuse_too_many_positionals(
    verb: Verb,
    args: dict[str, Any],
    prompt: str | None,
) -> None:
    """Refuse a positional bucket the receiving CLI would reject, before any
    job record or child process can be created."""
    limit = _POSITIONAL_LIMITS.get(verb.job_kind)
    query = args.get("query") or []
    prompt_count = int(prompt is not None)
    effective_count = len(query) + prompt_count
    if limit is None or effective_count <= limit:
        return
    sources = f"{len(query)} in 'query'"
    if prompt_count:
        sources += " plus a resolved prompt"
    raise OpError(
        "invalid_input",
        f"{verb.name!r} got {effective_count} positional values ({sources}), but accepts "
        f"at most {limit}: [MODEL] PROMPT; pass the prompt exactly once, quote a "
        "multi-word prompt, or use 'prompt' or 'prompt_file' instead",
    )


def _resolve_prompt(args: dict[str, Any]) -> str | None:
    prompt = args.get("prompt")
    prompt_file = args.get("prompt_file")
    if prompt_file is None:
        if prompt is not None and len(prompt) > MAX_SPEC_PROMPT_CHARS:
            raise OpError(
                "invalid_input",
                f"prompt exceeds maximum length of {MAX_SPEC_PROMPT_CHARS} characters",
            )
        return prompt
    if prompt is not None:
        raise OpError("invalid_input", "pass prompt or prompt_file, not both")
    if prompt_file == "-":
        raise OpError("invalid_input", "prompt_file cannot be '-': a detached run has no stdin")
    path = Path(prompt_file).expanduser()
    if not path.is_absolute():
        raise OpError(
            "invalid_input",
            f"prompt_file must be an absolute path, got {prompt_file!r}: the server reads it, "
            "so a relative path would resolve against the server's directory and not the run's",
        )
    try:
        with path.open() as prompt_stream:
            text = prompt_stream.read(MAX_SPEC_PROMPT_CHARS + 1)
    except OSError as exc:
        raise OpError("invalid_input", f"could not read prompt_file {path}: {exc}") from exc
    if len(text) > MAX_SPEC_PROMPT_CHARS:
        raise OpError(
            "invalid_input",
            f"prompt_file content exceeds maximum length of {MAX_SPEC_PROMPT_CHARS} characters",
        )
    if not text.strip():
        raise OpError("invalid_input", f"prompt_file is empty: {path}")
    return text


# Kinds whose command treats naming neither a model nor an agent as a request
# to orchestrate, answered with the default orchestrator profile instead of a
# refusal. Named explicitly (not inferred from absence of the others) so a
# spawning command added later is refused as before until known to match.
_ORCHESTRATING_KINDS = frozenset({"flow", "fanout", "play"})


def _has_model_source(kind: str, args: dict[str, Any], prompt: str | None) -> bool:
    """Whether this submission gives the run any way to obtain a model.

    Answered conservatively (true whenever any source is present) from the
    arguments alone, before anything is spawned — every spawning command
    otherwise refuses within its first second, after the handle has already
    gone back to the caller. A profile/spec-file/playbook each may name a
    model in content this doesn't read, so any of them present settles it.
    Orchestrating kinds get the same treatment via their implicit default
    profile (that default answers only the model question — prompt is checked
    separately below). Every command reads positionals as ``[MODEL] PROMPT``,
    so a lone positional is the prompt; the resolved prompt is counted
    alongside the positional bucket since an agent's prompt goes through
    ``--prompt-file`` instead, leaving that bucket one shorter.
    """
    if args.get("agent") or args.get("resume") or args.get("continue_last"):
        return True
    if kind in _ORCHESTRATING_KINDS:
        return True
    if args.get("file") or args.get("playbook"):
        return True
    query = args.get("query") or []
    bucket = [*query, *([prompt] if prompt is not None else [])]
    return len(bucket) >= 2


_FLOW_MODEL_SOURCES = (
    "pass a model as the first value of 'query' with the prompt after it — a lone "
    "positional is read as the prompt, not as a model — or name a profile with "
    "'agent', a spec with 'file', or a playbook with 'playbook'"
)

# Per-command (not shared) so a remediation never names a source that
# command's argument validation would then refuse. Play runs flow's argv.
_MODEL_SOURCES = {
    "agent": (
        "pass a model as the first value of 'query' with the prompt in 'prompt' or as a "
        "second value — a lone positional is read as the prompt, not as a model — or name "
        "a profile with 'agent'"
    ),
    "fanout": (
        "pass a model as the first value of 'query' with the prompt after it — a lone "
        "positional is read as the prompt, not as a model — or name a profile with 'agent'"
    ),
    "flow": _FLOW_MODEL_SOURCES,
    "play": _FLOW_MODEL_SOURCES,
}

# Fallback when a kind has no per-command entry above. Resuming an existing
# run also satisfies the check but is deliberately not offered here — it
# continues a run that already has a model rather than correcting this one.
_GENERIC_MODEL_SOURCES = (
    ("query", "pass a model as the first value of 'query' with the prompt after it"),
    ("agent", "name a profile with 'agent'"),
    ("file", "name a spec with 'file'"),
    ("playbook", "name a playbook with 'playbook'"),
)


def _unlisted_model_sources(kind: str, verb: Verb, schema: dict[str, Any]) -> str:
    """The remediation for a command kind the sources table does not name.

    Assembled from the intersection of "arguments the check accepts as a model
    source" and "arguments this command's own schema declares", so no name is
    offered on a guess about a command it wasn't written for. An empty
    intersection is reported as such — that command needs its own table entry.
    """
    declared = schema.get("properties", {})
    offered = [text for name, text in _GENERIC_MODEL_SOURCES if name in declared]
    if not offered:
        return (
            f"this server has no model sources recorded for the {kind!r} command and the "
            "command declares no argument this check reads as one, so there is no correction "
            "to name here; the command needs an entry in the server's per-command model "
            "sources before this refusal can point anywhere"
        )
    return (
        f"this server has no model sources recorded for the {kind!r} command, so these are "
        f"the arguments {verb.name!r} declares that this check accepts as a model source, "
        "rather than the ones that command documents — a lone positional is read as the "
        f"prompt, not as a model: {', or '.join(offered)}"
    )


def _refuse_without_model(
    verb: Verb, schema: dict[str, Any], args: dict[str, Any], prompt: str | None
) -> None:
    """Refuse a submission the command would reject on start, naming the fix.

    A run rejected for its arguments dies before reaching the terminal hook, so
    the job stays non-terminal forever and a caller waits on a run already
    over — refusing here instead costs one dictionary lookup.
    """
    kind = verb.job_kind
    if kind is None or _has_model_source(kind, args, prompt):
        return
    sources = _MODEL_SOURCES.get(kind) or _unlisted_model_sources(kind, verb, schema)
    raise OpError(
        "invalid_input",
        f"{verb.name!r} has no model and nothing to supply one, so the run would be "
        f"refused on start and would never reach a terminal status: {sources}",
    )


def _has_prompt_source(kind: str, args: dict[str, Any], prompt: str | None) -> bool:
    """Whether this submission gives an orchestrating run any way to obtain a prompt.

    Asked only of kinds whose model question the default orchestrator profile
    answers, since for every other kind the model check above already covers
    both. The last of ``prompt``/``prompt_file`` (resolved earlier) or a
    positional ``query`` value is tested for truth, not presence — the command
    itself refuses on a falsy prompt, so an empty string must fail this check
    too. Flow (and play, which requires a playbook) also accept a spec file or
    playbook that may carry its own ``prompt`` key this doesn't read, so their
    presence settles the question too; fanout has only the two routes.

    A resume names a saved run, and the command's resume path replays that run's
    checkpoint without reading a prompt (``li o flow --resume``), so a resume is a
    complete submission with or without one. Only flow and play declare it. A written
    graph gives each node its own prompt, so it is a complete submission too; only flow
    takes one.
    """
    if args.get("resume") or args.get("graph"):
        return True
    query = args.get("query") or []
    last_positional = prompt if prompt is not None else (query[-1] if query else None)
    if last_positional:
        return True
    if kind == "fanout":
        return False
    return bool(args.get("file") or args.get("playbook"))


_FLOW_PROMPT_SOURCES = (
    "pass the prompt in 'prompt' or 'prompt_file', or as the last value of 'query', or "
    "name a spec with 'file' or a playbook with 'playbook' that carries one"
)

# Per-command, same reason as _MODEL_SOURCES above.
_PROMPT_SOURCES = {
    "fanout": "pass the prompt in 'prompt' or 'prompt_file', or as the last value of 'query'",
    "flow": _FLOW_PROMPT_SOURCES,
    "play": _FLOW_PROMPT_SOURCES,
}


def _refuse_without_prompt(verb: Verb, args: dict[str, Any], prompt: str | None) -> None:
    """Refuse a promptless orchestrating submission, naming the fix.

    Same class of refusal as the missing-model check, kept separate since the
    two corrections are for different questions and mixing them would give a
    caller with a model and no prompt a fix they can't act on.
    """
    kind = verb.job_kind
    if kind not in _ORCHESTRATING_KINDS or _has_prompt_source(kind, args, prompt):
        return
    raise OpError(
        "invalid_input",
        f"{verb.name!r} has no prompt and nothing to supply one, so the run would be "
        f"refused on start and would never reach a terminal status: {_PROMPT_SOURCES[kind]}",
    )


def _refuse_unknown_profile(
    args: dict[str, Any], cwd: str | None, *, default_name: str | None = None
) -> Any:
    """Refuse a submission naming an agent profile nothing declares.

    Checked with the exact resolver a spawned run itself would use (same
    directories, same cwd, same plugin/ambiguity rules), so this never
    refuses a name the run would have found. Unresolved, the failure used to
    surface only inside the spawned process — after the job record already
    existed with nothing on it to say why — leaving a caller to poll a run
    that died silently on start.

    Returns the loaded profile, or None when the submission names none, so a
    caller can read the authority that profile grants.
    """
    name = args.get("agent") or default_name
    if not name:
        return None
    from lionhub.orchestration.spawn import (
        AgentProfileNotFoundError,
        AmbiguousProfileNameError,
        load_agent_profile,
    )

    with roster._resolving_under(cwd):
        try:
            return load_agent_profile(name)
        # ValueError covers a name the resolver refuses outright, such as one with a path in it
        except (AgentProfileNotFoundError, AmbiguousProfileNameError, ValueError) as exc:
            if (
                isinstance(exc, AgentProfileNotFoundError)
                and default_name
                and not args.get("agent")
            ):
                return None
            raise OpError("invalid_input", str(exc)) from exc


def _refuse_invalid_graph(verb: Verb, args: dict[str, Any], cwd: str | None) -> Any:
    """Load and check a flow's written graph before any job record or directory exists.

    The same loader and checks ``li o flow --graph`` runs (ADR-0069 Amendment 4 G3), with
    profiles resolved from the run's ``cwd``, so one file gets one answer on both paths
    and a refused graph leaves nothing on ``job.list``. Returns the accepted graph, or
    None when the submission names none.
    """
    if verb.job_kind != "flow" or args.get("graph") is None:
        return None
    from lionhub.orchestration.flow.graph import (
        FlowGraphError,
        check_graph,
        graph_flag_refusal,
        load_graph,
    )

    refusal = graph_flag_refusal(
        playbook=args.get("playbook"),
        file=args.get("file"),
        reactive=args.get("reactive"),
        resume=args.get("resume"),
        with_synthesis=args.get("with_synthesis"),
        bare=args.get("bare"),
    )
    if refusal is not None:
        raise OpError("invalid_input", refusal)
    try:
        source = load_graph(args["graph"])
        check_graph(source, cwd=cwd, max_ops=args.get("max_ops") or 0)
    except FlowGraphError as exc:
        raise OpError("invalid_input", str(exc)) from None
    return source


def _resolve_cwd(args: dict[str, Any]) -> str | None:
    """The caller's working directory, resolved the way it will be used.

    ``~`` expansion happens here so every verb taking a ``cwd`` expands it the
    same way — previously a roster read resolved under ``~/project`` while
    submit handed the tilde straight to the spawn, which can't chdir to it.
    Checked before spawning (a missing directory is the caller's to fix, not
    an ``unavailable`` retry-later error) so no run record is minted for a run
    that was never going to start.
    """
    cwd = args.get("cwd")
    if cwd is None:
        return None
    resolved = Path(cwd).expanduser().resolve()
    if not resolved.is_dir():
        raise OpError("invalid_input", f"cwd {cwd!r} is not a directory")
    return str(resolved)


def _playbook_cwd(args: dict[str, Any]) -> str | None:
    """The directory a named playbook resolves under: the run's ``cwd`` when one is given.

    The submitted run starts there and resolves the playbook there, so the schema and
    fingerprint checked here must come from the same file. A ``cwd`` that is not a
    string is left to schema validation, which reports it against the parameter.
    """
    if not isinstance(args.get("cwd"), str):
        return None
    return _resolve_cwd(args)


def _refuse_invalid_playbook_spec(schema: dict[str, Any]) -> None:
    path = schema.get("x-playbook-path")
    if path is None:
        return

    from lionhub.orchestration.flow.spec import load_flow_spec, validate_flow_spec

    name = schema.get("x-playbook")
    try:
        spec = load_flow_spec(path)
    except ValueError as exc:
        raise OpError("invalid_input", f"playbook {name!r} has an invalid spec: {exc}") from exc
    error = validate_flow_spec(spec)
    if error is not None:
        raise OpError("invalid_input", f"playbook {name!r} has an invalid spec: {error}")


def _write_capable(verb: Verb, args: dict[str, Any], profile: Any) -> bool:
    """Whether the run can act with write authority, which gives it its own khive actor.

    An agent run has it when its own ``yolo`` or ``bypass`` argument or its profile sets one; the
    argument reaches the child's argv whatever the profile says. A run that resumes or continues a
    branch reloads the provider settings that branch persisted, ``yolo`` and ``bypass`` included,
    so it counts whatever its arguments say. A flow, fanout or playbook run casts its workers onto
    profiles while it executes, so nothing known at submit time bounds what they may write, and
    such a run always counts.
    """
    if verb.job_kind != "agent":
        return True
    if args.get("resume") or args.get("continue_last"):
        return True
    return any(
        bool(args.get(flag)) or bool(getattr(profile, flag, False)) for flag in ("yolo", "bypass")
    )


def _agent_model_spec(args: dict[str, Any], profile: Any, prompt: str | None) -> Any:
    """The model an agent submission names: the first positional when a prompt follows it, else
    the profile's model. ``None`` when neither names one (a resumed run's model comes from its
    branch)."""
    query = args.get("query") or []
    bucket = [*query, *([prompt] if prompt is not None else [])]
    return bucket[0] if len(bucket) >= 2 else getattr(profile, "model", None)


def _requested_account(
    verb: Verb, args: dict[str, Any], profile: Any, prompt: str | None
) -> tuple[str | None, str | None]:
    """The subscription account an agent submission asks for, as ``(name, provider)``.

    Read the way ``li agent`` reads it: the ``account`` argument wins over the profile's
    ``account``, and a profile ``codex_home`` with neither names no account on a codex run.
    The provider is the account table the model's provider uses (``claude_code`` or
    ``codex``), or None when no model is named here (a resumed run's model comes from its
    branch) or its provider takes no account. Nothing is resolved or read: the run resolves
    the account itself, and refuses one it cannot use. Other job kinds pick accounts per
    worker while they run, so they ask for none here.
    """
    if verb.job_kind != "agent":
        return None, None
    name = args.get("account") or getattr(profile, "account", None)
    if name is None and getattr(profile, "codex_home", None) is None:
        return None, None
    model = _agent_model_spec(args, profile, prompt)
    provider = None
    if isinstance(model, str) and model:
        from lionhub.agent.subscription_accounts import account_provider

        provider = account_provider(model.split("/", 1)[0])
    if name is None:
        # A Codex home with no name: a codex run, on no named account.
        return None, "codex"
    return str(name), provider


def _resolve_hooks_from(
    args: dict[str, Any], profile: Any, prompt: str | None, cwd: str | None
) -> dict[str, Any] | None:
    """Read and plan the hooks file an agent submission names (ADR-0048 Amendment 1).

    Returns what ``jobs.submit`` snapshots, or None when no file is named. The file is read
    once here, under the H2 rules, and planned for the leg's working directory; a guard the
    leg cannot apply refuses the submission (H3, H5). The leg's model is picked the way
    ``_requested_account`` picks it: the first of two positionals, else the profile's model.
    The leg reads its copy and checks the same refusals again when it starts.
    """
    path = args.get("hooks_from")
    fmt = args.get("hooks_from_format")
    carry = args.get("hooks_from_carry")
    if path is None:
        if fmt is not None or carry is not None:
            raise OpError("invalid_input", "hooks_from_format and hooks_from_carry need hooks_from")
        return None
    if args.get("resume") or args.get("continue_last"):
        raise OpError(
            "invalid_input",
            "hooks_from cannot be combined with resume or continue_last: a resumed run keeps "
            "the guards it started with; start a fresh run to carry a hooks file",
        )
    if fmt is None:
        raise OpError("invalid_input", "hooks_from needs hooks_from_format: claude or codex")
    from lionagi._errors import ConfigurationError
    from lionagi.service.connections.registry import EndpointRegistry
    from lionhub.agent.subscription_accounts import account_provider
    from lionhub.hooks.hooks_from import (
        HOOKS_FROM_CARRY,
        HooksFromError,
        carry_refusal,
        hook_command_refusals,
        plan_carry,
        read_hooks_file,
        writable_roots,
    )
    from lionhub.providers.model_spec import CLI_PROVIDERS

    query = args.get("query") or []
    bucket = [*query, *([prompt] if prompt is not None else [])]
    model = bucket[0] if len(bucket) >= 2 else getattr(profile, "model", None)
    provider = model.split("/", 1)[0] if isinstance(model, str) and "/" in model else None
    claude_code = provider is not None and account_provider(provider) == "claude_code"
    api = (
        provider is not None and EndpointRegistry.canonical_provider(provider) not in CLI_PROVIDERS
    )
    carried = list(HOOKS_FROM_CARRY) if carry is None else carry
    leg_cwd = os.path.abspath(cwd) if cwd else os.getcwd()
    try:
        hooks_file = read_hooks_file(path)
        plan = plan_carry(
            hooks_file.document,
            fmt=fmt,
            carry=carried,
            source_path=hooks_file.resolved_path,
            leg_cwd=leg_cwd,
        )
    except HooksFromError as exc:
        raise OpError("invalid_input", str(exc)) from exc
    reason = carry_refusal(plan, claude_code=claude_code, api=api)
    if reason is not None:
        raise OpError("invalid_input", f"cannot submit this leg with hooks_from: {reason}")
    if plan.hooks:
        try:
            refused = hook_command_refusals(plan, leg_cwd=leg_cwd, writable=writable_roots(leg_cwd))
        except ConfigurationError as exc:
            raise OpError(
                "invalid_input", f"cannot submit this leg with hooks_from: {exc}"
            ) from exc
        if refused:
            listed = "; ".join(f"{where}: {why}" for where, why in refused)
            raise OpError(
                "invalid_input",
                f"cannot submit this leg with hooks_from: {len(refused)} hook command(s) "
                f"cannot run: {listed}",
            )
    return {"file": hooks_file, "plan": plan, "format": fmt, "carry": list(carried)}


def _refuse_untrusted_settings_layer(cwd: str | None) -> None:
    """Load the project settings layer the job will load, so an untrusted one refuses here.

    Called only for a gated submission. Anything else wrong with the file is left to the run to
    report, as it is with the gate off.
    """
    from lionagi._settings import load_settings

    try:
        load_settings(project_dir=cwd)
    except lionagi_dir_trust.UntrustedLionagiDirError:
        raise
    except Exception:  # noqa: BLE001, S110 — a malformed file is the run's to report, as before
        pass


def _refuse_untrusted_playbook_agent(
    schema: dict[str, Any], args: dict[str, Any], cwd: str | None
) -> None:
    """Resolve the profile a named playbook's ``agent`` field selects, for a gated submission.

    ``_refuse_unknown_profile`` reads only the submission's own ``agent``. A playbook that names
    one the submission does not override is resolved here the same way, and only a refusal of an
    untrusted directory stops the submission; a name that does not resolve is the run's to
    report, as it is with the gate off.
    """
    path = schema.get("x-playbook-path")
    if path is None or args.get("agent"):
        return
    from lionhub.orchestration.flow.spec import load_flow_spec
    from lionhub.orchestration.spawn import (
        AgentProfileNotFoundError,
        AmbiguousProfileNameError,
        load_agent_profile,
    )

    try:
        name = load_flow_spec(path).get("agent")
    except ValueError:
        return
    if not isinstance(name, str) or not name:
        return
    with roster._resolving_under(cwd):
        try:
            load_agent_profile(name)
        except (AgentProfileNotFoundError, AmbiguousProfileNameError, ValueError):
            return


def _run_spawn(
    verb: Verb, schema: dict[str, Any], args: dict[str, Any], *, trust_gate: bool = False
) -> dict[str, Any]:
    _refuse_invalid_playbook_spec(schema)
    resumes = bool(args.get("resume") or args.get("continue_last"))
    if resumes:
        flow_selector = verb.job_kind in {"flow", "play"} and (
            args.get("exclude_mcp_servers") is not None
            or args.get("mcp_config") is not None
            or bool(args.get("no_mcp_config"))
            or args.get("agent") is not None
        )
        agent_exclusion = verb.job_kind == "agent" and bool(args.get("exclude_mcp_servers"))
        if flow_selector or agent_exclusion:
            raise OpError(
                "invalid_input",
                "A resumed run replays its recorded MCP choice; start a fresh run to change it.",
            )
    prompt = _resolve_prompt(args)
    _refuse_too_many_positionals(verb, args, prompt)
    _refuse_without_model(verb, schema, args, prompt)
    _refuse_without_prompt(verb, args, prompt)
    cwd = _resolve_cwd(args)
    if trust_gate:
        _refuse_untrusted_settings_layer(cwd)
        _refuse_untrusted_playbook_agent(schema, args, cwd)
    default_name = None
    if (
        verb.job_kind in {"flow", "fanout"}
        and not args.get("file")
        and not args.get("playbook")
        and not args.get("resume")
    ):
        from lionhub.serve.cli.orchestrate import _resolve_model_and_prompt
        from lionhub.serve.cli.orchestrate._orchestration import _select_orchestrator_agent

        query = [*(args.get("query") or []), *([prompt] if prompt is not None else [])]
        resolved = _resolve_model_and_prompt(query)
        assert resolved is not None  # the positional limit was checked above
        selected, defaulted = _select_orchestrator_agent(args.get("agent"), resolved[0])
        if defaulted:
            default_name = selected
    profile = _refuse_unknown_profile(args, cwd, default_name=default_name)
    graph_source = _refuse_invalid_graph(verb, args, cwd)
    write_capable = _write_capable(verb, args, profile)
    account, account_provider = _requested_account(verb, args, profile, prompt)
    hooks_from = _resolve_hooks_from(args, profile, prompt, cwd)
    assert verb.job_kind is not None
    resume_id = None
    try:
        if verb.job_kind == "agent" and args.get("resume"):
            resume_id = jobs._resolve_agent_resume_id(args["resume"])
        flags = render_argv(schema, args)
        result = jobs.submit(
            verb.job_kind,
            flags,
            prompt=prompt,
            cwd=cwd,
            label=args.get("label") or args.get("playbook"),
            notify_command=args.get("notify_command"),
            notify_target=args.get("notify_seat"),
            notify_sender=args.get("notify_sender"),
            mcp_config=args.get("mcp_config"),
            no_mcp_config=bool(args.get("no_mcp_config")),
            exclude_mcp_servers=args.get("exclude_mcp_servers"),
            write_capable=write_capable,
            profile=profile,
            account=account,
            account_provider=account_provider,
            # Read for the profile's require_hooks before the run is recorded; a resumed run's
            # model comes from its branch, so its launch answers instead.
            model=(
                _agent_model_spec(args, profile, prompt)
                if verb.job_kind == "agent" and not resumes
                else None
            ),
            **({"hooks_from": hooks_from} if hooks_from is not None else {}),
            **({"replay_mcp_choice": True} if resumes else {}),
            **({"graph_source": graph_source} if graph_source is not None else {}),
            **({"lionagi_dir_gate": True} if trust_gate else {}),
        )
    except NestedAgentRefusedError as exc:
        # Well-formed, and refused for where the caller runs, which no retry changes.
        raise OpError("conflict", str(exc)) from exc
    except jobs.SubmittingActorError as exc:
        raise OpError("unavailable", str(exc)) from exc
    except jobs.ChildActorError as exc:
        raise OpError("invalid_input", str(exc)) from exc
    except jobs.NotifySenderMismatchError as exc:
        raise OpError("invalid_input", str(exc)) from exc
    except jobs.NotifySenderResolutionError as exc:
        raise OpError("unavailable", str(exc)) from exc
    except jobs.SubmitInputError as exc:
        # A submission this spawn refuses on its own terms (an exclusion it
        # cannot honour) is the caller's input to correct, not a server outage.
        raise OpError("invalid_input", str(exc)) from exc
    if verb.job_kind == "agent":
        result["resume_id"] = resume_id
    fingerprint = schema.get("x-playbook-fingerprint")
    if fingerprint is not None:
        result["playbook"] = schema.get("x-playbook")
        result["playbook_fingerprint"] = fingerprint
        declared = args.get("playbook_fingerprint")
        if declared is not None:
            result["playbook_fingerprint_declared"] = declared
            result["playbook_fingerprint_changed"] = declared != fingerprint
    return result


def _server_info() -> dict[str, Any]:
    from lionagi.version import __version__
    from lionhub.serve.cli.machine import CONTRACT_VERSION
    from lionhub.serve.diagnostics import code_identity

    return {
        "lionagi_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "code_identity": code_identity(),
        "started_at": _STARTED_AT,
        "uptime_seconds": round(time.time() - _STARTED_MONOTONIC, 3),
        "tool_count": 1,
        "verbs": sorted(VERBS),
        "verb_count": len(VERBS),
        "absent_verb_count": len(ABSENT),
        "pid": os.getpid(),
    }


_JOB_EXECUTORS = {
    "job.status": lambda a: jobs.status(a["run_id"]),
    "job.output": lambda a: jobs.output(a["run_id"], tail_chars=a.get("tail_chars", 20000)),
    "job.kill": lambda a: jobs.kill(a["run_id"]),
    "job.list": lambda a: {"jobs": jobs.list_jobs(a.get("limit", 50), a.get("status"))},
    "server.info": lambda a: _server_info(),
    "account.list": lambda a: _account_list(),
}


def _account_list() -> dict[str, Any]:
    from lionhub.agent.subscription_accounts import SubscriptionAccountError

    try:
        return accounts.account_list()
    except SubscriptionAccountError as exc:
        # The machine's account configuration, which no change to the call corrects.
        raise OpError("unavailable", str(exc)) from exc


async def _run_job(verb: Verb, args: dict[str, Any]) -> dict[str, Any]:
    if verb.name == "job.wait":
        return await jobs.wait(
            args["run_ids"],
            max_wait=args.get("max_wait", 60.0),
            poll_interval=args.get("poll_interval", 1.0),
        )
    result = _JOB_EXECUTORS[verb.name](args)
    if verb.name == "job.status" and args.get("detail"):
        from . import _run_detail

        result["detail"] = await _run_detail.build_run_detail(args["run_id"])
    return result


def _run_roster(verb: Verb, args: dict[str, Any]) -> dict[str, Any]:
    """Answer a roster verb through the resolver a run itself uses.

    Called in-process (not spawned) since it's the same profile loader
    `li agent` calls — nothing to drift from. `cwd` is taken and checked
    explicitly since the subprocess boundary would otherwise supply it for free.
    """
    cwd = _resolve_cwd(args)
    try:
        if verb.name == "profile.list":
            return roster.profile_list(cwd=cwd, names=args.get("names"), fields=args.get("fields"))
        return roster.profile_show(args["name"], cwd=cwd)
    except FileNotFoundError as exc:
        raise OpError("not_found", str(exc)) from exc
    except ValueError as exc:
        raise OpError("invalid_input", str(exc)) from exc


def _machine_environment_error(
    cli_path: str, completed: subprocess.CompletedProcess[bytes]
) -> OpError:
    """Classify a startup refusal from its exit status and keep usable diagnostics."""
    from lionhub.serve.cli._machine_envelope import _CONTRACT_VERSION
    from lionhub.state.engine import mask_credentials

    stderr = mask_credentials(completed.stderr.decode("utf-8", "replace"))[-_MACHINE_STDERR_TAIL:]
    detail = {"exit_code": completed.returncode, "stderr": stderr}
    fallback = OpError("unavailable", f"`{cli_path}` could not start in this environment", detail)
    if not completed.stdout or len(completed.stdout) > MACHINE_OUTPUT_LIMIT:
        return fallback
    try:
        envelope = json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, RecursionError):
        return fallback
    if not isinstance(envelope, dict) or envelope.get("ok") is not False:
        return fallback
    version = envelope.get("contract_version")
    if type(version) is not int or version != _CONTRACT_VERSION:
        return fallback
    if "data" not in envelope or envelope["data"] is not None:
        return fallback
    error = envelope.get("error")
    if not isinstance(error, dict) or error.get("kind") != "unavailable":
        return fallback
    message = error.get("message")
    envelope_detail = error.get("detail")
    if not isinstance(message, str) or (
        envelope_detail is not None and not isinstance(envelope_detail, dict)
    ):
        return fallback
    envelope_detail = envelope_detail or {}
    pending = [envelope_detail]
    while pending:
        container = pending.pop()
        items = container.items() if isinstance(container, dict) else enumerate(container)
        for key, value in items:
            if isinstance(value, str):
                container[key] = mask_credentials(value)
            elif isinstance(value, (dict, list)):
                pending.append(value)
    return OpError("unavailable", mask_credentials(message), {**envelope_detail, **detail})


def _run_machine(verb: Verb, schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Run the verb's CLI path as a subprocess and return its versioned envelope.

    Spawned rather than called in-process: an in-process route would carry its
    own parser defaults/settings/project resolution and drift from the CLI
    without either one being wrong enough to notice.
    """
    assert verb.cli_path is not None
    argv = [*config.li_command(), *verb.cli_path.split(), "--machine", *render_argv(schema, args)]
    try:
        completed = subprocess.run(  # noqa: S603 — resolved li command plus projected flags, no shell
            argv,
            capture_output=True,
            timeout=MACHINE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpError(
            "unavailable", f"`{verb.cli_path}` did not answer within {MACHINE_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise OpError("internal", f"could not launch `{verb.cli_path}`: {exc}") from exc

    if completed.returncode == EXIT_CODE_ENVIRONMENT_ERROR:
        raise _machine_environment_error(verb.cli_path, completed)
    if len(completed.stdout) > MACHINE_OUTPUT_LIMIT:
        raise OpError(
            "internal",
            f"`{verb.cli_path}` wrote {len(completed.stdout)} bytes on the result channel, "
            f"over the {MACHINE_OUTPUT_LIMIT} byte limit",
        )

    text = completed.stdout.decode("utf-8", "replace").strip()
    stderr_tail = completed.stderr.decode("utf-8", "replace")[-_MACHINE_STDERR_TAIL:]
    if not text:
        raise OpError(
            "internal",
            f"`{verb.cli_path}` exited {completed.returncode} with no machine result",
            {"stderr": stderr_tail},
        )
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OpError(
            "internal",
            f"`{verb.cli_path}` wrote something other than one JSON value: {exc}",
            {"stderr": stderr_tail},
        ) from exc

    if not isinstance(envelope, dict) or "ok" not in envelope or "contract_version" not in envelope:
        raise OpError("internal", f"`{verb.cli_path}` wrote a value that is not a result envelope")
    if not envelope["ok"]:
        error = envelope.get("error") or {}
        raise OpError(
            error.get("kind", "internal"),
            error.get("message", "the command refused without saying why"),
            error.get("detail"),
        )
    if completed.returncode != 0:
        # A success envelope beside a non-zero exit is two channels
        # contradicting each other, not a success reported twice.
        raise OpError(
            "internal",
            f"`{verb.cli_path}` reported success but exited {completed.returncode}",
            {"stderr": stderr_tail},
        )
    return {"contract_version": envelope["contract_version"], "data": envelope["data"]}


# ── the request entry point ──────────────────────────────────────────────────


def _help(target: Any) -> dict[str, Any]:
    if target is True:
        return catalog()
    playbook: str | None = None
    cwd: str | None = None
    if isinstance(target, dict):
        name = target.get("verb")
        playbook = target.get("playbook")
        unknown = sorted(set(target) - {"verb", "playbook", "cwd"})
        if unknown:
            raise ValueError(f"help object takes 'verb', 'playbook' and 'cwd'; got {unknown}")
        if not isinstance(name, str):
            raise ValueError("help object needs a 'verb' string")
        if "cwd" in target:
            raw_cwd = target["cwd"]
            if playbook is None:
                raise ValueError(
                    "help's 'cwd' names the directory a playbook resolves under; "
                    "pass it together with 'playbook'"
                )
            if not isinstance(raw_cwd, str):
                raise ValueError(f"help's 'cwd' is a directory path, got {raw_cwd!r}")
            try:
                cwd = _resolve_cwd({"cwd": raw_cwd})
            except OpError as exc:
                raise ValueError(str(exc)) from exc
    else:
        name = target
        if not isinstance(name, str):
            raise ValueError("help takes true, a verb name, or a {'verb': ...} object")
    verb = VERBS.get(name)
    if verb is None:
        return _unknown_verb(name)
    if playbook is not None and not verb.playbook_aware:
        raise ValueError(f"{name!r} takes no playbook")
    try:
        with roster._resolving_under(cwd):
            schema = verb_schema(verb, playbook=playbook)
    except projection.SchemaProjectionError as exc:
        raise ValueError(f"{name!r} has no describable schema: {exc}") from exc
    answer: dict[str, Any] = {"verb": name, "schema": schema}
    if verb.executor == "spawn":
        _describe_fingerprint(answer, verb, schema, playbook=playbook)
        answer["help_usage"] = (
            _SPAWN_RETRY_RULE
            if "schema_fingerprint" in answer
            else _FINGERPRINT_NEEDS_ARGUMENT + _SPAWN_RETRY_RULE
        )
    return answer


def _unknown_verb(name: str) -> dict[str, Any]:
    for absent in ABSENT:
        if absent.name == name:
            return {
                "verb": name,
                "available": False,
                "summary": absent.summary,
                "reason": absent.reason,
            }
    raise ValueError(
        f"no such verb {name!r}; ask for the catalog with help=true "
        f"({len(VERBS)} available, {len(ABSENT)} named and unavailable)"
    )


def _op_error(op: str, exc: OpError, schema: dict[str, Any] | None) -> dict[str, Any]:
    error: dict[str, Any] = {"kind": exc.kind, "message": str(exc)}
    if exc.detail is not None:
        error["detail"] = exc.detail
    if schema is not None:
        error["schema"] = schema
    return {"ok": False, "op": op, "error": error}


def _dedupe_error_schemas(results: list[dict[str, Any]]) -> None:
    """Keep one inline copy of each judged-against schema per response.

    The first refusal judged against a schema carries it whole, so one mistake
    still costs one request/reply cycle; every later refusal against the same schema
    carries ``schema_ref`` — the ``op_index`` of the entry holding the copy and
    the schema's fingerprint — instead of repeating it, so a batch of N
    identical mistakes does not carry N copies of an identical multi-kilobyte
    schema.
    """
    carriers: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(results):
        error = entry.get("error")
        if isinstance(error, dict) and isinstance(error.get("schema"), dict):
            carriers.append((index, error))
    if len(carriers) < 2:
        # Fingerprints exist here to compare copies; with at most one carrier
        # there is nothing to compare, so nothing gets hashed.
        return
    seen: dict[str, int] = {}
    for index, error in carriers:
        fingerprint = _error_schema_fingerprint(error)
        first = seen.setdefault(fingerprint, index)
        if first != index:
            del error["schema"]
            error["schema_ref"] = {"op_index": first, "schema_fingerprint": fingerprint}


def _error_schema_fingerprint(error: dict[str, Any]) -> str:
    # A stale_schema refusal's remedy already carries the fingerprint of the
    # exact schema attached to the error, computed once at the gate; hashing
    # the same content again here would double the cost of the batch this
    # pass exists to shrink.
    if error.get("kind") == "stale_schema":
        detail = error.get("detail")
        if isinstance(detail, dict):
            fingerprint = detail.get("schema_fingerprint")
            if isinstance(fingerprint, str):
                return fingerprint
    return schema_fingerprint(error["schema"])


async def _run_one(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return _op_error(
            "?", OpError("invalid_input", f"each op is an object, got {type(entry).__name__}"), None
        )
    unknown_keys = sorted(set(entry) - {"op", "args", "schema_fingerprint"})
    name = entry.get("op")
    if name is None:
        return _op_error(
            "?",
            OpError(
                "invalid_input",
                f"each op names its verb under 'op'; got the keys {sorted(map(str, entry))}",
            ),
            None,
        )
    if not isinstance(name, str):
        return _op_error("?", OpError("invalid_input", f"op must be a string, got {name!r}"), None)
    if unknown_keys:
        return _op_error(
            name,
            OpError(
                "invalid_input",
                f"an op takes 'op', 'args' and 'schema_fingerprint'; got {unknown_keys}",
            ),
            None,
        )

    verb = VERBS.get(name)
    if verb is None:
        try:
            absent = _unknown_verb(name)
        except ValueError as exc:
            return _op_error(name, OpError("not_found", str(exc)), None)
        return _op_error(
            name, OpError("unavailable", absent["reason"], {"summary": absent["summary"]}), None
        )

    # Absent/null mean "no arguments"; `or {}` would also collapse a wrongly
    # typed falsy value (empty list/string, false) into that same case.
    args = entry.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _op_error(
            name, OpError("invalid_input", f"args is an object, got {type(args).__name__}"), None
        )

    schema: dict[str, Any] | None = None
    try:
        playbook = args.get("playbook") if verb.playbook_aware else None
        playbook = playbook if isinstance(playbook, str) else None
        # A named playbook resolves where the run will, under the submitted cwd (the
        # spawned child starts there), not under this server's own working directory.
        playbook_cwd = _playbook_cwd(args) if playbook is not None else None
        # With lionagi_dir_trust.gate set to mcp, a submission resolves project .lionagi files
        # the way its gated job will, so one project gets one answer at both (ADR-0088
        # Amendment 2).
        trust_gate = verb.executor == "spawn" and lionagi_dir_trust.gate_setting() == "mcp"
        with roster._resolving_under(playbook_cwd), lionagi_dir_trust.enforced(trust_gate):
            schema = verb_schema(verb, playbook=playbook)
            if verb.executor == "spawn":
                _require_fingerprint(
                    name,
                    verb,
                    schema,
                    entry.get("schema_fingerprint"),
                    playbook=playbook,
                    cwd=playbook_cwd,
                )
        _validate(schema, args, verb)
        if verb.executor == "spawn":
            with lionagi_dir_trust.enforced(trust_gate):
                result = _run_spawn(verb, schema, args, trust_gate=trust_gate)
        elif verb.executor == "job":
            result = await _run_job(verb, args)
        elif verb.executor == "roster":
            result = _run_roster(verb, args)
        else:
            result = _run_machine(verb, schema, args)
    except OpError as exc:
        return _op_error(name, exc, schema)
    except lionagi_dir_trust.UntrustedLionagiDirError as exc:
        return _op_error(name, OpError("invalid_input", str(exc)), schema)
    except lionagi_dir_trust.LionagiDirTrustSettingError as exc:
        return _op_error(name, OpError("unavailable", str(exc)), schema)
    except jobs.SpawnError as exc:
        # `unavailable`, not `invalid_input`: arguments were already accepted
        # by the schema; what failed is this machine's ability to start a
        # process. run_id rides along since a record was written before failure.
        return _op_error(
            name,
            OpError("unavailable", str(exc), detail={"run_id": exc.run_id}),
            schema,
        )
    except projection.SchemaProjectionError as exc:
        return _op_error(name, OpError("unavailable", str(exc)), None)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return _op_error(name, OpError("internal", f"{type(exc).__name__}: {exc}"), schema)
    return {"ok": True, "op": name, "result": result}


async def request(ops: list[dict[str, Any]] | None = None, help: Any = None) -> dict[str, Any]:  # noqa: A002 — `help` is the parameter name the surface advertises
    """Run a batch of ops, or answer a help request. Never raises for one bad op.

    A failure to import this server's own code, or own source that no longer parses, ends
    the process instead of answering (``_stop_if_own_module_missing``).
    """
    try:
        return await _request(ops, help)
    except (ImportError, SyntaxError) as exc:
        _stop_if_own_module_missing(exc)
        raise


async def _request(ops: list[dict[str, Any]] | None, help: Any) -> dict[str, Any]:  # noqa: A002
    # Shape checked before the help branch, or a malformed `ops` is judged on
    # truthiness (an empty dict would slip past as "no ops").
    if ops is not None and not isinstance(ops, list):
        raise ValueError(f"ops is a list of {{op, args}} objects, got {type(ops).__name__}")
    if help is not None and help is not False:
        if ops:
            raise ValueError(
                "help and ops cannot be combined in one call: help returns the catalog "
                "and ops returns one result per op, which are different shapes. Send the "
                "help request and the ops as two separate calls."
            )
        return _help(help)
    if ops is None:
        raise ValueError(
            "pass ops, or help=true for the catalog. This tool dispatches namespaced "
            "verbs; help=true lists them with their required parameters."
        )
    if not ops:
        raise ValueError("ops is empty; pass at least one {op, args} object")
    if len(ops) > MAX_OPS:
        raise ValueError(f"ops carries {len(ops)} entries, over the maximum of {MAX_OPS}")

    results = [await _run_one(entry) for entry in ops]
    _dedupe_error_schemas(results)
    return {
        "status": "success" if all(r["ok"] for r in results) else "partial",
        "ops": results,
    }
