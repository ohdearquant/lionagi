# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Sandbox policy for CLI children.

One list names the credential locations a child must not read, the toolchain caches it may
write, and the configuration a child must not edit. It is resolved from the global lionagi
settings file only and rendered into the settings Claude Code understands. Any malformed value
raises ``ConfigurationError`` so a child never starts unconfined because of a configuration
mistake. Every refusal raised here ends with the same sentence naming the opt-out,
``CHILD_SANDBOX_OPT_OUT``.
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
    "DEFAULT_DENY_EDIT",
    "DEFAULT_DENY_READ",
    "PROJECT_CONFIG_NAMES",
    "ChildSandboxPolicy",
    "check_claude_cli_floor",
    "check_sandbox_backend",
    "claude_settings_argument",
    "claude_settings_for",
    "default_allow_write",
    "effective_policy",
    "policy_from_dict",
    "resolve_child_sandbox",
    "run_state_paths",
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
    "~/.claude.json",
    "~/.claude/settings.json",
    "~/.claude/settings.local.json",
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

#: Configuration a child may not change with its file tools: the files that decide how the next
#: child is launched and confined, and what Claude Code loads into every session. The sandbox's
#: write limit covers Bash only, so these are denied to the Edit and Write tools by permission
#: rules. ``~/.claude.json`` holds Claude Code's user-scope MCP servers, which run outside the
#: sandbox. It and the two user settings files are also in ``DEFAULT_DENY_READ``, but their entries
#: here are not redundant: a profile exempted from reading one drops it from ``deny_read``, and
#: this list is what keeps it denied to the file tools then. Every other ``deny_read`` entry, one
#: a profile is exempted from reading included, and every ``extends`` file is denied the same way.
DEFAULT_DENY_EDIT: tuple[str, ...] = (
    "~/.lionagi/settings.yaml",
    "~/.lionagi/agents",
    "~/.claude.json",
    "~/.claude/settings.json",
    "~/.claude/settings.local.json",
    "~/.claude/CLAUDE.md",
    "~/.claude/hooks",
    "~/.claude/plugins",
    "~/.claude/agents",
    "~/.claude/skills",
    "~/.claude/commands",
    "~/.claude/projects/*/memory",
)

#: Configuration Claude Code and lionagi load from a working directory, its parents or the git
#: root: project settings, hooks, commands, agents and skills under ``.claude``, project MCP
#: servers, memory files, lionagi's project settings, agent profiles and playbooks, which
#: are searched before the global ones, and the dotenv files lionagi's settings load from the
#: working directory over the user-wide one (``lionagi.config.AppSettings``). A child can write its own working directory and any
#: project directory it is handed, so these names are denied to the file tools wherever they
#: sit, and the next child launched there cannot be handed configuration this one wrote.
PROJECT_CONFIG_NAMES: tuple[str, ...] = (
    ".claude",
    ".mcp.json",
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".lionagi/settings.yaml",
    ".lionagi/agents",
    ".lionagi/playbooks",
    ".env",
    ".env.local",
    ".secrets.env",
)

#: Programs Claude Code's sandbox needs on Linux and WSL2.
_LINUX_BACKEND_PROGRAMS: tuple[str, ...] = ("bwrap", "socat")

_ROOT_KEYS = frozenset({"enabled", "deny_read", "allow_write", "deny_edit", "profiles", "extends"})
_LAYER_KEYS = frozenset({"deny_read", "allow_write", "deny_edit", "profiles"})

#: Project settings files already warned about, so each is reported once per process.
_warned_project_files: set[str] = set()


def _refusal(message: str) -> ConfigurationError:
    """A ``ConfigurationError`` whose text ends with the one-sentence opt-out."""
    return ConfigurationError(f"{message.rstrip().rstrip('.;:')}. {CHILD_SANDBOX_OPT_OUT}")


@dataclass(frozen=True)
class ChildSandboxPolicy:
    """Resolved sandbox policy: whether it applies, what stays unreadable, what stays writable,
    and what the file tools may not change.

    ``deny_edit`` adds to the rules every policy renders: ``DEFAULT_DENY_EDIT`` and the
    ``deny_read`` entries are denied to the file tools whatever it holds. ``profile`` is the
    agent profile the policy was resolved for, so a later launch from a stored policy can
    resolve it again for that profile (``effective_policy``).
    """

    enabled: bool
    deny_read: tuple[str, ...]
    allow_write: tuple[str, ...]
    deny_edit: tuple[str, ...] = ()
    profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form that survives endpoint kwargs, clones and resumes."""
        return {
            "enabled": self.enabled,
            "deny_read": list(self.deny_read),
            "allow_write": list(self.allow_write),
            "deny_edit": list(self.deny_edit),
            "profile": self.profile,
        }


def default_allow_write() -> tuple[str, ...]:
    """``DEFAULT_ALLOW_WRITE`` plus ``$UV_CACHE_DIR`` when it names an absolute or ``~/`` path."""
    extra = []
    uv_dir = os.environ.get("UV_CACHE_DIR", "").strip().rstrip("/")
    if uv_dir.startswith(("~/", "/")):
        extra.append(uv_dir)
    return tuple(dict.fromkeys([*extra, *DEFAULT_ALLOW_WRITE]))


def run_state_paths() -> tuple[str, ...]:
    """The run files a later process reads back to rebuild an agent, under ``LIONAGI_HOME``.

    A resumed branch takes its model, and the sandbox policy stored with it, from these files,
    so a child that rewrote them could hand a later run a policy of its choosing. A run's
    ``artifacts`` directory, where flow and fanout workers run, is not among them.
    """
    from lionagi._paths import LIONAGI_HOME

    home = str(LIONAGI_HOME)
    return (
        *(
            f"{home}/runs/*/{name}"
            for name in ("run.json", "checkpoint.json", "branches", "stream")
        ),
        f"{home}/logs/agents",
        f"{home}/last_branch.json",
    )


def _lionagi_home_credentials() -> tuple[str, ...]:
    """Credential files lionagi itself reads from ``LIONAGI_HOME``: its user-wide ``.env``."""
    from lionagi._paths import LIONAGI_HOME

    env = LIONAGI_HOME / ".env"
    try:
        # Written the way a user writes it in ``allow_read`` when it sits under the home.
        return (f"~/{env.relative_to(Path.home()).as_posix()}",)
    except ValueError:
        return (str(env),)


def _profile_file_is_global(profile: str) -> bool:
    """Whether ``profile`` resolves to a file in the user's ``~/.lionagi/agents``.

    The directories are searched the way ``load_agent_profile`` searches them, project ones
    first, so a project that declares the same name shadows the global file and this returns
    False. A plugin profile, a name no directory declares and an ambiguous name all return False.
    """
    from lionagi._paths import _resolve_profile_path, find_lionagi_dirs

    home = (Path.home() / ".lionagi").resolve()
    for directory in find_lionagi_dirs():
        try:
            path = _resolve_profile_path(directory / "agents", profile)
        except (OSError, ValueError):
            return False
        if path is not None:
            return directory.resolve() == home
    return False


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


def _layer(label: str, raw: dict) -> tuple[list[str], list[str], list[str], dict[str, list[str]]]:
    """Validate one layer (the settings block or an extends file) into its four parts."""
    _reject_unknown(label, raw, _LAYER_KEYS)
    deny = _path_list(f"{label}.deny_read", raw.get("deny_read", []))
    allow = _path_list(f"{label}.allow_write", raw.get("allow_write", []))
    edit = _path_list(f"{label}.deny_edit", raw.get("deny_edit", []))
    profiles: dict[str, list[str]] = {}
    for name, entry in _mapping(f"{label}.profiles", raw.get("profiles", {})).items():
        if not isinstance(name, str) or not name:
            raise _refusal(f"child_sandbox: {label}.profiles has a non-string name")
        entry = _mapping(f"{label}.profiles.{name}", entry)
        _reject_unknown(f"{label}.profiles.{name}", entry, {"allow_read"})
        profiles[name] = _path_list(
            f"{label}.profiles.{name}.allow_read", entry.get("allow_read", [])
        )
    return deny, allow, edit, profiles


def _read_extends(raw: Any) -> list[tuple[list[str], dict]]:
    """Each extends file as (the paths that name it, its parsed layer).

    The paths are the entry as written and, when it differs, the file it resolves to, so a
    child cannot edit the file through a link target the entry does not spell.
    """
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise _refusal("child_sandbox: extends must be a list of non-empty strings")
    layers = []
    for entry in raw:
        if not entry.strip().startswith(("~/", "/")):
            raise _refusal(
                f"child_sandbox: extends entry {entry!r} must be an absolute path or start "
                "with '~/'"
            )
        path = Path(entry.strip()).expanduser()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            real = str(path.resolve(strict=True))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise _refusal(f"child_sandbox: cannot read extends file {entry!r}: {exc}") from exc
        names = list(dict.fromkeys([re.sub(r"/{2,}", "/", entry.strip()).rstrip("/"), real]))
        layers.append((names, _mapping(f"extends file {entry!r}", {} if data is None else data)))
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
    extends = _read_extends(raw.get("extends", []))
    layers += [(f"extends[{i}]", d) for i, (_, d) in enumerate(extends)]
    deny: list[str] = [*DEFAULT_DENY_READ, *_lionagi_home_credentials()]
    allow: list[str] = list(default_allow_write())
    edit: list[str] = [name for names, _ in extends for name in names]
    profiles: dict[str, list[str]] = {}
    for label, layer in layers:
        d, a, e, p = _layer(label, layer)
        deny += d
        allow += a
        edit += e
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
    exempt: set[str] = set()
    if profile and profiles.get(profile):
        # The exemption is the user's, written for the profile in their global agents
        # directory; a project file of the same name, searched first, does not inherit it.
        if _profile_file_is_global(profile):
            exempt = set(profiles[profile])
        else:
            logger.warning(
                "child_sandbox: profile %r has an allow_read exemption, but that name does not "
                "resolve to a file in ~/.lionagi/agents; the exemption is not applied",
                profile,
            )
    return ChildSandboxPolicy(
        enabled=enabled,
        deny_read=tuple(p for p in deny if p not in exempt),
        allow_write=tuple(dict.fromkeys(allow)),
        # A read exemption never lifts the edit denial, so the whole read list is kept here.
        deny_edit=tuple(dict.fromkeys([*DEFAULT_DENY_EDIT, *edit, *deny])),
        profile=profile or None,
    )


def policy_from_dict(data: dict) -> ChildSandboxPolicy:
    """Rebuild a policy from ``ChildSandboxPolicy.to_dict`` output, validating it again."""
    data = _mapping("policy", data)
    _reject_unknown("policy", data, {"enabled", "deny_read", "allow_write", "deny_edit", "profile"})
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _refusal("child_sandbox: enabled must be true or false")
    profile = data.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise _refusal("child_sandbox: profile must be a profile name or null")
    return ChildSandboxPolicy(
        enabled=enabled,
        deny_read=tuple(_path_list("deny_read", data.get("deny_read", []))),
        allow_write=tuple(_path_list("allow_write", data.get("allow_write", []))),
        deny_edit=tuple(_path_list("deny_edit", data.get("deny_edit", []))),
        profile=profile,
    )


def effective_policy(stored: ChildSandboxPolicy) -> ChildSandboxPolicy:
    """The policy a launch from ``stored`` runs under, given the global settings now.

    A stored policy comes back from run files on resume and from a caller's endpoint kwargs,
    so it can only add to what the global settings resolve now for the profile it names: the
    global ``enabled`` decides whether the sandbox applies, every path the settings deny stays
    denied (a profile exemption holds only while it is still configured and the profile still
    resolves to the global file), and ``allow_write`` keeps only entries the settings allow.

    Raises:
        ConfigurationError: the global settings do not resolve.
    """
    current = resolve_child_sandbox(stored.profile)
    if not current.enabled:
        return current
    return ChildSandboxPolicy(
        enabled=True,
        deny_read=tuple(dict.fromkeys([*stored.deny_read, *current.deny_read])),
        allow_write=tuple(p for p in stored.allow_write if p in current.allow_write)
        if stored.enabled
        else current.allow_write,
        deny_edit=tuple(dict.fromkeys([*stored.deny_edit, *current.deny_edit])),
        profile=current.profile,
    )


def claude_settings_for(policy: ChildSandboxPolicy) -> dict[str, Any]:
    """Claude Code settings that confine a child to ``policy`` (``{}`` when disabled).

    ``sandbox.filesystem.denyRead`` stops Bash reads; the Read tool is stopped by the
    ``permissions.deny`` rules. Bash writes are held to the working directory and
    ``allowWrite`` by the sandbox, which does not cover the file tools, so ``Edit`` rules deny
    them ``policy.deny_edit``, ``DEFAULT_DENY_EDIT``, the read list, the run files
    (``run_state_paths``) and every ``PROJECT_CONFIG_NAMES`` entry at any depth. Claude Code
    also applies ``Edit`` denials to the sandbox's write limit, so Bash is refused the same
    paths inside the working directory. In permission rules an absolute ``/x`` is written
    ``//x``, because a single leading slash is relative to the settings source.
    """
    if not policy.enabled:
        return {}

    def rule_path(path: str) -> str:
        return "/" + path if path.startswith("/") else path

    rules: list[str] = []
    for path in policy.deny_read:
        rules += [f"Read({rule_path(path)})", f"Read({rule_path(path)}/**)"]
    edit = [*DEFAULT_DENY_EDIT, *policy.deny_edit, *policy.deny_read, *run_state_paths()]
    for path in dict.fromkeys(edit):
        rules += [f"Edit({rule_path(path)})", f"Edit({rule_path(path)}/**)"]
    for name in PROJECT_CONFIG_NAMES:
        rules += [f"Edit(//**/{name})", f"Edit(//**/{name}/**)"]
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
