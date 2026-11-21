# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Leg-host mode: a Studio daemon that runs agent legs for a caller on another machine.

Set ``LIONAGI_STUDIO_LEG_HOST=1`` and the daemon accepts one shape of work: an ``agent``
launch naming a configured repository and a full commit, which it runs in a detached
worktree it makes for that launch and removes afterwards. Everything else a caller could
ask for is refused, and the refusals are listed in docs/guides/remote-legs.md.

What the daemon holds, it never hands to a leg: the leg's environment is an allow-list,
its MCP configuration is off, and its working tree is one the daemon checked out itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lionagi._paths import find_lionagi_dirs
from lionhub.orchestration.scheduler.settings import _env_flag

__all__ = (
    "LEG_HOST_ENV",
    "LegLaunch",
    "LegSetupError",
    "LegRepo",
    "capabilities",
    "enabled",
    "leg_env",
    "load_repos",
    "make_worktree",
    "max_launches",
    "read_result",
    "remove_worktree",
    "result_path",
    "startup_refusals",
    "sweep_worktrees",
    "validate_launch",
    "worktree_refusal",
    "write_result",
)

LEG_HOST_ENV = "LIONAGI_STUDIO_LEG_HOST"
_MAX_LAUNCHES_ENV = "LIONAGI_STUDIO_LEG_HOST_MAX_LAUNCHES"
_DEFAULT_MAX_LAUNCHES = 2
_TOKEN_ENV = "LIONAGI_STUDIO_AUTH_TOKEN"  # noqa: S105 — a variable name, not a value

#: The most of a leg's standard output kept as its result: the last bytes, where its final
#: message is.
RESULT_CAP_BYTES = 1024 * 1024

_HEAD = re.compile(r"[0-9a-f]{40}")
_BARE_NAME = re.compile(r"[a-z0-9_-]+")
#: Invocation ids are ``short_id`` values: lowercase hex. Nothing else names a worktree.
_INVOCATION_ID = re.compile(r"[0-9a-f]{8,32}")

#: Private network ranges a tailnet assigns. A bind or a peer outside these and loopback is
#: reachable from networks the operator did not choose.
_TAILNET_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

#: The only providers whose children run under a sandbox that denies reads of the account's
#: credential files. A profile on any other provider is refused here until that is measured.
_CONTAINED_PROVIDERS = frozenset({"claude_code"})

#: The variables a leg receives from the daemon's environment. Everything else, the daemon's
#: bearer included, stays with the daemon.
_LEG_ENV_ALLOW = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # Where the leg finds its lionagi home and its subscription accounts: the ones the daemon
    # checked a named account against, rather than the defaults an unconfigured process uses.
    "LIONAGI_HOME",
    "LIONAGI_LOGINS_DIR",
)

#: Request fields a leg-host launch may carry. Any other field set to a value is refused.
_ALLOWED_FIELDS = frozenset(
    {
        "action_kind",
        "action_agent",
        "action_prompt",
        "action_repo",
        "action_head",
        "action_account",
    }
)

#: Files in a worktree that would configure the leg from the tree under review.
_TREE_CONFIG_NAMES = (".lionagi", ".mcp.json", ".claude")


class LegSetupError(RuntimeError):
    """A leg could not be given its worktree; the launch fails with this message."""


def enabled() -> bool:
    """True when this daemon runs in leg-host mode."""
    return _env_flag(LEG_HOST_ENV, default=False)


def max_launches() -> int:
    """The most leg launches that may run at once (default 2)."""
    raw = os.environ.get(_MAX_LAUNCHES_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_LAUNCHES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{_MAX_LAUNCHES_ENV} must be a positive integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{_MAX_LAUNCHES_ENV} must be a positive integer, got {raw!r}")
    return value


def capabilities() -> dict[str, Any]:
    """What ``GET /api/launches/capabilities`` reports, so a client can refuse a plain daemon."""
    return {
        "leg_host": enabled(),
        "pinned_head": enabled(),
        "named_account": enabled(),
        "max_concurrent": max_launches(),
    }


def _lionagi_home() -> Path:
    return Path.home() / ".lionagi"


@dataclass(frozen=True)
class LegRepo:
    """A repository the leg host may check out: its clone and the root its worktrees go under."""

    name: str
    clone: Path
    worktrees: Path


def load_repos(path: Path | None = None) -> dict[str, LegRepo]:
    """Read ``~/.lionagi/leg_repos.yaml``: ``{name: {clone: PATH, worktrees: PATH}}``.

    Both paths must be absolute after ``~`` expansion. A missing file is no repositories.
    """
    path = path or _lionagi_home() / "leg_repos.yaml"
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must map repository names to {{clone, worktrees}}")
    repos: dict[str, LegRepo] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not _BARE_NAME.fullmatch(name):
            raise ValueError(f"{path}: repository name {name!r} must match [a-z0-9_-]+")
        if not isinstance(entry, dict) or set(entry) != {"clone", "worktrees"}:
            raise ValueError(f"{path}: {name} must have exactly 'clone' and 'worktrees'")
        clone, worktrees = (Path(str(entry[key])).expanduser() for key in ("clone", "worktrees"))
        if not clone.is_absolute() or not worktrees.is_absolute():
            raise ValueError(f"{path}: {name}'s clone and worktrees must be absolute paths")
        repos[name] = LegRepo(name=name, clone=clone, worktrees=worktrees)
    return repos


def _is_loopback_or_tailnet(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return address == "localhost"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or any(ip in net for net in _TAILNET_NETS)


def _named_mcp_servers(path: Path, key: str) -> list[str]:
    """The MCP server names *path*'s JSON lists under *key*; an unreadable file names one."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"<unreadable {path.name}>"]
    servers = data.get(key) if isinstance(data, dict) else None
    return sorted(servers) if isinstance(servers, dict) else []


def startup_refusals(*, host: str) -> list[str]:
    """Why this daemon must not start in leg-host mode; empty when it may."""
    reasons = []
    if not os.environ.get(_TOKEN_ENV):
        reasons.append(f"{_TOKEN_ENV} is not set: a leg host never serves without its bearer")
    if not _is_loopback_or_tailnet(host):
        reasons.append(
            f"the bind address {host!r} is neither loopback nor a tailnet address: a leg host "
            "listens only where the operator's own private network reaches it"
        )
    home = Path.home()
    for path, key in (
        (_lionagi_home() / ".mcp.json", "mcpServers"),
        (home / ".claude.json", "mcpServers"),
    ):
        named = _named_mcp_servers(path, key)
        if named:
            reasons.append(
                f"{path} names MCP servers ({', '.join(named)}): a remote leg reaches no MCP server"
            )
    if not load_repos():
        reasons.append("~/.lionagi/leg_repos.yaml configures no repository")
    reasons.extend(_sandbox_refusals())
    return reasons


def _sandbox_refusals() -> list[str]:
    """Why the host's child sandbox cannot confine a leg; empty when it can."""
    from lionagi._errors import ConfigurationError
    from lionhub.agent.child_sandbox import resolve_child_sandbox

    try:
        policy = resolve_child_sandbox(None)
    except ConfigurationError as exc:
        return [f"the child sandbox settings do not resolve: {exc}"]
    if not policy.enabled:
        return ["child_sandbox is disabled in ~/.lionagi/settings.yaml: every leg runs under it"]
    return []


def _lifted_read_denials(agent: str) -> list[str]:
    """The default read denials *agent*'s profile is exempted from on this host."""
    from lionhub.agent.child_sandbox import DEFAULT_DENY_READ, resolve_child_sandbox

    denied = set(resolve_child_sandbox(agent).deny_read)
    return [path for path in DEFAULT_DENY_READ if path not in denied]


@dataclass(frozen=True)
class LegLaunch:
    """A validated leg-host launch."""

    repo: LegRepo
    head: str
    agent: str
    account: str | None = None
    """The subscription account the leg runs on in place of its role's, passed as
    ``li agent --account``; None runs it on the account its role names, if any."""


def _profile_provider(agent: str) -> str | None:
    """The provider *agent*'s profile in ``~/.lionagi/agents`` runs on, or None if it names none."""
    from lionagi.service.connections.registry import EndpointRegistry
    from lionagi.service.providers import parse_model_spec
    from lionhub.orchestration.spawn import load_agent_profile

    # Read from the account home, so only ~/.lionagi/agents is searched: a working
    # directory's project profiles never decide what a leg runs as.
    profile = load_agent_profile(agent, cwd=Path.home())
    if not profile.model:
        return None
    spec = parse_model_spec(profile.model).model
    if "/" not in spec:
        return None
    return EndpointRegistry.canonical_provider(spec.split("/", 1)[0])


def validate_launch(data: dict[str, Any]) -> LegLaunch:
    """The leg-host refusals for one launch request; raises ValueError (a 422) on the first."""
    extra = sorted(
        k for k, v in data.items() if v not in (None, [], "") and k not in _ALLOWED_FIELDS
    )
    if extra:
        raise ValueError(f"a leg host accepts no {', '.join(extra)}")
    if data.get("action_kind") != "agent":
        raise ValueError(f"a leg host runs only agent launches, not {data.get('action_kind')!r}")
    agent = data.get("action_agent")
    if not isinstance(agent, str) or not _BARE_NAME.fullmatch(agent):
        raise ValueError(
            "action_agent must be a bare profile name ([a-z0-9_-]+) from the host's own "
            "~/.lionagi/agents"
        )
    if not (data.get("action_prompt") or "").strip():
        raise ValueError("action_prompt is required")
    head = data.get("action_head")
    if not isinstance(head, str) or not _HEAD.fullmatch(head):
        raise ValueError("action_head must be a full 40-character lowercase hex commit")
    repos = load_repos()
    repo = repos.get(data.get("action_repo") or "")
    if repo is None:
        raise ValueError(
            f"action_repo {data.get('action_repo')!r} is not a repository this host serves"
        )
    try:
        provider = _profile_provider(agent)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(f"profile {agent!r} cannot be read on this host: {exc}") from exc
    if provider not in _CONTAINED_PROVIDERS:
        raise ValueError(
            f"profile {agent!r} runs on {provider or 'no named provider'}; a leg host runs only "
            f"{', '.join(sorted(_CONTAINED_PROVIDERS))} profiles, whose sandbox denies reads of "
            "the account's credential files"
        )
    lifted = _lifted_read_denials(agent)
    if lifted:
        raise ValueError(
            f"profile {agent!r} is exempted from reading {', '.join(lifted)}; a leg host lifts no "
            "read denial"
        )
    account = data.get("action_account")
    if account in (None, ""):
        account = None
    else:
        from lionhub.agent.subscription_accounts import (
            SubscriptionAccountError,
            resolve_account,
            validate_account_name,
        )

        # Checked against the accounts directory the leg is handed: a malformed name, or one
        # with no entry for the profile's provider, is refused before a worktree is made. The
        # credential itself is read only by the leg, so an entry whose token cannot be read
        # fails the leg, not the launch. The refusal names no path on this host.
        try:
            validate_account_name(account, where="action_account")
        except SubscriptionAccountError as exc:
            raise ValueError(str(exc)) from None
        try:
            resolve_account(account, provider)
        except SubscriptionAccountError:
            raise ValueError(
                f"action_account: {account!r} is not an account this host has for {provider}"
            ) from None
    return LegLaunch(repo=repo, head=head, agent=agent, account=account)


def _worktree_path(repo: LegRepo, invocation_id: str) -> Path:
    """``<worktrees>/<invocation_id>``, refused unless the id is a daemon id and the path sits
    strictly inside the root."""
    if not _INVOCATION_ID.fullmatch(invocation_id):
        raise ValueError(f"invocation id {invocation_id!r} is not one this daemon issues")
    root = repo.worktrees.resolve()
    path = (root / invocation_id).resolve()
    if path.parent != root:
        raise ValueError(f"worktree path {path} is not directly under {root}")
    return path


async def _git(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=leg_env(os.environ),
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")[-2000:]


async def make_worktree(launch: LegLaunch, invocation_id: str) -> Path:
    """Fetch *launch*'s head into the clone and check it out detached at ``<root>/<id>``."""
    path = _worktree_path(launch.repo, invocation_id)
    launch.repo.worktrees.mkdir(parents=True, exist_ok=True)
    clone = str(launch.repo.clone)
    rc, out = await _git("-C", clone, "fetch", "--quiet", "origin", launch.head)
    if rc != 0:
        raise LegSetupError(f"cannot fetch {launch.head} into {launch.repo.name}: {out.strip()}")
    rc, out = await _git("-C", clone, "worktree", "add", "--detach", str(path), launch.head)
    if rc != 0:
        raise LegSetupError(f"cannot make the worktree for {launch.head}: {out.strip()}")
    rc, out = await _git("-C", str(path), "rev-parse", "HEAD")
    if rc != 0 or out.strip() != launch.head:
        await remove_worktree(launch.repo, invocation_id)
        raise LegSetupError(f"the worktree is at {out.strip()!r}, not {launch.head}")
    return path


def worktree_refusal(path: Path) -> str | None:
    """Why the tree at *path* may not host a leg: it carries configuration a leg would load."""
    found = [name for name in _TREE_CONFIG_NAMES if (path / name).exists()]
    shadowing = [d for d in find_lionagi_dirs(path) if d.resolve() != _lionagi_home().resolve()]
    if shadowing:
        found.extend(str(d) for d in shadowing if str(d) not in found)
    if not found:
        return None
    return (
        f"the tree carries configuration a leg would load ({', '.join(found)}); a leg runs on "
        "the host's own configuration only"
    )


async def remove_worktree(repo: LegRepo, invocation_id: str) -> bool:
    """Remove ``<root>/<invocation_id>`` with ``git worktree remove`` and nothing else.

    A path that fails the id or containment check is left where it is. Returns whether git
    reported the removal.
    """
    try:
        path = _worktree_path(repo, invocation_id)
    except ValueError:
        return False
    rc, _out = await _git("-C", str(repo.clone), "worktree", "remove", "--force", str(path))
    return rc == 0


async def sweep_worktrees(repo: LegRepo, *, live: set[str]) -> list[str]:
    """Remove each leftover worktree under *repo*'s root whose invocation is not in *live*."""
    root = repo.worktrees
    if not root.is_dir():
        return []
    removed = []
    for entry in sorted(root.iterdir()):
        if entry.name in live or not _INVOCATION_ID.fullmatch(entry.name):
            continue
        if await remove_worktree(repo, entry.name):
            removed.append(entry.name)
    return removed


def leg_env(environ: Any) -> dict[str, str]:
    """The allow-listed subset of *environ* a leg is started with."""
    return {name: environ[name] for name in _LEG_ENV_ALLOW if environ.get(name)}


def _results_root() -> Path:
    return _lionagi_home() / "leg_results"


def result_path(invocation_id: str) -> Path:
    """Where a leg's result is kept: under the host's own state, never in the worktree."""
    if not _INVOCATION_ID.fullmatch(invocation_id):
        raise ValueError(f"invocation id {invocation_id!r} is not one this daemon issues")
    return _results_root() / f"{invocation_id}.json"


def write_result(invocation_id: str, stdout: bytes, *, total: int) -> None:
    """Keep the last :data:`RESULT_CAP_BYTES` of a leg's standard output as its result."""
    path = result_path(invocation_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "result": stdout[-RESULT_CAP_BYTES:].decode(errors="replace"),
        "truncated": total > RESULT_CAP_BYTES,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def read_result(invocation_id: str) -> dict[str, Any] | None:
    """The stored result of *invocation_id*, or None while it has none."""
    path = result_path(invocation_id)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
