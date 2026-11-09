from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]

_BLOCKER = """
import importlib.abc
import json
import sys

BLOCKED = (
    "fastapi",
    "starlette",
    "lionagi.studio.app",
    "lionagi.studio.operator",
    "lionagi.studio.scheduler",
    "lionagi.studio.services.projects",
)
refused = []

class BlockedModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in BLOCKED):
            refused.append(fullname)
            raise ImportError("blocked import: " + fullname)
        return None

sys.meta_path.insert(0, BlockedModules())
"""


def _run_child(body: str) -> dict:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "LIONAGI_SCHEDULER_COMMAND_ALLOWLIST": "refresh-index",
    }
    result = subprocess.run(
        [sys.executable, "-c", _BLOCKER + "\n" + textwrap.dedent(body)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"child failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])
