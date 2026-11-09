# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Check that schedule declarations and exports load without Studio runtime modules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def test_declaration_and_export_import_without_studio_runtime(tmp_path):
    result = _run_child(
        f"""
        import os
        from pathlib import Path
        import yaml

        import lionhub.orchestration.schedule.declaration as declaration
        import lionhub.orchestration.schedule.export as export

        root = Path({str(tmp_path)!r}) / "workspace"
        root.mkdir()
        (root / ".lionagi" / "agents").mkdir(parents=True)
        (root / ".lionagi" / "agents" / "reviewer.md").write_text(
            "---\\nmodel: anthropic/claude-sonnet-5\\n---\\nBody.\\n"
        )
        (root / "flow.yaml").write_text("workers: 2\\n")
        targets = [
            {{"kind": "agent", "profile": "reviewer", "prompt": "check things"}},
            {{"kind": "flow", "file": "flow.yaml"}},
            {{"kind": "playbook", "name": "health-audit"}},
            {{"kind": "command", "executable": "refresh-index", "args": ["incremental"]}},
        ]
        for index, target in enumerate(targets):
            manifest = {{
                "apiVersion": declaration.SPEC_VERSION,
                "kind": declaration.SPEC_KIND,
                "metadata": {{"name": "automation", "project": "demo"}},
                "schedules": {{"item-" + str(index): {{
                    "trigger": {{"every": "15m"}},
                    "target": target,
                    "execution": {{"cwd": str(root)}},
                }}}},
            }}
            document = declaration.parse_schedule_set(yaml.safe_dump(manifest, sort_keys=False))
            resolved = declaration.resolve_schedule_set(document, root)
            assert resolved["item-" + str(index)].resolved["target"]["kind"] == target["kind"]

        # The adoption comparison is exercised by the database-free comparison tests.
        canonical = declaration._adopt_comparable(
            "action_agent", {{"action_kind": "playbook"}}, "reviewer"
        )
        assert canonical is None

        row = {{
            "id": "a1",
            "name": "demo/nightly",
            "trigger_type": "cron",
            "cron_expr": "0 2 * * *",
            "effective_timezone": "America/New_York",
            "action_kind": "agent",
            "action_agent": "reviewer",
            "action_prompt": "check things",
            "action_cwd": str(root),
            "enabled": 1,
        }}
        docs, lines = export.convert_legacy_rows(
            [row], flows_dir=root / "flows", manifest_dir=root
        )
        assert [line.status for line in lines] == ["READY"]
        assert docs[0].schedules["nightly"].target.kind == "agent"

        blocked_loaded = sorted(
            name for name in sys.modules
            if any(name == item or name.startswith(item + ".") for item in BLOCKED)
        )
        print(json.dumps({{"refused": refused, "blocked_loaded": blocked_loaded,
                          "loaded": sorted(sys.modules)}}))
        """
    )
    assert result["refused"] == []
    assert result["blocked_loaded"] == []
    assert "lionhub.orchestration.schedule.declaration" in result["loaded"]
    assert "lionhub.orchestration.schedule.export" in result["loaded"]


def test_import_blocker_rejects_scheduler_runtime():
    result = _run_child(
        """
        try:
            import lionagi.studio.scheduler.engine
        except ImportError:
            pass
        else:
            raise AssertionError("blocked scheduler import unexpectedly succeeded")
        print(json.dumps({"refused": refused}))
        """
    )
    assert "lionagi.studio.scheduler" in result["refused"]
