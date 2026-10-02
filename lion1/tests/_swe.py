"""What the bench tests share: `bench/swe` importable by module name, as the scripts import each
other, and the fake boxes the codex arm and the runner are driven through."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "swe"))

import codex_cli  # noqa: E402
import run_naked  # noqa: E402
import run_set  # noqa: E402
import run_swe  # noqa: E402
import sandbox  # noqa: E402

__all__ = (
    "BASE",
    "DIFF",
    "CodexBox",
    "GatewayTimeoutBox",
    "Started",
    "codex_cli",
    "run_naked",
    "run_set",
    "run_swe",
    "sandbox",
    "stream_text",
)

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
BASE = "b" * 40  # the commit the boxes were handed


class Started:
    """A box the runner can start and stop, and nothing else."""

    async def start(self):
        return self

    async def stop(self):
        pass


def stream_text() -> str:
    ev = lambda **e: json.dumps(e)  # noqa: E731
    return "\n".join(
        [
            ev(type="thread.started", thread_id="t"),
            ev(type="turn.started"),
            ev(type="item.completed", item={"type": "command_execution", "command": "ls", "exit_code": 0}),
            ev(
                type="item.completed",
                item={"type": "command_execution", "command": "pytest -q", "exit_code": 0},
            ),
            ev(type="item.completed", item={"type": "agent_message", "text": "Fixed."}),
            ev(
                type="turn.completed",
                usage={"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200},
            ),
        ]
    )


class CodexBox(Started):
    """The codex arm's box: the install answers, the baseline is `BASE`, and `codex exec` leaves
    `stream_text()` behind and exits `rc`."""

    def __init__(self, rc: int = 0):
        self.rc = rc
        self.cmds: list[str] = []
        self.envs: list[dict | None] = []
        self.files: dict[str, str] = {}

    async def run(self, cmd: str, *, timeout: float = 120.0, env: dict | None = None) -> tuple[int, str]:
        self.cmds.append(cmd)
        self.envs.append(env)
        if "curl -fsSL" in cmd:
            return 0, "codex-cli 0.153.4\n"
        if cmd == "git rev-parse HEAD":
            return 0, BASE + "\n"
        if f"{codex_cli.BIN} exec" in cmd:
            self.files[codex_cli.STREAM] = stream_text()
            self.files[codex_cli.STDERR] = ""
            self.files[codex_cli.LAST] = "Fixed."
            return self.rc, ""
        return 0, ""

    async def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write(self, path: str, text: str) -> None:
        self.files[path] = text

    async def diff(self, base: str = "HEAD") -> str:
        return DIFF


class GatewayTimeoutBox(CodexBox):
    """codex ran and wrote its stream in the box; the host's exec call came back as a 504. `stop_rc`
    is what the stop probe answers: 0 when no codex process is left, 1 when one survived the kills,
    None when the probe itself failed."""

    def __init__(self, stop_rc: int | None):
        super().__init__()
        self.stop_rc, self.diffs = stop_rc, 0

    async def run(self, cmd, *, timeout=120.0, env=None):
        if f"{codex_cli.BIN} exec" in cmd:
            self.cmds.append(cmd)
            usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200}
            self.files[codex_cli.STREAM] = json.dumps({"type": "turn.completed", "usage": usage})
            self.files[codex_cli.STDERR] = ""
            self.files[codex_cli.LAST] = ""
            raise RuntimeError("DaytonaTimeoutError: Failed to execute command: 504 Gateway Time-out")
        if "/proc/" in cmd:  # the stop probe
            self.cmds.append(cmd)
            if self.stop_rc is None:
                raise RuntimeError("the box is gone")
            return self.stop_rc, ""
        return await super().run(cmd, timeout=timeout, env=env)

    async def diff(self, base: str = "HEAD"):
        self.diffs += 1
        return DIFF
