# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Check the server boot actor with an MCP initialize request.

The pytest case uses a stand-in. Run this file directly with an installed
kkernel path to check the real server in isolated temporary state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from lionhub.agent.khive_actor import attribute_khive_servers, child_khive_actor

_PROJECT_ACTOR = "agent:fixture-project"
_INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "lionagi-actor-check", "version": "1"},
    },
}
_BOOT_ACTOR = re.compile(r'\bactor: "([^"]+)" \(resolved; attributed\)')


async def _initialize(server: dict, project: Path, home: Path) -> str:
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "TMPDIR": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "KHIVE_VOLUME_LOCK_DIR": str(home / "volume-locks"),
        "KHIVE_NO_DAEMON": "1",
        **server["env"],
    }
    with (home / "boot.log").open("w+b") as log:
        child = await asyncio.create_subprocess_exec(
            server["command"],
            *server["args"],
            cwd=project,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log,
        )
        assert child.stdin is not None and child.stdout is not None

        async def exchange() -> bytes:
            child.stdin.write(json.dumps(_INIT_REQUEST).encode("utf-8") + b"\n")
            await child.stdin.drain()
            return await child.stdout.readline()

        failure = None
        reply = b""
        try:
            reply = await asyncio.wait_for(exchange(), timeout=15)
        except (asyncio.TimeoutError, OSError) as exc:
            failure = exc
        finally:
            if child.returncode is None:
                with suppress(ProcessLookupError):
                    child.kill()
            await asyncio.wait_for(child.communicate(), timeout=5)
        log.seek(0)
        boot_log = log.read(64 * 1024).decode("utf-8", errors="replace")

    if failure is not None or not reply:
        raise AssertionError(f"kkernel did not answer MCP initialize:\n{boot_log}") from failure
    response = json.loads(reply)
    assert response.get("jsonrpc") == "2.0" and response.get("id") == 1, response
    assert isinstance(response.get("result"), dict), response
    actors = _BOOT_ACTOR.findall(boot_log)
    assert len(actors) == 1, boot_log
    return actors[0]


def _boot_actor(
    kkernel: str, root: Path, profile: str, *, with_flag: bool, stand_in: str | None
) -> str:
    home = root / "home"
    home.mkdir(parents=True)
    project = root / "project"
    config = project / ".khive"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(f'[actor]\nid = "{_PROJECT_ACTOR}"\n', encoding="utf-8")
    actor = child_khive_actor(profile)
    server = {
        "command": kkernel,
        "args": ["mcp", "--db", ":memory:", "--no-embed", "--pack", "kg"],
        "env": {"KHIVE_ACTOR": actor},
    }
    if with_flag:
        servers = attribute_khive_servers({"khive": server}, actor)
        assert servers is not None
        server = servers["khive"]
    if stand_in is not None:
        # Match the kkernel command during attribution, then run the test fixture.
        server = {**server, "command": stand_in}
    return asyncio.run(_initialize(server, project, home))


def _run_cases(kkernel: str, root: Path, *, stand_in: str | None = None) -> dict[str, str]:
    cases = (
        ("worker", "fixture-worker", True, "agent:fixture-worker"),
        ("reviewer", "fixture-reviewer", True, "agent:fixture-reviewer"),
        ("env-only", "fixture-worker", False, _PROJECT_ACTOR),
    )
    observed = {}
    for case, profile, with_flag, expected in cases:
        actor = _boot_actor(kkernel, root / case, profile, with_flag=with_flag, stand_in=stand_in)
        assert actor == expected, f"{case}: expected {expected}, got {actor}"
        observed[case] = actor
    return observed


def test_khive_actor_boot_harness_with_stand_in(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIVE_EMAIL_EXTERNAL_SENDERS", "synthetic-parent-setting")
    stand_in = tmp_path / "kkernel-stand-in"
    stand_in.write_text(
        f"#!{sys.executable}\n"
        """import json
import os
import re
import sys
from pathlib import Path

assert sys.argv[1] == "mcp"
assert sys.argv[sys.argv.index("--db") + 1] == ":memory:"
assert "--no-embed" in sys.argv
assert os.environ["KHIVE_NO_DAEMON"] == "1"
assert "KHIVE_EMAIL_EXTERNAL_SENDERS" not in os.environ
assert Path(os.environ["HOME"]).resolve() == Path.cwd().parent / "home"
assert os.environ["KHIVE_ACTOR"] in {"agent:fixture-worker", "agent:fixture-reviewer"}
config = Path(".khive/config.toml").read_text()
fallback = re.search(r'id = "([^"]+)"', config)[1]
actor = sys.argv[sys.argv.index("--actor") + 1] if "--actor" in sys.argv else fallback
message = json.loads(sys.stdin.readline())
assert message["method"] == "initialize"
print(f'actor: "{actor}" (resolved; attributed)', file=sys.stderr, flush=True)
print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
""",
        encoding="utf-8",
    )
    stand_in.chmod(0o700)
    assert _run_cases("kkernel", tmp_path, stand_in=str(stand_in)) == {
        "worker": "agent:fixture-worker",
        "reviewer": "agent:fixture-reviewer",
        "env-only": _PROJECT_ACTOR,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kkernel", nargs="?", help="Path to the installed kkernel")
    args = parser.parse_args()
    kkernel = shutil.which(args.kkernel or "kkernel")
    if kkernel is None:
        if args.kkernel is not None:
            parser.error(f"kkernel is missing or not executable: {args.kkernel}")
        print("SKIP: kkernel is not installed on PATH")
        return
    kkernel = str(Path(kkernel).absolute())
    with TemporaryDirectory(prefix="lionagi-khive-actor-") as root:
        for case, actor in _run_cases(kkernel, Path(root)).items():
            print(f"{case}: {actor}")


if __name__ == "__main__":
    main()
