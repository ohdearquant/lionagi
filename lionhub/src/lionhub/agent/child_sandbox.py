# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Sandbox policy for CLI children.

One list names the credential locations a child must not read and the toolchain caches it may
write. It is resolved from the global lionagi settings file only and rendered into the settings
Claude Code understands. Any malformed value raises ``ConfigurationError`` so a child never
starts unconfined because of a configuration mistake. Every refusal raised here ends with the
same sentence naming the opt-out, ``CHILD_SANDBOX_OPT_OUT``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lionagi._errors import ConfigurationError

__all__ = (
    "CHILD_SANDBOX_OPT_OUT",
    "CLAUDE_CLI_FLOOR",
    "DEFAULT_ALLOW_WRITE",
    "DEFAULT_DENY_READ",
    "ChildSandboxPolicy",
    "check_claude_cli_floor",
    "check_sandbox_backend",
    "claude_settings_argument",
    "claude_settings_for",
    "default_allow_write",
    "policy_from_dict",
    "resolve_child_sandbox",
    "warn_project_child_sandbox",
)

logger = logging.getLogger(__name__)

#: Oldest Claude Code release whose sandbox honours ``filesystem.denyRead``. Older releases
#: ignore unknown settings keys and would run the child unconfined.
CLAUDE_CLI_FLOOR: tuple[int, int, int] = (2, 1, 323)

#: The closing sentence of every refusal: the key and the one-line opt-out.
CHILD_SANDBOX_OPT_OUT = (
    "To run Claude Code children without the sandbox, set "
    "`child_sandbox: {enabled: false}` in ~/.lionagi/settings.yaml."
)

#: Generic credential locations a child may not read. Entries are ``~/`` or absolute paths;
#: a file entry names one file and a directory entry names the whole directory.
DEFAULT_DENY_READ: tuple[str, ...] = (
    "~/.ssh",
    "~/.gnupg",
    "~/.netrc",
    "~/.git-credentials",
    "~/.config/gh",
    "~/.config/hub",
    "~/.aws",
    "~/.config/gcloud",
    "~/.azure",
    "~/.kube/config",
    "~/.docker/config.json",
    "~/.npmrc",
    "~/.pypirc",
    "~/.config/pip/pip.conf",
    "~/.cargo/credentials.toml",
    "~/.cargo/credentials",
    "~/.gem/credentials",
    "~/.m2/settings.xml",
    "~/.gradle/gradle.properties",
    "~/.terraform.d/credentials.tfrc.json",
    "~/.fly",
    "~/.config/stripe",
    "~/.config/rclone",
    "~/.pgpass",
    "~/.my.cnf",
    "~/.codex/auth.json",
    "~/.claude/.credentials.json",
    "~/.gemini/oauth_creds.json",
    "~/.config/github-copilot",
    "~/.copilot",
    "~/.zsh_history",
    "~/.bash_history",
    "~/.python_history",
)

#: Toolchain caches a sandboxed child may write besides its working directory.
DEFAULT_ALLOW_WRITE: tuple[str, ...] = (
    "~/.cache/uv",
    "~/.cargo/registry",
    "~/.cargo/git",
    "~/.npm",
    "~/.cache/pip",
    "~/Library/Caches/pip",
)

#: Programs Claude Code's sandbox needs on Linux and WSL2.
_LINUX_BACKEND_PROGRAMS: tuple[str, ...] = ("bwrap", "socat")

_ROOT_KEYS = frozenset({"enabled", "deny_read", "allow_write", "profiles", "extends"})
_LAYER_KEYS = frozenset({"deny_read", "allow_write", "profiles"})

#: Project settings files already warned about, so each is reported once per process.
_warned_project_files: set[str] = set()


def _refusal(message: str) -> ConfigurationError:
    """A ``ConfigurationError`` whose text ends with the one-sentence opt-out."""
    return ConfigurationError(f"{message.rstrip().rstrip('.;:')}. {CHILD_SANDBOX_OPT_OUT}")


@dataclass(frozen=True)
class ChildSandboxPolicy:
    """Resolved sandbox policy: whether it applies, what stays unreadable, what stays writable."""

    enabled: bool
    deny_read: tuple[str, ...]
    allow_write: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form that survives endpoint kwargs, clones and resumes."""
        return {
            "enabled": self.enabled,
            "deny_read": list(self.deny_read),
            "allow_write": list(self.allow_write),
        }


def default_allow_write() -> tuple[str, ...]:
    """``DEFAULT_ALLOW_WRITE`` plus ``$UV_CACHE_DIR`` when it names an absolute or ``~/`` path."""
    extra = []
    uv_dir = os.environ.get("UV_CACHE_DIR", "").strip().rstrip("/")
    if uv_dir.startswith(("~/", "/")):
        extra.append(uv_dir)
    return tuple(dict.fromkeys([*extra, *DEFAULT_ALLOW_WRITE]))


def _path_list(label: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        raise _refusal(f"child_sandbox: {label} must be a list of non-empty strings")
    paths = []
    for raw in value:
        # Repeated slashes collapse, so ``//x`` and ``/x`` name one path and render one rule.
        path = re.sub(r"/{2,}", "/", raw.strip()).rstrip("/")
        if not path.startswith(("~/", "/")):
            raise _refusal(
                f"child_sandbox: {label} entry {raw!r} must be an absolute path or start with '~/'"
            )
        if any(part in {".", ".."} for part in path.split("/")):
            raise _refusal(f"child_sandbox: {label} entry {raw!r} must not contain '.' or '..'")
        paths.append(path)
    return paths


def _mapping(label: str, value: Any) -> dict:
    if not isinstance(value, dict):
        raise _refusal(f"child_sandbox: {label} must be a mapping")
    return value


def _reject_unknown(label: str, mapping: dict, allowed: frozenset[str] | set[str]) -> None:
    unknown = sorted(str(k) for k in mapping if k not in allowed)
    if unknown:
        raise _refusal(f"child_sandbox: {label} has unknown keys {unknown}")


def _layer(label: str, raw: dict) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Validate one layer (the settings block or an extends file) into its three parts."""
    _reject_unknown(label, raw, _LAYER_KEYS)
    deny = _path_list(f"{label}.deny_read", raw.get("deny_read", []))
    allow = _path_list(f"{label}.allow_write", raw.get("allow_write", []))
    profiles: dict[str, list[str]] = {}
    for name, entry in _mapping(f"{label}.profiles", raw.get("profiles", {})).items():
        if not isinstance(name, str) or not name:
            raise _refusal(f"child_sandbox: {label}.profiles has a non-string name")
        entry = _mapping(f"{label}.profiles.{name}", entry)
        _reject_unknown(f"{label}.profiles.{name}", entry, {"allow_read"})
        profiles[name] = _path_list(
            f"{label}.profiles.{name}.allow_read", entry.get("allow_read", [])
        )
    return deny, allow, profiles


def _read_extends(raw: Any) -> list[dict]:
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise _refusal("child_sandbox: extends must be a list of non-empty strings")
    layers = []
    for entry in raw:
        if not entry.strip().startswith(("~/", "/")):
            raise _refusal(
                f"child_sandbox: extends entry {entry!r} must be an absolute path or start "
                "with '~/'"
            )
        try:
            data = yaml.safe_load(Path(entry.strip()).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise _refusal(f"child_sandbox: cannot read extends file {entry!r}: {exc}") from exc
        layers.append(_mapping(f"extends file {entry!r}", {} if data is None else data))
    return layers


def _global_settings() -> dict:
    """The global lionagi settings layer; the project layer is never read here.

    A global file that cannot be read or parsed refuses the launch, naming the opt-out.
    """
    from lionagi._settings import load_settings

    try:
        return load_settings(include_project=False)
    except (OSError, ValueError, yaml.YAMLError, ConfigurationError) as exc:
        raise _refusal(f"child_sandbox: cannot read the global settings file: {exc}") from exc


def warn_project_child_sandbox(project_dir: str | Path | None = None) -> None:
    """Log one warning when the nearest project settings file carries a ``child_sandbox`` block.

    The block is ignored by design: a child can write files in its own worktree, so a project
    file must not be able to relax the policy. The file is located the way the settings loader
    locates it, from ``project_dir`` (the current directory by default). Each file is reported
    once per process. A project file that cannot be read is left to the settings loader to
    report; this check never raises.
    """
    from lionagi._settings import find_project_settings_file, load_project_settings_layer

    try:
        path = find_project_settings_file(project_dir)
        if path is None or str(path) in _warned_project_files:
            return
        layer = load_project_settings_layer(project_dir)
    except (OSError, ValueError, yaml.YAMLError):
        return
    if isinstance(layer, dict) and "child_sandbox" in layer:
        _warned_project_files.add(str(path))
        logger.warning(
            "child_sandbox in %s is ignored: only the global settings file "
            "(~/.lionagi/settings.yaml) is read for it",
            path,
        )


def resolve_child_sandbox(profile: str | None, settings: dict | None = None) -> ChildSandboxPolicy:
    """Resolve the sandbox policy for the agent profile named ``profile``.

    ``profile`` is the agent profile name the user passed (``None`` for a launch without one,
    which gets no ``allow_read`` exemption). It is matched against ``profiles.<name>`` in the
    ``child_sandbox`` block; a role name never matches.

    Reads the ``child_sandbox`` block of the global lionagi settings when ``settings`` is
    ``None``. The project layer is never read: a child can write files in its own worktree, so
    a project-level setting must not be able to relax the policy. A ``child_sandbox`` block in
    the project file is reported with one warning.

    Raises:
        ConfigurationError: any malformed value, unknown key, ``allow_read`` entry that is not on
            the deny list, or unreadable ``extends`` file.
    """
    if settings is None:
        warn_project_child_sandbox()
        settings = _global_settings()
    raw = (settings or {}).get("child_sandbox")
    raw = _mapping("block", {} if raw is None else raw)
    _reject_unknown("block", raw, _ROOT_KEYS)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _refusal("child_sandbox: enabled must be true or false")

    layers = [("child_sandbox", {k: v for k, v in raw.items() if k in _LAYER_KEYS})]
    layers += [(f"extends[{i}]", d) for i, d in enumerate(_read_extends(raw.get("extends", [])))]
    deny: list[str] = list(DEFAULT_DENY_READ)
    allow: list[str] = list(default_allow_write())
    profiles: dict[str, list[str]] = {}
    for label, layer in layers:
        d, a, p = _layer(label, layer)
        deny += d
        allow += a
        for name, paths in p.items():
            profiles.setdefault(name, []).extend(paths)

    deny = list(dict.fromkeys(deny))
    for name, paths in profiles.items():
        missing = [p for p in paths if p not in deny]
        if missing:
            raise _refusal(
                f"child_sandbox: profile {name!r} allow_read names paths not on the deny list: "
                f"{missing}"
            )
    exempt = set(profiles.get(profile, [])) if profile else set()
    return ChildSandboxPolicy(
        enabled=enabled,
        deny_read=tuple(p for p in deny if p not in exempt),
        allow_write=tuple(dict.fromkeys(allow)),
    )


def policy_from_dict(data: dict) -> ChildSandboxPolicy:
    """Rebuild a policy from ``ChildSandboxPolicy.to_dict`` output, validating it again."""
    data = _mapping("policy", data)
    _reject_unknown("policy", data, {"enabled", "deny_read", "allow_write"})
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _refusal("child_sandbox: enabled must be true or false")
    return ChildSandboxPolicy(
        enabled=enabled,
        deny_read=tuple(_path_list("deny_read", data.get("deny_read", []))),
        allow_write=tuple(_path_list("allow_write", data.get("allow_write", []))),
    )


def claude_settings_for(policy: ChildSandboxPolicy) -> dict[str, Any]:
    """Claude Code settings that confine a child to ``policy`` (``{}`` when disabled).

    ``sandbox.filesystem.denyRead`` stops Bash reads; the Read tool is stopped by the
    ``permissions.deny`` rules. In permission rules an absolute ``/x`` is written ``//x``,
    because a single leading slash is relative to the settings source.
    """
    if not policy.enabled:
        return {}
    rules: list[str] = []
    for path in policy.deny_read:
        rule_path = "/" + path if path.startswith("/") else path
        rules += [f"Read({rule_path})", f"Read({rule_path}/**)"]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": list(policy.deny_read),
                "allowWrite": list(policy.allow_write),
            },
        },
        "permissions": {"deny": rules},
    }


def _read_settings_value(value: Any, base_dir: Any = None) -> dict:
    """Read an existing ``--settings`` value: an inline JSON object or a path to a JSON file."""
    # ``str.__str__`` reads the text itself, as the request's own validator does.
    text = (str.__str__(value) if isinstance(value, str) else str(value)).strip()
    if text.startswith("{"):
        source, body = "inline settings", text
    else:
        path = Path(text).expanduser()
        if base_dir is not None and not path.is_absolute():
            path = Path(base_dir) / path
        source = f"settings file {text!r}"
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise _refusal(f"child_sandbox: cannot read {source}: {exc}") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise _refusal(f"child_sandbox: {source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _refusal(f"child_sandbox: {source} must be a JSON object")
    return data


def _merge(base: dict, ours: dict) -> dict:
    """Merge ``ours`` into ``base``: lists gain our entries, our scalars win, nothing is dropped."""
    merged = dict(base)
    for key, value in ours.items():
        current = merged.get(key)
        if current is None or not isinstance(value, (dict, list)):
            merged[key] = value
        elif isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        elif isinstance(value, list) and isinstance(current, list):
            merged[key] = current + [x for x in value if x not in current]
        else:
            raise _refusal(f"child_sandbox: existing settings key {key!r} has an unexpected type")
    return merged


def claude_settings_argument(
    policy: ChildSandboxPolicy, existing: Any = None, base_dir: Any = None
) -> str:
    """One inline-JSON ``--settings`` value: ``existing`` (if any) merged with the policy.

    ``existing`` may not carry a ``sandbox`` block of its own: its keys would join the policy's
    at the same settings level, and some of them (excluded commands, extra readable or writable
    paths) loosen the sandbox. The sandbox is configured through ``child_sandbox`` only.
    """
    base = _read_settings_value(existing, base_dir) if existing else {}
    if "sandbox" in base:
        raise _refusal(
            "child_sandbox: the settings handed to the child carry a sandbox block; configure "
            "the sandbox through child_sandbox in ~/.lionagi/settings.yaml instead"
        )
    return json.dumps(_merge(base, claude_settings_for(policy)))


#: Versions read so far, by binary. Only a successful read is kept, so a failed probe is
#: retried on the next launch rather than remembered for the life of the process.
_cli_versions: dict[str, tuple[int, ...]] = {}


def _cli_version(binary: str) -> tuple[int, ...] | None:
    """Version ``binary --version`` prints at the start of a line, or ``None`` when unreadable.

    A binary that cannot be executed, exits non-zero, or prints no version at the start of a
    line reads as ``None``.
    """
    if binary in _cli_versions:
        return _cli_versions[binary]
    try:
        proc = subprocess.run(  # noqa: S603 -- the resolved CLI binary and a literal flag, no shell
            [binary, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found = re.search(r"^\s*(\d+)\.(\d+)\.(\d+)\b", proc.stdout, re.MULTILINE)
    if not found:
        return None
    version = tuple(int(n) for n in found.groups())
    _cli_versions[binary] = version
    return version


def check_claude_cli_floor(binary: str) -> None:
    """Refuse a Claude CLI older than ``CLAUDE_CLI_FLOOR`` or whose version cannot be read."""
    version = _cli_version(binary)
    floor = ".".join(str(n) for n in CLAUDE_CLI_FLOOR)
    if version is None:
        raise _refusal(
            f"child_sandbox: cannot read the version of {binary!r}; the sandbox needs "
            f"claude >= {floor}"
        )
    if version < CLAUDE_CLI_FLOOR:
        raise _refusal(
            f"child_sandbox: claude {'.'.join(map(str, version))} is older than {floor} and would "
            "ignore the sandbox settings; upgrade it"
        )


def check_sandbox_backend() -> None:
    """Refuse a host on which Claude Code's sandbox has no backend.

    Native Windows has none. Linux and WSL2 need ``bwrap`` (bubblewrap) and ``socat`` on PATH.
    macOS needs nothing extra. Any other platform has no backend. Call it only when the policy
    is enabled: a disabled policy never asks the CLI for a sandbox.
    """
    if sys.platform == "win32":
        raise _refusal(
            "child_sandbox: Claude Code's sandbox has no backend on native Windows, so a child "
            "cannot run confined here; use WSL2 with bubblewrap (`bwrap`) and `socat` installed"
        )
    if sys.platform == "linux":
        missing = [p for p in _LINUX_BACKEND_PROGRAMS if shutil.which(p) is None]
        if missing:
            raise _refusal(
                "child_sandbox: Claude Code's sandbox on Linux and WSL2 needs bubblewrap (`bwrap`) "
                f"and `socat` on PATH; missing: {', '.join(missing)}"
            )
    elif sys.platform != "darwin":
        raise _refusal(
            f"child_sandbox: Claude Code's sandbox has no backend on {sys.platform}, so a child "
            "cannot run confined here"
        )
