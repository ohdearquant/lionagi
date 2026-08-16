# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Project the CLI's own argparse parsers into JSON Schema at runtime.

Builds the *same* parser the CLI builds for a real invocation and translates
it, so a verb's schema cannot drift from the command it describes.

Translation is deliberately bounded (scalars, store_true/false, choices as
enums, nargs/repeats as arrays, requiredness, defaults, aliases, positional
order, mutually-exclusive groups); anything outside that raises
:class:`SchemaProjectionError` naming the offending action — a verb better
absent than described wrongly, since coercing an unmodelable parameter to
``string`` would betray a caller's trust in the schema.

``li play`` has no parser of its own (it rewrites into ``li o flow -p NAME``,
and the playbook's args reach the parser only once NAME is known), so
``orchestrate flow`` projects in two stages: without a playbook it advertises
the playbook parameter and common flow flags; with one it performs the same
injection the CLI performs and returns a fingerprint of what it resolved.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

from lionagi._auto import CliSeed, build_cli_parser, iter_cli_seeds, seed_for
from lionagi.cli._argtypes import JsonArgument

__all__ = (
    "SchemaProjectionError",
    "PlaybookResolutionError",
    "VerbProjection",
    "available_paths",
    "build_parser_for",
    "playbook_fingerprint",
    "project",
    "project_parser",
)


class SchemaProjectionError(RuntimeError):
    """A parser holds something the bounded translation cannot describe."""

    def __init__(self, path: str, reason: str, *, action: str | None = None) -> None:
        self.path = path
        self.reason = reason
        self.action = action
        where = f"{path!r} action {action!r}" if action else f"{path!r}"
        super().__init__(f"cannot project {where}: {reason}")


class PlaybookResolutionError(SchemaProjectionError):
    """A named playbook did not resolve to a readable file."""


# ── the CLI seam ─────────────────────────────────────────────────────────────


def _subparser_actions(parser: argparse.ArgumentParser) -> list[argparse._SubParsersAction]:
    return [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]


def _canonical_choices(
    action: argparse._SubParsersAction,
) -> list[tuple[str, tuple[str, ...], argparse.ArgumentParser]]:
    """(canonical name, aliases, parser) per registered subcommand.

    The first name seen for a given parser object is its canonical name;
    ``choices`` maps every alias to that same object in registration order.
    """
    ordered: dict[int, list[str]] = {}
    for name, sub in action.choices.items():
        ordered.setdefault(id(sub), []).append(name)
    out = []
    for names in ordered.values():
        sub = action.choices[names[0]]
        out.append((names[0], tuple(names[1:]), sub))
    return out


_Tree = dict[tuple[str, ...], tuple[argparse.ArgumentParser, bool]]


def _walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...], canonical: bool) -> _Tree:
    found: _Tree = {prefix: (parser, canonical)}
    for sub_action in _subparser_actions(parser):
        for name, aliases, sub in _canonical_choices(sub_action):
            found.update(_walk(sub, (*prefix, name), canonical))
            for alias in aliases:
                found.update(_walk(sub, (*prefix, alias), False))
    return found


def _command_tree(spec: CliSeed) -> _Tree:
    """Every parser path under one top-level command, freshly built.

    Each entry says whether the path spells every level with its canonical
    name; alias spellings resolve to the same parser, not a separate command.
    Walks the root parser's registered subparsers action rather than the
    factory's own return value, since ``orchestrate`` returns a dict of
    sub-parsers where the others return a single parser.
    """
    root = build_cli_parser(spec).parser
    tree: _Tree = {}
    for sub_action in _subparser_actions(root):
        for name, aliases, sub in _canonical_choices(sub_action):
            if name != spec.name:
                continue  # the unselected commands are metadata-only stubs
            tree.update(_walk(sub, (name,), True))
            for alias in aliases:
                tree.update(_walk(sub, (alias,), False))
    return tree


def _split(path: str) -> tuple[str, ...]:
    parts = tuple(p for p in path.strip().split() if p)
    if not parts:
        raise SchemaProjectionError(path, "empty command path")
    return parts


def _spec_for(head: str) -> CliSeed:
    spec = seed_for(head)
    if spec is None:
        raise SchemaProjectionError(head, "no such command")
    return spec


def available_paths() -> tuple[str, ...]:
    """Every command path the projector can reach, canonical names only.

    Reachability here is not authorization — what the projector can read is
    strictly wider than what the dispatch surface allows.
    """
    paths: list[str] = []
    for spec in iter_cli_seeds():
        for parts, (_parser, canonical) in _command_tree(spec).items():
            if canonical:
                paths.append(" ".join(parts))
    return tuple(sorted(set(paths)))


def build_parser_for(path: str) -> argparse.ArgumentParser:
    """The real parser the CLI would build for *path*.

    Freshly constructed on every call — projecting a playbook-bearing path
    mutates the parser it reads.
    """
    parts = _split(path)
    spec = _spec_for(parts[0])
    entry = _command_tree(spec).get(parts)
    if entry is None:
        raise SchemaProjectionError(path, "no such command path")
    return entry[0]


# ── bounded translation ──────────────────────────────────────────────────────

# Exact classes, not isinstance: a subclass may override __call__ with
# semantics this translation would silently misdescribe, so a subclass is an
# unknown action like any other.
_STORE = argparse._StoreAction
_STORE_TRUE = argparse._StoreTrueAction
_STORE_FALSE = argparse._StoreFalseAction
_APPEND = argparse._AppendAction

# Terminating actions that print and exit. They are not parameters of an
# invocation, so skipping them is not a gap in the description.
_META_ACTIONS = (argparse._HelpAction, argparse._VersionAction)

_SCALAR_JSON_TYPE = {None: "string", str: "string", int: "integer", float: "number"}

_UNBOUNDED_NARGS = (argparse.REMAINDER, argparse.PARSER)


def _scalar_type(path: str, action: argparse.Action, label: str) -> str:
    kind = action.type
    if kind not in _SCALAR_JSON_TYPE:
        name = getattr(kind, "__name__", repr(kind))
        raise SchemaProjectionError(
            path, f"type={name} has no scalar JSON counterpart", action=label
        )
    return _SCALAR_JSON_TYPE[kind]


def _label(action: argparse.Action) -> str:
    if action.option_strings:
        return "/".join(action.option_strings)
    return action.metavar or action.dest


def _flag_of(action: argparse.Action) -> tuple[str | None, tuple[str, ...]]:
    """Primary flag and its aliases; argparse derives dest from the first long
    option, so that is the primary when there is one."""
    if not action.option_strings:
        return None, ()
    long_opts = [o for o in action.option_strings if o.startswith("--")]
    primary = long_opts[0] if long_opts else action.option_strings[0]
    aliases = tuple(o for o in action.option_strings if o != primary)
    return primary, aliases


def _description(parser: argparse.ArgumentParser, action: argparse.Action) -> str | None:
    """The help text, with argparse's ``%(default)s``-style fields expanded the
    way argparse expands them when it prints help."""
    text = action.help
    if not text:
        return None
    if "%" not in text:
        return text
    params = dict(vars(action), prog=parser.prog)
    for name, value in list(params.items()):
        if value is argparse.SUPPRESS:
            del params[name]
        elif hasattr(value, "__name__"):
            params[name] = value.__name__
    try:
        return text % params
    except (KeyError, TypeError, ValueError):
        return text


def _choices_enum(path: str, action: argparse.Action, label: str) -> list[Any] | None:
    if action.choices is None:
        return None
    values = list(action.choices)
    for value in values:
        if not isinstance(value, str | int | float | bool) and value is not None:
            raise SchemaProjectionError(
                path, f"choices contain a non-JSON value {value!r}", action=label
            )
    return values


def _jsonable_default(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool | list | dict):
        return value
    return str(value)


def _project_action(
    path: str, parser: argparse.ArgumentParser, action: argparse.Action
) -> dict[str, Any]:
    label = _label(action)
    kind = type(action)

    if kind is _STORE_TRUE or kind is _STORE_FALSE:
        schema: dict[str, Any] = {"type": "boolean"}
    elif kind is _STORE or kind is _APPEND:
        nargs = action.nargs
        if nargs in _UNBOUNDED_NARGS:
            raise SchemaProjectionError(
                path, f"nargs={nargs!r} consumes argv verbatim", action=label
            )
        if kind is _APPEND and nargs is not None:
            raise SchemaProjectionError(
                path, f"append with nargs={nargs!r} nests arrays", action=label
            )
        if isinstance(action.type, JsonArgument):
            if nargs is not None or kind is _APPEND:
                raise SchemaProjectionError(
                    path, f"JSON-encoded value with nargs={nargs!r}", action=label
                )
            return _project_json_action(parser, action, action.type)
        item: dict[str, Any] = {"type": _scalar_type(path, action, label)}
        enum = _choices_enum(path, action, label)
        if enum is not None:
            item["enum"] = enum

        if kind is _APPEND and nargs is None:
            schema = {"type": "array", "items": item}
        elif nargs is None:
            schema = dict(item)
        elif isinstance(nargs, int):
            schema = {"type": "array", "items": item, "minItems": nargs, "maxItems": nargs}
        elif nargs == "*":
            schema = {"type": "array", "items": item}
        elif nargs == "+":
            schema = {"type": "array", "items": item, "minItems": 1}
        elif nargs == "?":
            if action.const is None:
                schema = dict(item)
            else:
                # The flag is legal bare, and argparse then stores `const`.
                # `true` is how a caller asks for the bare form.
                schema = {"anyOf": [item, {"const": True}], "x-bare-value": action.const}
        else:
            raise SchemaProjectionError(path, f"nargs={nargs!r} is not modelled", action=label)
    else:
        raise SchemaProjectionError(path, f"unknown action class {kind.__name__}", action=label)

    return _annotate(schema, parser, action)


def _annotate(
    schema: dict[str, Any], parser: argparse.ArgumentParser, action: argparse.Action
) -> dict[str, Any]:
    """Add the parts every parameter carries: prose, default, and how it spells."""
    description = _description(parser, action)
    if description:
        schema["description"] = description
    if action.default is not None and action.default is not argparse.SUPPRESS:
        schema["default"] = _jsonable_default(action.default)

    flag, aliases = _flag_of(action)
    if flag is None:
        schema["x-positional"] = True
    else:
        schema["x-flag"] = flag
        if aliases:
            schema["x-aliases"] = list(aliases)
    return schema


def _project_json_action(
    parser: argparse.ArgumentParser, action: argparse.Action, kind: JsonArgument
) -> dict[str, Any]:
    """A flag whose value the parser decodes from JSON, described as it decodes.

    Advertised type is the *decoded* shape, since that's what the argument
    accepts; ``x-json-encoded`` tells a renderer the one argv token has to be
    the JSON encoding of it.
    """
    schema = dict(kind.json_schema)
    schema["x-json-encoded"] = True
    return _annotate(schema, parser, action)


def _accepts_no_values(action: argparse.Action) -> bool:
    """A positional that parses happily with nothing supplied for it.

    See docs/internals/mcp.md#accepts-no-values-required-unenforced for why
    this is checked structurally rather than by trusting `action.required`.
    """
    return not action.option_strings and action.nargs == "*"


def _mutually_exclusive(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    groups = []
    for group in parser._mutually_exclusive_groups:
        members = [a.dest for a in group._group_actions]
        if members:
            groups.append({"parameters": members, "required": bool(group.required)})
    return groups


# A run of flag spellings offered as alternatives, e.g. ``-r / --resume``.
_FLAG_ALTERNATIVES = re.compile(r"--?[A-Za-z][\w-]*(?:\s*/\s*--?[A-Za-z][\w-]*)*")

# Help text that DEMONSTRATES a command rather than referring to one: a literal
# invocation, or a worked example. Those are meant to be read as typed, so the
# flags in them are the point and renaming them produces a line that does not run.
_DEMONSTRATES = re.compile(r"(?:^|\s)li\s+[a-z]|\be\.g\.|\bExample\b", re.IGNORECASE)


def _flag_properties(parser: argparse.ArgumentParser) -> dict[str, str]:
    """Every flag spelling *parser* accepts, mapped to the property it becomes."""
    out: dict[str, str] = {}
    for action in parser._actions:
        if isinstance(action, _META_ACTIONS) or action.dest == argparse.SUPPRESS:
            continue
        for option in action.option_strings:
            out[option] = action.dest
    return out


def _name_parameters(text: str, flags: dict[str, str]) -> str:
    """Rewrite flag spellings in help text as the parameters they project to.

    Help text is written for someone typing a command, so it names flags. A
    caller of this schema sends an object and can never type one, and a
    parameter it cannot find is worse than a longer sentence would have been.

    Only spellings *this* parser accepts are rewritten. Help text quotes argv
    for other programs -- a scheduled command's own arguments, for one -- and
    those are still meant literally, so an unrecognised flag is left exactly as
    written rather than guessed at.

    Text that demonstrates a command is left whole for the same reason. A worked
    example or a literal ``li ...`` line is meant to be read as typed, and a
    renamed flag inside one produces a command that does not run while still
    looking like it would. Referring to a flag and showing one being used are
    different acts, and only the first has a parameter to name.
    """
    if _DEMONSTRATES.search(text):
        return text

    def replace(match: re.Match[str]) -> str:
        spellings = [part.strip() for part in match.group(0).split("/")]
        named = [flags[s] for s in spellings if s in flags]
        if not named:
            return match.group(0)
        # Alternative spellings of one flag are one parameter, so the run
        # collapses rather than repeating the same name several times.
        seen = list(dict.fromkeys(named))
        return " / ".join(f"`{name}`" for name in seen)

    # Backtick-quoted spans are already literal -- typically a whole command,
    # like `li agent --agent`. Renaming a flag inside one both breaks the
    # command and nests the quoting, so only the text between spans is touched.
    parts = text.split("`")
    for index in range(0, len(parts), 2):
        parts[index] = _FLAG_ALTERNATIVES.sub(replace, parts[index])
    return "`".join(parts)


def _rewrite_descriptions(node: Any, flags: dict[str, str]) -> None:
    """Rename flags to parameters in every description under *node*, in place.

    A JSON-valued argument projects to a nested schema whose own fields carry
    help text too, so this recurses rather than touching only the top level.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                node[key] = _name_parameters(value, flags)
            else:
                _rewrite_descriptions(value, flags)
    elif isinstance(node, list):
        for item in node:
            _rewrite_descriptions(item, flags)


def project_parser(parser: argparse.ArgumentParser, *, path: str) -> dict[str, Any]:
    """Translate one fully-resolved parser into a JSON Schema object."""
    nested = _subparser_actions(parser)
    if nested:
        names = sorted({name for a in nested for name in a.choices})
        raise SchemaProjectionError(
            path,
            f"path stops at an unresolved subcommand; name one of {names}",
            action=nested[0].dest,
        )

    properties: dict[str, Any] = {}
    required: list[str] = []
    unenforced: list[str] = []
    positionals: list[str] = []

    flags = _flag_properties(parser)

    for action in parser._actions:
        if isinstance(action, _META_ACTIONS):
            continue
        if action.dest == argparse.SUPPRESS:
            continue
        properties[action.dest] = _project_action(path, parser, action)
        _rewrite_descriptions(properties[action.dest], flags)
        if _accepts_no_values(action):
            unenforced.append(action.dest)
        elif action.required:
            required.append(action.dest)
        if not action.option_strings:
            positionals.append(action.dest)

    schema: dict[str, Any] = {
        "type": "object",
        "title": path,
        "properties": properties,
        "additionalProperties": False,
    }
    if parser.description:
        schema["description"] = _name_parameters(parser.description.strip(), flags)
    if required:
        schema["required"] = required
    if unenforced:
        schema["x-required-unenforced"] = unenforced
    if positionals:
        schema["x-positional-order"] = positionals
    exclusive = _mutually_exclusive(parser)
    if exclusive:
        schema["x-mutually-exclusive"] = exclusive
    return schema


# ── playbooks ────────────────────────────────────────────────────────────────

_PLAYBOOK_DEST = "playbook"


def _orchestrate() -> ModuleType:
    return import_module("lionagi.cli.orchestrate")


def _has_playbook_parameter(parser: argparse.ArgumentParser) -> bool:
    return any(a.dest == _PLAYBOOK_DEST and a.option_strings for a in parser._actions)


def playbook_fingerprint(name: str) -> tuple[str, str]:
    """``(fingerprint, resolved path)`` for a playbook name.

    Covers the whole playbook file, not just its declared arguments, since
    the body is what runs — a caller validating against one fingerprint and
    executing against another should be detectable.
    """
    path_obj, err = _orchestrate()._resolve_playbook_path(name)
    if err is not None or path_obj is None:
        raise PlaybookResolutionError(f"playbook:{name}", err or "playbook did not resolve")
    resolved = Path(str(path_obj))
    try:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError as exc:
        raise PlaybookResolutionError(f"playbook:{name}", f"unreadable: {exc}") from exc
    return f"sha256:{digest[:32]}", str(resolved)


@dataclass(frozen=True)
class VerbProjection:
    """A verb's parameter schema, and how the playbook stage was resolved.

    ``stage`` is ``static`` for a path with no playbook parameter, ``base``
    for a playbook-bearing path projected without a playbook named, and
    ``resolved`` once one is.
    """

    path: str
    schema: dict[str, Any]
    stage: str
    playbook: str | None = None
    playbook_fingerprint: str | None = None
    playbook_path: str | None = None
    playbook_parameters: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"path": self.path, "stage": self.stage, "schema": self.schema}
        if self.playbook is not None:
            out["playbook"] = self.playbook
            out["playbook_fingerprint"] = self.playbook_fingerprint
            out["playbook_path"] = self.playbook_path
            out["playbook_parameters"] = list(self.playbook_parameters)
        return out


def project(path: str, *, playbook: str | None = None) -> VerbProjection:
    """Project one command path into a JSON Schema object.

    Naming *playbook* on a playbook-bearing path performs the same argument
    injection the CLI performs before argparse runs, so the returned schema
    carries that playbook's declared arguments alongside the built-in flags.
    """
    parser = build_parser_for(path)
    canonical = " ".join(_split(path))
    playbook_bearing = _has_playbook_parameter(parser)

    if playbook is None:
        schema = project_parser(parser, path=canonical)
        if playbook_bearing:
            schema["x-playbook-arguments"] = (
                "This command accepts arguments declared by the playbook named in "
                "'playbook'. Ask for this schema again with that playbook named to "
                "see them."
            )
            return VerbProjection(path=canonical, schema=schema, stage="base")
        return VerbProjection(path=canonical, schema=schema, stage="static")

    if not playbook_bearing:
        raise SchemaProjectionError(canonical, "command takes no playbook")

    fingerprint, resolved_path = playbook_fingerprint(playbook)
    injected = _orchestrate().inject_playbook_schema_into_parser(parser, ["--playbook", playbook])
    schema = project_parser(parser, path=canonical)
    injected_names = tuple(injected)
    for name in injected_names:
        if name in schema["properties"]:
            schema["properties"][name]["x-from-playbook"] = playbook
    schema["x-playbook-fingerprint"] = fingerprint
    return VerbProjection(
        path=canonical,
        schema=schema,
        stage="resolved",
        playbook=playbook,
        playbook_fingerprint=fingerprint,
        playbook_path=resolved_path,
        playbook_parameters=injected_names,
    )
