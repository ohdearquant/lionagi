# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Dispatch for the single tool: validate an op, run it, envelope the result.

The advertised tool describes ``ops`` and ``help`` and nothing else, so a
caller's only route to a verb's parameters is to ask for them. That makes two
things load-bearing here.

The catalog carries a signature — the verb, what it requires, one line of
summary — rather than a bare name, because a list of names tells a caller what
exists and not how to call it, which forces a second round-trip before any first
call.

A rejected op comes back with the schema it was judged against. Validation is
closed, so a misspelled parameter is refused by name; pairing that refusal with
the schema means the first mistake costs one round-trip and teaches the shape,
instead of costing a rejection and then a separate help call.

Every schema is generated from the parser the CLI itself builds, at the moment it
is asked for. Nothing here keeps a copy of a command's parameters, so a flag that
moves in the CLI moves here with it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, jobs, projection, roster
from .verbs import (
    ABSENT,
    MAX_OPS,
    SYNONYM_REMOVAL_DATE,
    VERBS,
    Verb,
    resolve,
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

_STARTED_AT = datetime.now(timezone.utc).isoformat()
_STARTED_MONOTONIC = time.time()


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

    "Enough" is measured against the gate the call actually meets. A verb whose
    ops must carry a ``schema_fingerprint`` gets that fingerprint here, because
    an entry that lists a verb's parameters and withholds the one thing without
    which the call is refused describes a call that cannot be made. The schema is
    built anyway to read ``required`` off it, so the fingerprint is a hash of a
    document already in hand and costs no extra work.

    Where the schema depends on an argument, no fingerprint is quoted: the one
    for the argument-free schema would be a value that never matches. The entry
    names the parameter it varies with instead, so the caller knows to ask help
    for that spelling rather than to retry with a stale string.
    """
    entries: list[dict[str, Any]] = []
    for verb in VERBS.values():
        entry: dict[str, Any] = {"verb": verb.name, "available": True, "summary": verb.summary}
        try:
            schema = verb_schema(verb)
            entry["required"] = list(schema.get("required", []))
            unenforced = list(schema.get("x-required-unenforced", []))
            if unenforced:
                # Named apart from `required` because the parser will not refuse a
                # call that omits these, and the schema may offer another way to
                # supply the same thing. Reporting them inside `required` would
                # make the schema and what is admitted two different contracts.
                entry["required_unenforced"] = unenforced
            if verb.executor == "spawn":
                _describe_fingerprint(entry, verb, schema)
        except Exception as exc:  # noqa: BLE001 — one unreadable parser must not hide the rest
            entry["available"] = False
            entry["reason"] = f"schema generation failed: {type(exc).__name__}: {exc}"
        entries.append(entry)
    for absent in ABSENT:
        entries.append(
            {
                "verb": absent.name,
                "available": False,
                "summary": absent.summary,
                "reason": absent.reason,
            }
        )
    available = [e for e in entries if e["available"]]
    return {
        "verbs": entries,
        "verb_count": len(entries),
        "available_count": len(available),
        "max_ops": MAX_OPS,
        "help_usage": (
            "help=true returns this catalog; help='<verb>' returns that verb's full "
            "parameter schema; help={'verb': '<verb>', 'playbook': '<name>'} resolves a "
            "playbook's own declared arguments into the schema. An entry carrying a "
            "schema_fingerprint names a verb whose ops must repeat it: "
            "{'op': 'agent.submit', 'args': {...}, 'schema_fingerprint': '<from this entry>'}. "
            "An entry carrying schema_fingerprint_varies_with names the parameters that "
            "change the schema: pass one of them and the fingerprint to send is the one "
            "help returns for that spelling, not the one quoted here. "
            "required_unenforced names parameters the parser will not refuse a call for "
            "omitting but the command cannot do its work without."
        ),
        "synonyms_removed_after": SYNONYM_REMOVAL_DATE,
    }


# ── schema fingerprint ───────────────────────────────────────────────────────


def schema_fingerprint(schema: dict[str, Any]) -> str:
    """A short digest of a verb's schema, stable across processes.

    Derived from the schema's own content, so it changes exactly when the
    parameters a caller would have read change, and not when anything else about
    the build does.
    """
    body = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _describe_fingerprint(entry: dict[str, Any], verb: Verb, schema: dict[str, Any]) -> None:
    """Say what a fingerprint-gated verb's ops have to carry.

    A playbook-aware verb is projected again once a playbook is named, so its
    fingerprint is a function of that argument. When the playbook is optional the
    argument-free schema is a real call and its fingerprint is quoted; when the
    verb requires a playbook there is no such call, so quoting anything would
    hand the caller a string that is guaranteed to be refused.
    """
    varies = ["playbook"] if verb.playbook_aware else []
    if varies:
        entry["schema_fingerprint_varies_with"] = varies
    if any(name in verb.requires for name in varies):
        return
    entry["schema_fingerprint"] = schema_fingerprint(schema)


def _require_fingerprint(name: str, verb: Verb, schema: dict[str, Any], supplied: Any) -> None:
    """Spawn ops carry the fingerprint targeted help returned for them.

    What this establishes is agreement: the schema the caller validated against is
    the schema about to run. For a caller that fetched it, it also means the
    parameters were in that caller's context first, which is the whole reason a
    wide spawn surface is discoverable at all rather than merely documented. It
    does not establish that in general — a fingerprint is a string and can be
    inherited from someone who did read the schema.

    The refusal carries its own remedy, because a rejection that only says
    "stale" strands exactly the caller this exists to help.
    """
    current = schema_fingerprint(schema)
    if supplied == current:
        return
    remedy = {
        "help": {"verb": name} if verb.playbook_aware else name,
        "schema_fingerprint": current,
    }
    # Where the key goes is the part a caller gets wrong: put it inside `args`
    # and it is simply not read, so this refusal repeats verbatim and the
    # failure reads as idempotent rather than as a misplaced key. Spelling the
    # whole op is the only form of the instruction that cannot be misread.
    shape = f"{{'op': {name!r}, 'args': {{...}}, 'schema_fingerprint': {current!r}}}"
    if supplied is None:
        raise OpError(
            "stale_schema",
            f"{name!r} needs the schema_fingerprint that help returns for it; ask for "
            f"help={name!r} and send the fingerprint as a sibling of 'args', not a "
            f"member of it: {shape}",
            remedy,
        )
    raise OpError(
        "stale_schema",
        f"{name!r} was called with schema_fingerprint {supplied!r}, which is not the "
        f"current {current!r}; the parameters changed since that schema was read. "
        f"Re-read help={name!r} and send: {shape}",
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
}


def _describes(spec: dict[str, Any]) -> str:
    """How a branch of an ``anyOf`` reads in a refusal."""
    if "const" in spec:
        return f"the literal {json.dumps(spec['const'])}"
    return str(spec.get("type", "a value the schema describes"))


def _check_value(name: str, spec: dict[str, Any], value: Any) -> list[str]:
    problems: list[str] = []
    if "anyOf" in spec:
        # A flag that is legal bare: either its value, or true for the bare form.
        # Each branch is checked, and the value has to satisfy one of them —
        # accepting whatever arrives because the schema has two shapes would make
        # the advertised schema and what is admitted two different contracts.
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
    if expected == "array":
        item = spec.get("items", {})
        item_check = _JSON_TYPE_CHECK.get(item.get("type", ""))
        for index, element in enumerate(value):
            if item_check is not None and not item_check(element):
                problems.append(
                    f"{name}[{index}] expects {item['type']}, got {type(element).__name__}"
                )
            elif "enum" in item and element not in item["enum"]:
                problems.append(f"{name}[{index}] must be one of {item['enum']}, got {element!r}")
        if "minItems" in spec and len(value) < spec["minItems"]:
            problems.append(f"{name!r} needs at least {spec['minItems']} value(s)")
        if "maxItems" in spec and len(value) > spec["maxItems"]:
            problems.append(f"{name!r} takes at most {spec['maxItems']} value(s)")
    elif "enum" in spec and value not in spec["enum"]:
        problems.append(f"{name!r} must be one of {spec['enum']}, got {value!r}")
    return problems


def _validate(schema: dict[str, Any], args: dict[str, Any], verb: Verb) -> None:
    """Refuse anything the schema does not describe, naming what was wrong.

    Silently ignoring an unrecognized argument turns a typo into a call that
    succeeds while doing something other than what was asked, so an unknown name
    is echoed back rather than dropped.
    """
    properties = schema.get("properties", {})
    problems: list[str] = []

    for name, value in args.items():
        if name in properties:
            problems += _check_value(name, properties[name], value)
            continue
        reason = verb.refuses.get(name)
        if reason is not None:
            # Every refusal used to be on a spawn verb, so the message could say
            # why in general terms: nobody is attached to the terminal. A refusal
            # on a synchronous verb has nothing to do with detachment, and saying
            # so would send the caller looking for a background run that is not
            # what they invoked.
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

    A command line is a list of NUL-terminated strings all the way down to
    ``execve``, so a string carrying a NUL is not a value the platform can pass
    at all. Refusing it here — before any record of the run exists — is the
    difference between the caller learning its input was wrong and the spawn
    failing later with a job record nothing can terminalise.
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

    ``--flag value`` puts a caller's string in argv's option position: a value of
    ``--machine`` is then read as a switch by the parser, and by anything that
    scans argv ahead of the parser. ``--flag=value`` binds the two into a single
    token, which no scan can split back apart.
    """
    token = _tokens(name, value)[0]
    if flag.startswith("--"):
        return [f"{flag}={token}"]
    # A short-only flag has no `=` form: `-f=x` parses as the value `=x`. Nothing
    # on the surface is short-only today, so this refuses rather than inventing a
    # spelling for a case that would need its own decision.
    if token.startswith("-"):
        raise OpError(
            "invalid_input",
            f"{name!r} cannot be passed a value starting with '-' because {flag} "
            "has no long form to bind it to",
        )
    return [flag, token]


def render_argv(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """The CLI tokens *args* becomes, spelled the way the projected parser reads.

    Every token comes from the schema the parser produced — its flag string, its
    aliases, its position — so a flag that is renamed in the CLI is renamed here
    without an edit.
    """
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
            # The parser decodes this flag's single token from JSON, so the
            # value the caller sent has to reach it encoded.
            flags += _flag_tokens(name, flag, json.dumps(value))
        elif spec.get("type") == "boolean":
            # A store_false action defaults to true and its flag turns it off, so
            # the flag belongs on the line exactly when the value differs from
            # what the parser would have chosen on its own.
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
    # Everything after `--` is a positional, to the parser and to every scan that
    # runs ahead of it. Without it a positional whose text begins with a dash is
    # read as an option, and a prompt is exactly the kind of value that legitimately
    # begins with one.
    return [*flags, "--", *tail]


# ── executors ────────────────────────────────────────────────────────────────


def _resolve_prompt(args: dict[str, Any]) -> str | None:
    prompt = args.get("prompt")
    prompt_file = args.get("prompt_file")
    if prompt_file is None:
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
        text = path.read_text()
    except OSError as exc:
        raise OpError("invalid_input", f"could not read prompt_file {path}: {exc}") from exc
    if not text.strip():
        raise OpError("invalid_input", f"prompt_file is empty: {path}")
    return text


def _run_spawn(verb: Verb, schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    prompt = _resolve_prompt(args)
    flags = render_argv(schema, args)
    assert verb.job_kind is not None
    result = jobs.submit(
        verb.job_kind,
        flags,
        prompt=prompt,
        cwd=args.get("cwd"),
        label=args.get("label") or args.get("playbook"),
        notify_command=args.get("notify_command"),
        notify_target=args.get("notify_seat"),
        notify_sender=args.get("notify_sender"),
        mcp_config=args.get("mcp_config"),
        no_mcp_config=bool(args.get("no_mcp_config")),
    )
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
    from lionagi.cli.machine import CONTRACT_VERSION
    from lionagi.version import __version__

    return {
        "lionagi_version": __version__,
        "contract_version": CONTRACT_VERSION,
        "started_at": _STARTED_AT,
        "uptime_seconds": round(time.time() - _STARTED_MONOTONIC, 3),
        "tool_count": 1,
        "verbs": sorted(VERBS),
        "verb_count": len(VERBS),
        "absent_verb_count": len(ABSENT),
        "synonyms_removed_after": SYNONYM_REMOVAL_DATE,
        "pid": os.getpid(),
    }


_JOB_EXECUTORS = {
    "job.status": lambda a: jobs.status(a["run_id"]),
    "job.output": lambda a: jobs.output(a["run_id"], tail_chars=a.get("tail_chars", 20000)),
    "job.kill": lambda a: jobs.kill(a["run_id"]),
    "job.list": lambda a: {"jobs": jobs.list_jobs(a.get("limit", 50), a.get("status"))},
    "server.info": lambda a: _server_info(),
}


async def _run_job(verb: Verb, args: dict[str, Any]) -> dict[str, Any]:
    if verb.name == "job.wait":
        return await jobs.wait(
            args["run_ids"],
            max_wait=args.get("max_wait", 60.0),
            poll_interval=args.get("poll_interval", 1.0),
        )
    return _JOB_EXECUTORS[verb.name](args)


def _run_roster(verb: Verb, args: dict[str, Any]) -> dict[str, Any]:
    """Answer a roster verb through the resolver a run itself uses.

    Called in this process rather than spawned, because there is nothing here to
    drift from: the profile loader is one function, and it is the same one the
    spawned `li agent` calls. The one thing the subprocess boundary would have
    carried for free is the working directory, so that is taken as an argument
    and checked here — a run submitted with a bad cwd fails at spawn, and a
    roster read of the same cwd should not answer for a different directory.
    """
    cwd = args.get("cwd")
    if cwd is not None and not Path(cwd).expanduser().is_dir():
        raise OpError("invalid_input", f"cwd {cwd!r} is not a directory")
    try:
        if verb.name == "profile.list":
            return roster.profile_list(cwd=cwd)
        return roster.profile_show(args["name"], cwd=cwd)
    except FileNotFoundError as exc:
        # The loader's own miss already names every available profile; re-listing
        # them here would be a second answer that could disagree with it.
        raise OpError("not_found", str(exc)) from exc
    except ValueError as exc:
        raise OpError("invalid_input", str(exc)) from exc


def _run_machine(verb: Verb, schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Run the verb's CLI path as a subprocess and return its versioned envelope.

    The command is spawned rather than called in this process on purpose: a
    second in-process route would carry its own parser defaults, settings and
    project resolution, and the two would drift without either one being wrong
    enough to notice.
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

    if len(completed.stdout) > MACHINE_OUTPUT_LIMIT:
        raise OpError(
            "internal",
            f"`{verb.cli_path}` wrote {len(completed.stdout)} bytes on the result channel, "
            f"over the {MACHINE_OUTPUT_LIMIT} byte limit",
        )

    text = completed.stdout.decode("utf-8", "replace").strip()
    stderr_tail = completed.stderr.decode("utf-8", "replace")[-2000:]
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
        # The envelope is the authoritative answer for a command that speaks this
        # contract, and such a command exits 0 whenever it emitted one. A success
        # envelope beside a non-zero exit is therefore not a success reported
        # twice, it is two channels contradicting each other, and nothing here can
        # tell which one is right. A refusal is left alone: there both channels
        # agree that something went wrong, and the envelope says more about it.
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
    if isinstance(target, dict):
        name = target.get("verb")
        playbook = target.get("playbook")
        unknown = sorted(set(target) - {"verb", "playbook"})
        if unknown:
            raise ValueError(f"help object takes 'verb' and 'playbook'; got {unknown}")
        if not isinstance(name, str):
            raise ValueError("help object needs a 'verb' string")
    else:
        name = target
    resolved = resolve(name)
    verb = VERBS.get(resolved)
    if verb is None:
        return _unknown_verb(name, resolved)
    if playbook is not None and not verb.playbook_aware:
        raise ValueError(f"{resolved!r} takes no playbook")
    try:
        schema = verb_schema(verb, playbook=playbook)
    except projection.SchemaProjectionError as exc:
        raise ValueError(f"{resolved!r} has no describable schema: {exc}") from exc
    answer = {"verb": resolved, "schema": schema}
    if verb.executor == "spawn":
        answer["schema_fingerprint"] = schema_fingerprint(schema)
    return answer


def _unknown_verb(name: Any, resolved: str) -> dict[str, Any]:
    for absent in ABSENT:
        if absent.name == resolved:
            return {
                "verb": resolved,
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


async def _run_one(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return _op_error(
            "?", OpError("invalid_input", f"each op is an object, got {type(entry).__name__}"), None
        )
    unknown_keys = sorted(set(entry) - {"op", "args", "schema_fingerprint"})
    raw_op = entry.get("op")
    try:
        name = resolve(raw_op)
    except TypeError:
        return _op_error(
            "?", OpError("invalid_input", f"op must be a string, got {raw_op!r}"), None
        )
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
            absent = _unknown_verb(raw_op, name)
        except ValueError as exc:
            return _op_error(name, OpError("not_found", str(exc)), None)
        # A named-but-unavailable verb answers with why, which is a different
        # fact from a name nobody ever registered.
        return _op_error(
            name, OpError("unavailable", absent["reason"], {"summary": absent["summary"]}), None
        )

    # Absent and null both mean "no arguments"; anything else is judged on its
    # type. `or {}` would have collapsed every falsy value — an empty list, an
    # empty string, false — into the no-arguments case, so a caller passing the
    # wrong shape was told its op succeeded and its input was dropped, which is
    # the one answer closed validation exists to make impossible.
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
        schema = verb_schema(verb, playbook=playbook if isinstance(playbook, str) else None)
        if verb.executor == "spawn":
            _require_fingerprint(name, verb, schema, entry.get("schema_fingerprint"))
        _validate(schema, args, verb)
        if verb.executor == "spawn":
            result = _run_spawn(verb, schema, args)
        elif verb.executor == "job":
            result = await _run_job(verb, args)
        elif verb.executor == "roster":
            result = _run_roster(verb, args)
        else:
            result = _run_machine(verb, schema, args)
    except OpError as exc:
        return _op_error(name, exc, schema)
    except projection.SchemaProjectionError as exc:
        return _op_error(name, OpError("unavailable", str(exc)), None)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return _op_error(name, OpError("internal", f"{type(exc).__name__}: {exc}"), schema)
    return {"ok": True, "op": name, "result": result}


async def request(ops: list[dict[str, Any]] | None = None, help: Any = None) -> dict[str, Any]:  # noqa: A002 — `help` is the parameter name the surface advertises
    """Run a batch of ops, or answer a help request. Never raises for one bad op."""
    if help is not None and help is not False:
        return _help(help)
    if ops is None:
        raise ValueError(
            "pass ops, or help=true for the catalog. This tool dispatches namespaced "
            "verbs; help=true lists them with their required parameters."
        )
    if not isinstance(ops, list):
        raise ValueError(f"ops is a list of {{op, args}} objects, got {type(ops).__name__}")
    if not ops:
        raise ValueError("ops is empty; pass at least one {op, args} object")
    if len(ops) > MAX_OPS:
        raise ValueError(f"ops carries {len(ops)} entries, over the maximum of {MAX_OPS}")

    results = [await _run_one(entry) for entry in ops]
    return {
        "status": "success" if all(r["ok"] for r in results) else "partial",
        "ops": results,
    }
