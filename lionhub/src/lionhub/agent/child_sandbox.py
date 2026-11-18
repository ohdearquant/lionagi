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
import shlex
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
    "lionagi_home_config_paths",
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

#: Generic credential locations and local stores a child may not read. Entries are ``~/`` or absolute paths;
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
    "~/.secrets",
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
    # khive's default home: its stores, server socket and configuration. A child reaches khive
    # through the server it is handed, which runs outside the sandbox.
    "~/.khive",
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
#: are searched before the global ones, the per-run actor config that sets a child's identity
#: and namespace scope, and the dotenv files lionagi's settings load from the working directory
#: over the user-wide one (``lionagi.config.AppSettings``). A child can write its own working
#: directory and any project directory it is handed, so the file tools are denied these names
#: at every depth. The next child launched there cannot be handed configuration this one wrote.
PROJECT_CONFIG_NAMES: tuple[str, ...] = (
    ".claude",
    ".mcp.json",
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".lionagi/settings.yaml",
    ".lionagi/agents",
    ".lionagi/playbooks",
    ".khive/config.toml",
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


def _home_directories(env: Any = None) -> set[str]:
    """Every directory that is this account's home, resolved: this process's ``HOME``, the
    child's ``HOME`` when ``env`` carries one, and the account's home in the password
    database. The last one stays the real home when a parent was started with ``HOME``
    pointing somewhere else, as a harness, a scheduled job or a scratch-home test does.
    """
    homes = {os.path.realpath(os.path.expanduser("~"))}
    child_home = env.get("HOME") if hasattr(env, "get") else None
    if isinstance(child_home, str) and child_home:
        homes.add(os.path.realpath(os.path.expanduser(child_home)))
    try:
        import pwd

        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError, AttributeError):
        account_home = None
    if account_home:
        homes.add(os.path.realpath(account_home))
    return homes


def _reaches_home(path: str | Path, env: Any = None) -> bool:
    """Whether ``path`` is the home directory or an ancestor of it.

    The homes are those of ``_home_directories``. A path that exists is compared by identity
    (device and inode) with each home and each of its ancestors, since a spelling is not an
    identity: on a case-insensitive volume ``/users/u`` is the home ``/Users/u`` and
    ``realpath`` keeps the case it was given. The spelling comparison, after ``~`` expansion
    and link resolution, covers a path that does not exist yet.
    """
    homes = _home_directories(env)
    identities: set[tuple[int, int]] = set()
    for home in homes:
        for directory in (home, *map(str, Path(home).parents)):
            try:
                found = os.stat(directory)
            except OSError:
                continue
            identities.add((found.st_dev, found.st_ino))
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    try:
        found = os.stat(real)
        if (found.st_dev, found.st_ino) in identities:
            return True
    except OSError:
        pass
    return any(real == home or home.startswith(real.rstrip(os.sep) + os.sep) for home in homes)


def _refuse_a_home_workspace(
    workspace: str | Path, env: Any = None, *, enabled: bool | None = None
) -> None:
    """Refuse a child whose working directory is the home directory or an ancestor of it.

    The working directory is writable to a confined child by construction, so a workspace at
    the home directory, ``/Users`` or ``/`` makes the whole home writable: the same outcome as
    no sandbox. Applied while the child sandbox is enabled; ``enabled`` is the caller's already
    resolved switch, and ``None`` resolves the global one.
    """
    if enabled is None:
        enabled = resolve_child_sandbox(None).enabled
    if enabled and _reaches_home(workspace, env):
        raise _refusal(
            f"child_sandbox: the working directory {os.fspath(workspace)!r} is the home "
            "directory or an ancestor of it, which would make the whole home writable to the "
            "child; start the agent from a project directory"
        )


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


# Configuration under ``LIONAGI_HOME`` that later runs load: files, then directories.
_HOME_CONFIG_FILES = (
    "operator_hooks.json",
    "operator_hooks.settings.json",
    "operator_mcp.json",
    "operator_house_rules.md",
    "mcp_servers.json",
    ".mcp.json",
)
_HOME_CONFIG_DIRS = ("hooks", "playbooks", "skills")


# More links than this in one chain is treated as a loop, as the operating system does.
_MAX_LINK_HOPS = 40


def _link_hops(path: str) -> list[str]:
    """Every link ``path`` passes through, then the path it finally resolves to.

    Each hop is named, not only the last, because replacing any link in a chain redirects
    what the first one loads. Resolution is not strict, so a link whose target is missing
    still names that target.
    """
    hops: list[str] = []
    current = os.path.abspath(os.path.expanduser(path))
    for _ in range(_MAX_LINK_HOPS):
        if not os.path.islink(current):
            break
        try:
            target = os.readlink(current)
        except OSError:
            break
        parent = os.path.realpath(os.path.dirname(current))
        nxt = os.path.join(parent, target)
        name = os.path.basename(nxt)
        if name in ("", ".", ".."):
            nxt = os.path.realpath(nxt)
        else:
            nxt = os.path.join(os.path.realpath(os.path.dirname(nxt)), name)
        if nxt in hops:
            break
        hops.append(nxt)
        current = nxt
    try:
        hops.append(str(Path(path).expanduser().resolve()))
    except (OSError, RuntimeError):
        pass
    return list(dict.fromkeys(hops))


def _written_and_resolved(path: str) -> list[str]:
    """``path`` as written, then every link it passes through and the path it resolves to."""
    written = re.sub(r"/{2,}", "/", path).rstrip("/")
    return list(dict.fromkeys([written, *_link_hops(path)]))


def _link_targets_under(root: str) -> list[str]:
    """Every hop and target of each link in the tree at ``root``, linked directories followed."""
    targets: list[str] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        for name in (*dirnames, *filenames):
            entry = os.path.join(dirpath, name)
            if os.path.islink(entry):
                targets += _link_hops(entry)
    return targets


def lionagi_home_config_paths() -> tuple[str, ...]:
    """Configuration under ``LIONAGI_HOME`` that later runs load outside the sandbox.

    Studio's hook library and operator hooks become the Claude Code hooks of later operator
    turns, the MCP server registry, the ``.mcp.json`` synced from it and the operator's own MCP
    configuration start MCP servers, the operator's house rules are loaded into its later turns,
    and playbooks and skills are loaded into later runs. A child that rewrote any of them could
    steer or run code outside its sandbox the next time they are read.

    Each entry is named as written and as the path it resolves to, and every link inside the
    hook, playbook and skill directories is named by its target too, so a child cannot rewrite
    loaded configuration through a link target. A link whose target is missing names that target,
    so the child cannot create it either.
    """
    from lionagi._paths import LIONAGI_HOME

    home = str(LIONAGI_HOME)
    paths: list[str] = []
    for name in (*_HOME_CONFIG_FILES, *_HOME_CONFIG_DIRS):
        written = f"{home}/{name}"
        paths += _written_and_resolved(written)
        if name in _HOME_CONFIG_DIRS and os.path.isdir(written):
            paths += _link_targets_under(written)
    return tuple(dict.fromkeys(paths))


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
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise _refusal(f"child_sandbox: cannot read extends file {entry!r}: {exc}") from exc
        names = _written_and_resolved(entry.strip())
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


#: The file tools a ``PreToolUse`` hook holds to the working directory and ``allow_write``.
_FILE_TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"

#: Run by the child's ``PreToolUse`` hook as ``python -I -S -c <this> <roots JSON>``. It reads
#: the tool call from stdin and exits 0 when the target, symlinks resolved, is inside one of the
#: roots, and 2 (Claude Code's blocking exit) otherwise. The path is read from the key the named
#: tool writes (``notebook_path`` for NotebookEdit, ``file_path`` for the others); a call naming
#: another tool, carrying both keys, or giving a path that is not absolute is refused, since the
#: hook cannot know where Claude Code would resolve it. Every failure exits 2 as well: any
#: other code is a non-blocking hook error to Claude Code, and the write would go ahead.
_FILE_TOOL_SCOPE_CHILD = r"""
import json, os, sys

PATH_KEYS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
             "NotebookEdit": "notebook_path"}

def refuse(message):
    sys.stderr.write("lionagi file-tool scope: " + message + "\n")
    sys.exit(2)

def main():
    try:
        roots = json.loads(sys.argv[1])
        event = json.load(sys.stdin)
        tool = event.get("tool_name")
        tool_input = event.get("tool_input") or {}
        keys = [key for key in ("file_path", "notebook_path") if key in tool_input]
    except Exception as exc:
        refuse("cannot read the tool call (%s); the write is refused" % type(exc).__name__)
    if tool not in PATH_KEYS:
        refuse("%r is not a file tool this check knows; the write is refused" % (tool,))
    if keys != [PATH_KEYS[tool]]:
        refuse("a %s call must carry %s and no other path; the write is refused"
               % (tool, PATH_KEYS[tool]))
    target = tool_input[PATH_KEYS[tool]]
    if not isinstance(target, str) or not target or "\0" in target:
        refuse("the tool call names no usable file path; the write is refused")
    if not os.path.isabs(target):
        refuse("%s is not an absolute path; the write is refused" % target)
    real = os.path.realpath(target)
    for root in roots:
        base = os.path.realpath(root)
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            sys.exit(0)
    refuse(
        "%s is outside the working directory and the writable caches; file tools may write "
        "only under %s" % (target, ", ".join(roots))
    )

try:
    main()
except SystemExit:
    raise
except BaseException as exc:
    refuse("the scope check failed (%s); the write is refused" % type(exc).__name__)
"""


def _file_tool_scope_hook(workspace: str | Path, allow_write: tuple[str, ...]) -> dict[str, Any]:
    """The ``hooks`` settings that refuse file-tool writes outside ``workspace`` and ``allow_write``.

    The sandbox confines Bash only, and ``permissions.deny`` can name paths to refuse but not a
    set to allow, so a hook carries the allow list. It runs under ``bypassPermissions`` too.
    """
    if not sys.executable:
        raise _refusal(
            "child_sandbox: no Python interpreter to run the file-tool hook, so the child's "
            "Edit and Write tools cannot be confined"
        )
    roots = [str(Path(workspace).expanduser())]
    roots += [os.path.expanduser(path) for path in allow_write]
    # ``|| exit 2`` turns an interpreter that fails to start (127, 126) into a refusal too.
    command = (
        shlex.join([sys.executable, "-I", "-S", "-c", _FILE_TOOL_SCOPE_CHILD, json.dumps(roots)])
        + " || exit 2"
    )
    # ``disableAllHooks`` from the workspace's own ``.claude/settings.json`` or
    # ``settings.local.json`` would switch the hook off; this settings level is above those.
    return {
        "disableAllHooks": False,
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": _FILE_TOOL_MATCHER,
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        },
    }


def claude_settings_for(
    policy: ChildSandboxPolicy, workspace: str | Path | None = None
) -> dict[str, Any]:
    """Claude Code settings that confine a child to ``policy`` (``{}`` when disabled).

    ``sandbox.filesystem.denyRead`` stops Bash reads; the Read tool is stopped by the
    ``permissions.deny`` rules. Bash writes are held to the working directory and
    ``allowWrite`` by the sandbox, which does not cover the file tools, so ``Edit`` rules deny
    them ``policy.deny_edit``, ``DEFAULT_DENY_EDIT``, the read list, the run files
    (``run_state_paths``), the configuration later runs load from the lionagi home
    (``lionagi_home_config_paths``) and every ``PROJECT_CONFIG_NAMES`` entry at any depth. Every
    other path outside the working directory stays writable to the file tools unless
    ``workspace`` is given: then a ``PreToolUse`` hook also refuses Edit, Write, MultiEdit and
    NotebookEdit on any target outside ``workspace`` and ``allow_write``. Claude Code
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
    edit = [
        *DEFAULT_DENY_EDIT,
        *policy.deny_edit,
        *policy.deny_read,
        *run_state_paths(),
        *lionagi_home_config_paths(),
    ]
    for path in dict.fromkeys(edit):
        rules += [f"Edit({rule_path(path)})", f"Edit({rule_path(path)}/**)"]
    for name in PROJECT_CONFIG_NAMES:
        rules += [f"Edit(//**/{name})", f"Edit(//**/{name}/**)"]
    settings: dict[str, Any] = {
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
    if workspace is not None:
        settings.update(_file_tool_scope_hook(workspace, policy.allow_write))
    return settings


#: The settings sources a confined child may load. The project's ``.claude/settings.json`` and
#: ``settings.local.json`` belong to the working directory the child is confined to, which may be
#: a checkout under review: hooks, helper commands and plugins named there run outside the
#: sandbox, and ``disableAllHooks`` there would switch off the file-tool hook.
_CHILD_SETTING_SOURCES: tuple[str, ...] = ("user",)


def _child_setting_sources(requested: str | None) -> str:
    """The ``--setting-sources`` value for a confined child.

    ``None`` or a value naming no source becomes ``user``: without the flag Claude Code loads
    every source. A request naming ``project`` or ``local``, or a source Claude Code does not
    know, is refused; a request naming only ``user`` is kept.
    """
    names = [name.strip() for name in (requested or "").split(",") if name.strip()]
    if not names:
        return ",".join(_CHILD_SETTING_SOURCES)
    unknown = [name for name in names if name not in ("user", "project", "local")]
    if unknown:
        raise _refusal(f"child_sandbox: unknown setting sources {unknown!r}")
    loose = [name for name in names if name not in _CHILD_SETTING_SOURCES]
    if loose:
        raise _refusal(
            f"child_sandbox: a confined child cannot load the {', '.join(loose)} settings: they "
            "come from the working directory, and hooks or helper commands named there run "
            "outside the sandbox"
        )
    return ",".join(names)


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
    policy: ChildSandboxPolicy,
    existing: Any = None,
    base_dir: Any = None,
    workspace: str | Path | None = None,
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
    ours = claude_settings_for(policy, workspace)
    if "disableAllHooks" in ours and base.get("disableAllHooks", False) is not False:
        raise _refusal(
            "child_sandbox: the settings handed to the child set disableAllHooks, which would "
            "switch off the hook that keeps its file tools inside the working directory"
        )
    return json.dumps(_merge(base, ours))


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
