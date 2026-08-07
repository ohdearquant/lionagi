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

    A verb whose ops must carry a ``schema_fingerprint`` gets it here (the
    schema is already built to read ``required`` off it, so the hash costs
    nothing extra). Where the schema depends on an argument, no fingerprint is
    quoted — it would never match — and the entry instead names the parameter
    it varies with.
    """
    entries: list[dict[str, Any]] = []
    for verb in VERBS.values():
        entry: dict[str, Any] = {"verb": verb.name, "available": True, "summary": verb.summary}
        try:
            schema = verb_schema(verb)
            entry["required"] = list(schema.get("required", []))
            unenforced = list(schema.get("x-required-unenforced", []))
            if unenforced:
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


def _require_fingerprint(
    name: str,
    verb: Verb,
    schema: dict[str, Any],
    supplied: Any,
    *,
    playbook: str | None = None,
) -> None:
    """Spawn ops carry the fingerprint targeted help returned for them.

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
        source: Any = {"verb": name, "playbook": playbook}
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
            f"{name!r} needs the schema_fingerprint that help returns for it; ask for "
            f"{ask} and send the fingerprint as a sibling of 'args', not a "
            f"member of it: {shape}",
            remedy,
        )
    raise OpError(
        "stale_schema",
        f"{name!r} was called with schema_fingerprint {supplied!r}, which is not the "
        f"current {current!r}; the parameters changed since that schema was read. "
        f"Re-read {ask} and send: {shape}",
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
    """
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


def _refuse_unknown_profile(args: dict[str, Any], cwd: str | None) -> None:
    """Refuse a submission naming an agent profile nothing declares.

    Checked with the exact resolver a spawned run itself would use (same
    directories, same cwd, same plugin/ambiguity rules), so this never
    refuses a name the run would have found. Unresolved, the failure used to
    surface only inside the spawned process — after the job record already
    existed with nothing on it to say why — leaving a caller to poll a run
    that died silently on start.
    """
    name = args.get("agent")
    if not name:
        return
    from lionagi.cli._providers import (
        AgentProfileNotFoundError,
        AmbiguousProfileNameError,
        load_agent_profile,
    )

    with roster._resolving_under(cwd):
        try:
            load_agent_profile(name)
        except (AgentProfileNotFoundError, AmbiguousProfileNameError) as exc:
            raise OpError("invalid_input", str(exc)) from exc


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


def _run_spawn(verb: Verb, schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    prompt = _resolve_prompt(args)
    _refuse_too_many_positionals(verb, args, prompt)
    _refuse_without_model(verb, schema, args, prompt)
    _refuse_without_prompt(verb, args, prompt)
    cwd = _resolve_cwd(args)
    _refuse_unknown_profile(args, cwd)
    flags = render_argv(schema, args)
    assert verb.job_kind is not None
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
    from lionagi.cli._code_identity import code_identity
    from lionagi.cli.machine import CONTRACT_VERSION
    from lionagi.version import __version__

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
    answer: dict[str, Any] = {"verb": resolved, "schema": schema}
    if verb.executor == "spawn":
        _describe_fingerprint(answer, verb, schema, playbook=playbook)
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
        schema = verb_schema(verb, playbook=playbook if isinstance(playbook, str) else None)
        if verb.executor == "spawn":
            _require_fingerprint(
                name,
                verb,
                schema,
                entry.get("schema_fingerprint"),
                playbook=playbook if isinstance(playbook, str) else None,
            )
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
    """Run a batch of ops, or answer a help request. Never raises for one bad op."""
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
    return {
        "status": "success" if all(r["ok"] for r in results) else "partial",
        "ops": results,
    }
