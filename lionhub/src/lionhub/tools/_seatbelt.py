# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""macOS Seatbelt confinement for the editor and bash an API model is handed (ADR-0041 Amendment 2).

The policy takes the shape of the Codex CLI's macOS sandbox: deny by default, reads unrestricted,
writes only under the writable roots, network only when asked for. Seatbelt judges the resolved
path, so the roots are resolved before they are passed and a write through a symlink is judged
where the link points.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lionagi._command_env_metadata import command_environment

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Reads are unrestricted, so sysctl reads are too; what the policy confines is writes and network.
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

_NETWORK_POLICY = """(allow network-outbound)
(allow network-inbound)
(allow system-socket (socket-domain AF_UNIX))
(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd")
  (global-name "com.apple.TrustEvaluationAgent"))
(allow sysctl-read (sysctl-name-regex #"^net.routetable"))
"""

# Runs as `python -I -S -c`: no site, no user or script directory on sys.path. O_NOFOLLOW keeps the
# editor's refusal to write through a symlink in the child that does the write.
_WRITE_CHILD = """import os, sys
path = sys.argv[1]
try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o666)
    with os.fdopen(fd, "wb") as f:
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


@dataclass(frozen=True)
class SeatbeltPolicy:
    """The resolved writable roots, the read-only paths inside them, and the network switch."""

    writable_roots: tuple[str, ...]
    read_only: tuple[str, ...] = ()
    network: bool = False

    @classmethod
    def for_workspace(
        cls,
        workspace_root: str | Path,
        *,
        network: bool = False,
        _command_env_names: frozenset[str] | None = None,
    ) -> SeatbeltPolicy:
        """Writes to the workspace, the uv cache and the temporary directory; the workspace's
        ``.git`` stays read-only, since a hook written there would run outside the sandbox."""
        workspace = Path(workspace_root).expanduser().resolve()
        roots: list[str] = []
        for root in (
            workspace,
            _uv_cache_dir(_command_env_names=_command_env_names),
            Path(tempfile.gettempdir()),
        ):
            resolved = str(Path(root).expanduser().resolve())
            if resolved not in roots:
                roots.append(resolved)
        return cls(tuple(roots), (str(workspace / ".git"),), network)

    def profile(self) -> str:
        writes = "\n".join(
            f'  (subpath (param "WRITABLE_ROOT_{i}"))' for i in range(len(self.writable_roots))
        )
        parts = [_BASE_POLICY, f"(allow file-write*\n{writes})\n"]
        parts += [
            f'(deny file-write* (subpath (param "READ_ONLY_{i}")))\n'
            for i in range(len(self.read_only))
        ]
        if self.network:
            parts.append(_NETWORK_POLICY)
        return "".join(parts)

    def wrap(self, argv: Sequence[str]) -> list[str]:
        """``argv`` run under this policy; paths travel as parameters, never inside the policy text."""
        params: list[str] = []
        for i, root in enumerate(self.writable_roots):
            params += ["-D", f"WRITABLE_ROOT_{i}={root}"]
        for i, path in enumerate(self.read_only):
            params += ["-D", f"READ_ONLY_{i}={path}"]
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
