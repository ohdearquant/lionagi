# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Read a hooks file named when a leg is submitted, and plan what the leg carries from it.

ADR-0048 Amendment 1 lets a caller hand one leg its parent's guards from a Claude Code
``settings.json`` or a Codex ``hooks.json``. This module holds the parts that need no
running leg:

- :func:`read_hooks_file` reads the file once under the amendment's H2 rules and returns
  the accepted bytes, their digest and the parsed object. A refusal names the rule it
  broke and the path as given and carries no bytes from the file.
- :func:`plan_carry` sorts the parsed object under H3: the three guard keys (``hooks``,
  ``permissions.deny``, ``permissions.ask``) are carried, every other key is reported
  and dropped, and a guard that cannot be carried refuses the whole plan. Hook entries
  are translated to ``hooks_external`` entries tagged ``source: imported:<format>``;
  deny and ask rules are rendered for the leg by :func:`render_permission_rule` (H6).

A refusal names an entry by its key path and index, never by its content.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lionhub.hooks.external import SUPPORTED_EVENTS, ExternalHookConfigError, validate_argv

__all__ = (
    "HOOKS_FROM_CARRY",
    "HOOKS_FROM_FORMATS",
    "MAX_HOOKS_FROM_BYTES",
    "CarryPlan",
    "HooksFile",
    "HooksFromError",
    "plan_carry",
    "read_hooks_file",
    "render_permission_rule",
)

MAX_HOOKS_FROM_BYTES = 256 * 1024
HOOKS_FROM_FORMATS = ("claude", "codex")
# The guard kinds a caller may carry; ``deny`` covers ``permissions.ask`` too.
HOOKS_FROM_CARRY = ("hooks", "deny")

# Claude Code settings keys this module may name when it drops them. Any other dropped
# key is counted, so the result never lists the keys of an arbitrary JSON file.
_KNOWN_SETTINGS_KEYS = frozenset(
    {
        "$schema",
        "alwaysThinkingEnabled",
        "apiKeyHelper",
        "awsAuthRefresh",
        "awsCredentialExport",
        "cleanupPeriodDays",
        "companyAnnouncements",
        "disableAllHooks",
        "disabledMcpjsonServers",
        "enableAllProjectMcpServers",
        "enabledMcpjsonServers",
        "enabledPlugins",
        "env",
        "extraKnownMarketplaces",
        "forceLoginMethod",
        "forceLoginOrgUUID",
        "includeCoAuthoredBy",
        "model",
        "outputStyle",
        "permissions",
        "sandbox",
        "spinnerTipsEnabled",
        "statusLine",
        "permissions.additionalDirectories",
        "permissions.allow",
        "permissions.defaultMode",
        "permissions.disableBypassPermissionsMode",
    }
)

# Shell-only characters: a string command holding one cannot become an argv vector.
_SHELL_METACHARACTERS = re.compile(r"""[|&;<>$`\\"'*?\[\]{}~#!\n]""")
# An event or tool name is named in a refusal only when it looks like one.
_NAME = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{0,63}\Z")
# Tools whose rules have no path anchor to lose, carried as written.
_ANCHORLESS_TOOLS = frozenset({"Bash", "PowerShell", "WebFetch", "Agent"})
_PATH_TOOLS = frozenset({"Read", "Edit"})
# Tools whose path rules Claude Code accepts and never matches, with the spelling to use.
_UNMATCHED_PATH_TOOLS = {
    "Write": "Edit",
    "NotebookEdit": "Edit",
    "MultiEdit": "Edit",
    "Glob": "Read",
    "Grep": "Read",
}


class HooksFromError(ValueError):
    """A hooks file or its plan is refused.

    ``rule`` names the rule broken. ``problems`` lists, for a refused plan, each entry's
    key path and the reason it cannot be carried. No message holds bytes from the file.
    """

    def __init__(self, rule: str, message: str, problems: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__(message)
        self.rule = rule
        self.problems = problems


class _RefusedError(ValueError):
    """Raised from inside the JSON parser; carries a reason with no file content."""


@dataclass(frozen=True)
class HooksFile:
    """The accepted bytes of a hooks file and what was read from them."""

    path: str
    resolved_path: str
    data: bytes
    sha256: str
    document: dict[str, Any]


@dataclass(frozen=True)
class CarryPlan:
    """What one leg carries from a hooks file, and what the submit result reports."""

    hooks: dict[str, list[dict[str, Any]]]
    deny_rules: tuple[str, ...]
    ask_rules_as_deny: int
    left_out: tuple[str, ...]
    dropped_keys: tuple[str, ...]
    dropped_other: int

    def report(self) -> list[str]:
        lines = []
        if self.ask_rules_as_deny:
            lines.append(
                f"{self.ask_rules_as_deny} ask rules rendered as deny: a leg has no one to ask"
            )
        lines.extend(f"{kind} left out at the caller's request" for kind in self.left_out)
        if self.dropped_keys:
            lines.append("not carried: " + ", ".join(self.dropped_keys))
        if self.dropped_other:
            lines.append(f"not carried: {self.dropped_other} other key(s)")
        return lines


def _refuse_constant(name: str) -> Any:
    raise _RefusedError(f"uses {name}, which JSON does not define")


def _refuse_repeated_names(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise _RefusedError("repeats a name inside an object")
    return obj


def read_hooks_file(path: Any) -> HooksFile:
    """Read the hooks file at *path* once, under ADR-0048 Amendment 1 H2."""
    if not isinstance(path, str) or not path:
        raise HooksFromError("absolute-path", "hooks_from must be an absolute path")
    if path == "-":
        raise HooksFromError(
            "absolute-path", "hooks_from cannot be '-': the server reads the file itself"
        )
    given = Path(path)
    if not given.is_absolute():
        raise HooksFromError(
            "absolute-path",
            f"hooks_from must be an absolute path, got {path!r}: the server reads it, so a "
            "relative path would resolve against the server's directory",
        )
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise HooksFromError(
            "no-symlink", "hooks_from needs O_NOFOLLOW, which this platform does not provide"
        )
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise HooksFromError(
                "no-symlink",
                f"hooks_from {path!r} is a symbolic link; name the file it points to",
            ) from None
        raise HooksFromError(
            "open", f"could not open hooks_from {path!r}: {exc.strerror}"
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HooksFromError("regular-file", f"hooks_from {path!r} is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_HOOKS_FROM_BYTES:
            chunk = os.read(fd, MAX_HOOKS_FROM_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > MAX_HOOKS_FROM_BYTES:
        raise HooksFromError(
            "size", f"hooks_from {path!r} is larger than {MAX_HOOKS_FROM_BYTES} bytes"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HooksFromError("json", f"hooks_from {path!r} is not UTF-8 text") from None
    try:
        document = json.loads(
            text, parse_constant=_refuse_constant, object_pairs_hook=_refuse_repeated_names
        )
    except _RefusedError as exc:
        raise HooksFromError("json", f"hooks_from {path!r} {exc}") from None
    except json.JSONDecodeError as exc:
        raise HooksFromError(
            "json",
            f"hooks_from {path!r} is not valid JSON (line {exc.lineno}, column {exc.colno})",
        ) from None
    except RecursionError:
        raise HooksFromError("json", f"hooks_from {path!r} nests too deeply") from None
    if not isinstance(document, dict):
        raise HooksFromError("json", f"hooks_from {path!r} must hold a JSON object at the top")
    return HooksFile(
        path=path,
        resolved_path=str(given.parent.resolve() / given.name),
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        document=document,
    )


def _source_kind(resolved_path: str, home: Path) -> tuple[str, Path | None]:
    """``user``, ``project`` with its project directory, or ``other``."""
    path = Path(resolved_path)
    if path == home / ".claude" / "settings.json":
        return "user", None
    if path.name in ("settings.json", "settings.local.json") and path.parent.name == ".claude":
        return "project", path.parent.parent
    return "other", None


def _absolute_rule(directory: Path, pattern: str) -> str:
    return "//" + str(directory).strip("/") + "/" + pattern


def _directories(source_dir: Path, leg_cwd: Path) -> list[Path]:
    return [source_dir] if source_dir == leg_cwd else [source_dir, leg_cwd]


def render_permission_rule(
    rule: Any, *, source_kind: str, source_dir: Path | None, leg_cwd: Path
) -> list[str]:
    """The rule text a leg's settings carry for one deny or ask *rule* (ADR-0048 H6).

    *source_kind* is ``project``, ``user`` or ``other``, as the source file is a project or
    local settings file (then *source_dir* is its project directory), the user settings
    file, or any other file. Raises :class:`ValueError` with a reason that quotes nothing
    from the rule.
    """
    if not isinstance(rule, str) or not rule:
        raise ValueError("a rule must be a non-empty string")
    if "(" not in rule:
        return [rule]
    if not rule.endswith(")"):
        raise ValueError("is not a rule: its '(' has no closing ')' at the end")
    tool, spec = rule[:-1].split("(", 1)
    if not tool:
        raise ValueError("is not a rule: it names no tool")
    if spec == "*" or tool in _ANCHORLESS_TOOLS or tool.startswith("mcp__"):
        return [rule]
    if tool == "Cd":
        if spec.startswith(("//", "~/")):
            return [rule]
        raise ValueError(
            "a Cd rule anchors elsewhere in a leg unless it is written from the filesystem "
            "root as Cd(//...) or from the home directory as Cd(~/...)"
        )
    if tool in _UNMATCHED_PATH_TOOLS:
        spelling = _UNMATCHED_PATH_TOOLS[tool]
        raise ValueError(
            f"Claude Code never matches a {tool} rule with a path, so it guards nothing; "
            f"write it as a {spelling} rule for the same path"
        )
    if tool not in _PATH_TOOLS:
        name = tool if _NAME.match(tool) else "this tool"
        raise ValueError(f"a rule with a specifier for {name} cannot be carried")
    if spec.startswith("!"):
        raise ValueError("a '!' negation cannot be carried: it reads relative to the directory")
    if spec.startswith(("//", "~/")):
        return [rule]
    if spec.startswith("/"):
        if source_kind == "user":
            return [f"{tool}(~/.claude/{spec[1:]})"]
        if source_kind == "project" and source_dir is not None:
            return [
                f"{tool}({_absolute_rule(directory, spec[1:])})"
                for directory in _directories(source_dir, leg_cwd)
            ]
        raise ValueError(
            "a rule anchored with '/' in a file that is not a project, local or user settings "
            "file has no known anchor"
        )
    if source_kind != "project" or source_dir is None:
        raise ValueError(
            "a relative rule in a file that is not a project or local settings file has no "
            "known directory"
        )
    dotted = spec.startswith("./")
    pattern = spec[2:] if dotted else spec
    anchored = not dotted and len(pattern.split("/")) - 1 >= 2
    if anchored or pattern.startswith("**/"):
        rendered = [
            _absolute_rule(directory, pattern) for directory in _directories(source_dir, leg_cwd)
        ]
    else:
        rendered = [
            _absolute_rule(directory, "**/" + pattern)
            for directory in _directories(source_dir, leg_cwd)
        ]
    return [f"{tool}({text})" for text in rendered]


def _argv(command: Any) -> tuple[list[str] | None, str | None]:
    """The argv vector for a hook command, or a reason that quotes nothing from it."""
    if isinstance(command, list):
        try:
            return validate_argv(command), None
        except ExternalHookConfigError:
            return None, "the command list must hold non-empty strings only"
    if isinstance(command, str):
        if _SHELL_METACHARACTERS.search(command):
            return None, "the command needs a shell: it holds shell-only characters"
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None, "the command cannot be split into an argv vector"
        if not tokens:
            return None, "the command is empty"
        return tokens, None
    return None, "the command must be a string or a list of strings"


def _is_timeout(value: Any) -> bool:
    """A hook timeout is a positive, finite number of seconds."""
    return (
        not isinstance(value, bool) and isinstance(value, (int, float)) and 0 < value < float("inf")
    )


def _plan_hooks(
    block: Any, fmt: str, problems: list[tuple[str, str]]
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(block, dict):
        problems.append(("hooks", "must be an object mapping event names to matcher groups"))
        return {}
    external: dict[str, list[dict[str, Any]]] = {}
    for position, (event, groups) in enumerate(block.items()):
        label = f"hooks.{event}" if _NAME.match(event) else f"hooks key {position}"
        if event not in SUPPORTED_EVENTS:
            problems.append((label, "has no LionAGI seam for this event"))
            continue
        if not isinstance(groups, list):
            groups = [groups]
        for group_index, group in enumerate(groups):
            where = f"{label}[{group_index}]"
            if not isinstance(group, dict):
                problems.append((where, "a matcher group must be an object"))
                continue
            matcher = group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                problems.append((where, "the matcher must be a string"))
                continue
            specs = group.get("hooks", [])
            if not isinstance(specs, list):
                specs = [specs]
            translated = []
            for hook_index, spec in enumerate(specs):
                entry_where = f"{where}.hooks[{hook_index}]"
                if not isinstance(spec, dict):
                    problems.append((entry_where, "a hook entry must be an object"))
                    continue
                if spec.get("type", "command") != "command":
                    problems.append((entry_where, "only 'command' hooks can run"))
                    continue
                argv, reason = _argv(spec.get("command"))
                if argv is None:
                    problems.append((entry_where, reason or "the command cannot be carried"))
                    continue
                entry: dict[str, Any] = {
                    "type": "command",
                    "command": argv,
                    "source": f"imported:{fmt}",
                }
                if "timeout" in spec:
                    if not _is_timeout(spec["timeout"]):
                        problems.append(
                            (entry_where, "the timeout must be a positive number of seconds")
                        )
                        continue
                    entry["timeout"] = spec["timeout"]
                translated.append(entry)
            if translated:
                group_entry: dict[str, Any] = {"hooks": translated}
                if matcher is not None:
                    group_entry = {"matcher": matcher, "hooks": translated}
                external.setdefault(event, []).append(group_entry)
    return external


def plan_carry(
    document: dict[str, Any],
    *,
    fmt: str,
    carry: Any = HOOKS_FROM_CARRY,
    source_path: str,
    leg_cwd: str,
    home: str | None = None,
) -> CarryPlan:
    """Sort *document* into what a leg carries (ADR-0048 Amendment 1 H1, H3, H6).

    *source_path* is the file's resolved path, which decides how relative and
    ``/``-anchored rules render; *leg_cwd* is the leg's working directory. Raises
    :class:`HooksFromError` listing every guard that cannot be carried.
    """
    if fmt not in HOOKS_FROM_FORMATS:
        raise HooksFromError(
            "format", f"hooks_from_format must be one of {list(HOOKS_FROM_FORMATS)}"
        )
    if not isinstance(carry, (list, tuple)) or not all(
        isinstance(kind, str) and kind in HOOKS_FROM_CARRY for kind in carry
    ):
        raise HooksFromError(
            "carry", f"hooks_from_carry must be a list drawn from {list(HOOKS_FROM_CARRY)}"
        )
    carried = set(carry)
    home_dir = Path(home) if home is not None else Path.home().resolve()
    kind, source_dir = _source_kind(source_path, home_dir)
    leg_dir = Path(leg_cwd)
    problems: list[tuple[str, str]] = []
    dropped: list[str] = []
    dropped_other = 0

    def drop(name: str) -> None:
        nonlocal dropped_other
        if name in _KNOWN_SETTINGS_KEYS:
            dropped.append(name)
        else:
            dropped_other += 1

    hooks: dict[str, list[dict[str, Any]]] = {}
    deny_rules: list[str] = []
    ask_count = 0
    for key, value in document.items():
        if key == "hooks":
            if "hooks" in carried:
                hooks = _plan_hooks(value, fmt, problems)
            continue
        if key == "permissions" and fmt == "claude":
            if not isinstance(value, dict):
                if "deny" in carried:
                    problems.append(("permissions", "must be an object"))
                continue
            for sub, rules in value.items():
                if sub not in ("deny", "ask"):
                    drop(f"permissions.{sub}")
                    continue
                if "deny" not in carried:
                    continue
                if not isinstance(rules, list):
                    problems.append((f"permissions.{sub}", "must be a list of rules"))
                    continue
                for index, rule in enumerate(rules):
                    try:
                        rendered = render_permission_rule(
                            rule, source_kind=kind, source_dir=source_dir, leg_cwd=leg_dir
                        )
                    except ValueError as exc:
                        problems.append((f"permissions.{sub}[{index}]", str(exc)))
                        continue
                    deny_rules.extend(text for text in rendered if text not in deny_rules)
                    if sub == "ask":
                        ask_count += 1
            continue
        drop(key)
    if problems:
        listed = "; ".join(f"{where}: {reason}" for where, reason in problems)
        raise HooksFromError(
            "carry",
            f"hooks_from holds {len(problems)} guard(s) a leg cannot carry: {listed}",
            tuple(problems),
        )
    return CarryPlan(
        hooks=hooks,
        deny_rules=tuple(deny_rules),
        ask_rules_as_deny=ask_count,
        left_out=tuple(guard for guard in HOOKS_FROM_CARRY if guard not in carried),
        dropped_keys=tuple(sorted(dropped)),
        dropped_other=dropped_other,
    )
