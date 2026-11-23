# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""macOS Seatbelt confinement for the editor and bash an API model is handed (ADR-0041 Amendments 2
and 4).

The policy takes the shape of the Codex CLI's macOS sandbox: deny by default, reads unrestricted
except for the paths a caller names as unreadable, writes only under the writable roots, network
only when asked for and never the keychain service. A policy given read roots instead allows reads
only under the writable roots, the read roots and a fixed system set; whether that holds is
measured by `lionhub.tools.seatbelt_read_probe`, and no launch builds such a policy yet. Seatbelt judges the resolved path, so the roots
and the unreadable paths are resolved before they are passed, and a read or write through a
symlink is judged where the link points.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lionagi._command_env_metadata import command_environment
from lionagi._errors import ConfigurationError

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Reads are unrestricted here, sysctl reads included; a caller's unreadable paths are denied after
# this, and what the base confines is writes and network. A policy with read roots replaces the
# `(allow file-read*)` line with the rules `SeatbeltPolicy._read_rules` renders.
_BASE_POLICY = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow file-read*)
(allow file-read* file-write* file-ioctl
  (literal "/dev/null")
  (literal "/dev/tty")
  (literal "/dev/ptmx"))
(allow file-write* (regex #"^/dev/fd/(1|2)$"))
(allow pseudo-tty)
(allow ipc-posix-sem)
(allow user-preference-read)
(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs."))
(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo")
  (global-name "com.apple.cfprefsd.daemon")
  (global-name "com.apple.cfprefsd.agent")
  (local-name "com.apple.cfprefsd.agent"))
"""

# The network rules leave out the keychain service (com.apple.SecurityServer): with it, a confined
# command can ask the security tool for any item whose access list admits that tool, and making
# the keychain files unreadable does not stop it, since the service reads them, not the command.
_NETWORK_POLICY = """(allow network-outbound)
(allow network-inbound)
(allow system-socket (socket-domain AF_UNIX))
(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
  (global-name "com.apple.TrustEvaluationAgent"))
(allow sysctl-read (sysctl-name-regex #"^net.routetable"))
"""

# Runs as `python -I -S -c`: the confined editor checks the descriptor it will write.
#: The pseudo-terminals a CLI's shell tool allocates for each command it runs. Only a terminal
#: the confined process allocated itself carries the ``com.apple.sandbox.pty`` extension, so a
#: terminal it did not allocate (another program's, a terminal multiplexer's pane) cannot be
#: opened, written to or sent ioctls.
_PTY_POLICY = """(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write* file-ioctl
  (require-all (regex #"^/dev/ttys[0-9]+$") (extension "com.apple.sandbox.pty")))
"""

#: Readable under a policy with read roots, beside the roots themselves: the system directories a
#: shell, its commands and the interpreter load from. The running interpreter's prefixes are added
#: to these where the policy is built.
_SYSTEM_READ_ROOTS: tuple[str, ...] = (
    "/bin",
    "/sbin",
    "/usr",
    "/System",
    "/private/etc",
    "/private/var/db/dyld",
    "/private/var/db/timezone",
    "/private/var/select",
)

# Under a policy with read roots: the links at the top of the disk, each read as a link only, and
# the devices a process reads that the base does not already allow.
_SYSTEM_READ_LITERALS = """(allow file-read*
  (literal "/etc")
  (literal "/tmp")
  (literal "/var")
  (literal "/dev/random")
  (literal "/dev/urandom")
  (literal "/dev/zero")
  (literal "/dev/dtracehelper")
  (regex #"^/dev/fd/"))
"""

_WRITE_CHILD = """import errno, os, stat, sys
path = sys.argv[1]
kinds = {"b": "block device", "c": "character device", "d": "directory",
         "l": "symbolic link", "p": "FIFO", "s": "socket"}

def require_regular(mode):
    if not stat.S_ISREG(mode):
        kind = kinds.get(stat.filemode(mode)[0], "special file")
        raise OSError(errno.EINVAL, f"Expected a regular file, got {kind}", path)

try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as exc:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            raise exc from None
        require_regular(mode)
        raise
    with os.fdopen(fd, "wb") as f:
        require_regular(os.fstat(fd).st_mode)
        os.ftruncate(fd, 0)
        f.write(sys.stdin.buffer.read())
except OSError as e:
    sys.stderr.write(f"{e.strerror}: {e.filename}")
    sys.exit(1)
"""


def seatbelt_available() -> bool:
    """True on macOS with an executable ``/usr/bin/sandbox-exec``."""
    return sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


def _uv_cache_dir(*, _command_env_names: frozenset[str] | None = None) -> Path:
    """The cache uv itself reports, which follows its environment and configuration files."""
    uv = shutil.which("uv")
    if uv:
        try:
            out = subprocess.run(  # noqa: S603
                [uv, "cache", "dir"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=command_environment(
                    declared_names=_command_env_names, command_env_allowlist=()
                ),
            )
        except (OSError, subprocess.SubprocessError):
            out = None
        if out is not None and out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    configured = os.environ.get("UV_CACHE_DIR")
    if configured:
        return Path(configured)
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv"


def _system_read_roots() -> tuple[str, ...]:
    """``_SYSTEM_READ_ROOTS`` and the running interpreter's prefixes, resolved, each once."""
    named = (
        *_SYSTEM_READ_ROOTS,
        sys.prefix,
        sys.exec_prefix,
        sys.base_prefix,
        sys.base_exec_prefix,
    )
    return tuple(dict.fromkeys(os.path.realpath(path) for path in named if path))


def _within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or lies under it. Where both exist this is judged by file
    identity, so a spelling in another case on a case-insensitive volume counts too."""
    if path == root or root in path.parents:
        return True
    try:
        root_stat = root.stat()
    except OSError:
        return False
    for candidate in (path, *path.parents):
        try:
            if os.path.samestat(candidate.stat(), root_stat):
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class SeatbeltPolicy:
    """The resolved writable roots, the read-only paths inside them, the network switch, the
    paths the confined process may not read, the environment entries its commands run with, and
    the read roots: ``None`` leaves reads unrestricted, and a tuple, even an empty one, allows
    reads only under the writable roots and these roots, beside the links and devices of
    ``_SYSTEM_READ_LITERALS``. ``for_workspace`` adds the system directories to the read roots."""

    writable_roots: tuple[str, ...]
    read_only: tuple[str, ...] = ()
    network: bool = False
    pty: bool = False
    deny_read: tuple[str, ...] = ()
    child_env: tuple[tuple[str, str], ...] = ()
    read_roots: tuple[str, ...] | None = None

    @classmethod
    def for_workspace(
        cls,
        workspace_root: str | Path,
        *,
        network: bool = False,
        extra_roots: Sequence[str | Path] = (),
        pty: bool = False,
        deny_read: Sequence[str | Path] = (),
        per_leg_uv_cache: bool = False,
        read_roots: Sequence[str | Path] | None = None,
        _command_env_names: frozenset[str] | None = None,
    ) -> SeatbeltPolicy:
        """Writes to the workspace, the uv cache, the temporary directory and ``extra_roots``; the
        workspace's ``.git`` stays read-only, since a hook written there would run outside the
        sandbox. ``pty`` lets the confined process allocate pseudo-terminals of its own, which a
        CLI's shell tool needs to run a command; terminals it did not allocate stay closed to
        it. While the child sandbox is enabled, a workspace that is the home directory or an
        ancestor of it is refused. ``deny_read`` names files and directories (``~/`` or
        absolute) the confined process may neither read nor write, each denied as written, where
        it sits once its parent directory is resolved, at every link it passes through and at
        the path it resolves to; an entry that resolves under ``/dev`` is left out, since a link
        to ``/dev/null`` holds nothing and denying it would break every command that reads
        ``/dev/null``. An entry that is a writable root or holds one is refused, since the
        process could not use its own tree; so is an entry inside a writable root, the temporary
        directory excepted unless it is the workspace, since a command could move a directory
        above it and read it there, and the editor and the other in-process tools, which this
        policy does not confine, could read it. ``per_leg_uv_cache`` leaves the shared uv cache
        out of the writable roots, where a confined process could plant an entry a later install
        outside the sandbox would use; the policy instead makes a new, empty uv cache under the
        temporary directory, removed when the process exits, and hands it to its commands as
        ``UV_CACHE_DIR``. A confined ``uv run`` then works in a workspace whose environment
        already exists, and installs nothing it would have had to download or take from the
        shared cache. ``read_roots``, when given (an empty sequence included), denies reads by
        default: the process may read under the writable roots, these roots, the system
        directories and the running interpreter's prefixes, and may read the metadata of the
        directories above them so their paths resolve. A read root that is the home directory
        or holds it is refused, since every read would then pass."""
        from lionhub.agent.child_sandbox import _refuse_a_home_workspace, _written_and_resolved

        _refuse_a_home_workspace(workspace_root)
        workspace = Path(workspace_root).expanduser().resolve()
        child_env: tuple[tuple[str, str], ...] = ()
        if per_leg_uv_cache:
            leg_cache = tempfile.mkdtemp(prefix="lionagi-leg-uv-")
            atexit.register(shutil.rmtree, leg_cache, True)
            child_env = (("UV_CACHE_DIR", str(Path(leg_cache).resolve())),)
            uv_roots: tuple[Path, ...] = ()
        else:
            uv_roots = (_uv_cache_dir(_command_env_names=_command_env_names),)
        roots: list[str] = []
        for root in (
            workspace,
            *uv_roots,
            Path(tempfile.gettempdir()),
            *extra_roots,
        ):
            resolved = str(Path(root).expanduser().resolve())
            if resolved not in roots:
                roots.append(resolved)
        # The temporary directory, unless it is the workspace, is exempt from the inside-a-root
        # refusal below: it is shared scratch, and test harnesses keep a home there. A
        # credential location the user keeps under it stays unreadable to commands but could
        # be moved and then read.
        temp = str(Path(tempfile.gettempdir()).expanduser().resolve())
        unreadable: list[str] = []
        for path in deny_read:
            written = str(Path(path).expanduser())
            # Where the entry itself sits once its parent directory is resolved: a link under a
            # linked directory is judged by Seatbelt at that path, not at the one written.
            placed = os.path.join(
                os.path.realpath(os.path.dirname(written)), os.path.basename(written)
            )
            for entry in dict.fromkeys([placed, *_written_and_resolved(written)]):
                if entry == "/dev" or entry.startswith("/dev/") or entry in unreadable:
                    continue
                for root in roots:
                    if _within(Path(root), Path(entry)):
                        raise ConfigurationError(
                            f"the writable root {root!r} is inside {entry!r}, which the "
                            "sandbox denies reading; start the agent from a project directory"
                        )
                    if (root == str(workspace) or root != temp) and _within(
                        Path(entry), Path(root)
                    ):
                        raise ConfigurationError(
                            f"{entry!r}, which the sandbox denies reading, is inside the "
                            f"writable root {root!r}; start the agent from a directory "
                            "that holds no credentials"
                        )
                unreadable.append(entry)
        readable: tuple[str, ...] | None = None
        if read_roots is not None:
            home = Path.home().expanduser().resolve()
            named: list[str] = []
            for root in (*read_roots, *_system_read_roots()):
                resolved = str(Path(root).expanduser().resolve())
                if _within(home, Path(resolved)):
                    raise ConfigurationError(
                        f"the read root {resolved!r} is the home directory or holds it, so "
                        "every read would pass; name the directories the agent reads"
                    )
                if resolved not in named and resolved not in roots:
                    named.append(resolved)
            readable = tuple(named)
        return cls(
            tuple(roots),
            (str(workspace / ".git"),),
            network,
            pty,
            tuple(unreadable),
            child_env,
            readable,
        )

    def _read_parents(self) -> tuple[str, ...]:
        """Every directory above a writable or a read root, each once. Under read roots their
        metadata is readable, so a path under a root resolves; their contents are not."""
        parents: dict[str, None] = {}
        for root in (*self.writable_roots, *(self.read_roots or ())):
            for parent in Path(root).parents:
                parents[str(parent)] = None
        return tuple(parents)

    def _read_rules(self) -> str:
        """The read rules that replace the base's ``(allow file-read*)`` under read roots."""
        grants = [
            f'  (subpath (param "WRITABLE_ROOT_{i}"))' for i in range(len(self.writable_roots))
        ]
        grants += [
            f'  (subpath (param "READ_ROOT_{i}"))' for i in range(len(self.read_roots or ()))
        ]
        parts = ["(allow file-read*\n" + "\n".join(grants) + ")\n"] if grants else []
        parts.append(_SYSTEM_READ_LITERALS)
        parents = [
            f'  (literal (param "READ_PARENT_{i}"))' for i in range(len(self._read_parents()))
        ]
        if parents:
            parts.append("(allow file-read-metadata\n" + "\n".join(parents) + ")\n")
        return "".join(parts)

    def profile(self) -> str:
        writes = "\n".join(
            f'  (subpath (param "WRITABLE_ROOT_{i}"))' for i in range(len(self.writable_roots))
        )
        base = (
            _BASE_POLICY
            if self.read_roots is None
            else _BASE_POLICY.replace("(allow file-read*)\n", self._read_rules(), 1)
        )
        parts = [base, f"(allow file-write*\n{writes})\n"]
        parts += [
            f'(deny file-write* (subpath (param "READ_ONLY_{i}")))\n'
            for i in range(len(self.read_only))
        ]
        # After the base's `(allow file-read*)` and the writable roots, so these win.
        parts += [
            f'(deny file-read* file-write* (subpath (param "DENY_READ_{i}")))\n'
            for i in range(len(self.deny_read))
        ]
        if self.network:
            parts.append(_NETWORK_POLICY)
            # A connect() to a unix socket is judged by the network rules, not by file-read*, so
            # without these a listener inside an unreadable path (a daemon's socket in its store
            # directory) still answers, and the daemon reads the store for the command.
            parts += [
                f'(deny network-outbound (subpath (param "DENY_READ_{i}")))\n'
                for i in range(len(self.deny_read))
            ]
        if self.pty:
            parts.append(_PTY_POLICY)
        # A hard link gives a file outside the roots, or in a read-only or unreadable path, a
        # second name inside a root, where it can then be written. On a volume mounted with
        # ownership ignored (`noowners`, an external disk's default) Seatbelt does not judge the
        # source of link(2) by the write rules above, though it does judge the file-link
        # operation, so the link is refused by that operation here. These rules come last, so no
        # later rule can allow what they deny. Whether a volume honours them is measured per
        # volume by `lionhub.tools.seatbelt_link_probe`.
        if self.writable_roots:
            inside = " ".join(
                f'(subpath (param "WRITABLE_ROOT_{i}"))' for i in range(len(self.writable_roots))
            )
            parts.append(f"(deny file-link (require-not (require-any {inside})))\n")
        else:
            parts.append("(deny file-link)\n")
        parts += [
            f'(deny file-link (subpath (param "{kind}_{i}")))\n'
            for kind, paths in (("READ_ONLY", self.read_only), ("DENY_READ", self.deny_read))
            for i in range(len(paths))
        ]
        return "".join(parts)

    def wrap(self, argv: Sequence[str]) -> list[str]:
        """``argv`` run under this policy; paths travel as parameters, never inside the policy text."""
        params: list[str] = []
        for i, root in enumerate(self.writable_roots):
            params += ["-D", f"WRITABLE_ROOT_{i}={root}"]
        for i, path in enumerate(self.read_only):
            params += ["-D", f"READ_ONLY_{i}={path}"]
        for i, path in enumerate(self.deny_read):
            params += ["-D", f"DENY_READ_{i}={path}"]
        if self.read_roots is not None:
            for i, path in enumerate(self.read_roots):
                params += ["-D", f"READ_ROOT_{i}={path}"]
            for i, path in enumerate(self._read_parents()):
                params += ["-D", f"READ_PARENT_{i}={path}"]
        return [SANDBOX_EXEC, *params, "-p", self.profile(), "--", *argv]

    def write_text(
        self,
        path: Path,
        content: str,
        timeout: float = 30.0,
        *,
        _command_env: Mapping[str, str] | None = None,
    ) -> str | None:
        """Write ``content`` to ``path`` from a confined child; the refusal text, or None."""
        if not sys.executable:
            return "no Python interpreter to run the confined write"
        argv = self.wrap([sys.executable, "-I", "-S", "-c", _WRITE_CHILD, str(path)])
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                input=content.encode("utf-8"),
                capture_output=True,
                timeout=timeout,
                check=False,
                env=(
                    command_environment(command_env_allowlist=())
                    if _command_env is None
                    else _command_env
                ),
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"confined write failed: {e}"
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()
            return detail or f"confined write exited {proc.returncode}"
        return None
