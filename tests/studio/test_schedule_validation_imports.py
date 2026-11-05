# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The schedule declaration uses shared validators without importing the router."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize(
    "name",
    [
        "_svc_validate_budget_usd",
        "_svc_validate_budget_tokens",
        "_svc_validate_github_repo",
        "_svc_validate_github_filter",
        "_validate_flow_yaml_spec",
        "_svc_validate_command_args",
        "_GITHUB_FILTER_ALLOWED_KEYS",
        "_GITHUB_FILTER_ALLOWED_EVENTS",
    ],
)
def test_router_reexports_the_same_validation_objects(name):
    from lionagi.studio.services import _schedule_validation, schedules

    assert getattr(schedules, name) is getattr(_schedule_validation, name)


def test_declaration_validators_run_without_importing_router(tmp_path):
    """Record refused imports even if a caller catches their exceptions."""
    program = textwrap.dedent(
        r"""
        import importlib
        import importlib.abc
        import os
        import sys
        from pathlib import Path

        router = "lionagi.studio.services.schedules"
        attempts = []

        class BlockRouter(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == router or fullname.startswith(router + "."):
                    attempts.append(fullname)
                    raise ModuleNotFoundError("blocked router", name=fullname)
                return None

        sys.meta_path.insert(0, BlockRouter())
        assert router not in sys.modules
        # The exact same detector must record and refuse a swallowed import.
        try:
            importlib.import_module(router)
        except ModuleNotFoundError as exc:
            assert exc.name == router
        else:
            raise AssertionError("router control unexpectedly imported")
        assert attempts == [router]
        attempts.clear()

        from lionagi.studio.services import _schedule_validation as validators
        from lionagi.studio.services.schedule_declaration import (
            Budget, CommandTarget, FlowTarget, GithubTriggerSpec, Trigger,
            _resolve_command_target, _resolve_flow_target, _resolve_trigger,
        )

        names = (
            "_svc_validate_budget_usd", "_svc_validate_budget_tokens",
            "_svc_validate_github_repo", "_svc_validate_github_filter",
            "_validate_flow_yaml_spec", "_svc_validate_command_args",
        )
        calls = {name: 0 for name in names}
        for name in names:
            original = getattr(validators, name)
            def counted(*args, _name=name, _original=original, **kwargs):
                calls[_name] += 1
                return _original(*args, **kwargs)
            setattr(validators, name, counted)

        def refused(call):
            try:
                call()
            except ValueError:
                return
            raise AssertionError("invalid declaration was accepted")

        budget = Budget(usd=1.5, tokens=100)
        assert budget.usd == 1.5 and budget.tokens == 100
        refused(lambda: Budget(usd=-1, tokens=100))
        refused(lambda: Budget(usd=1, tokens=True))

        def trigger(repo, github_filter):
            return _resolve_trigger(Trigger(github=GithubTriggerSpec(repo=repo, filter=github_filter)))

        assert trigger("acme/widgets", {"event": "pr_merged"})["repo"] == "acme/widgets"
        refused(lambda: trigger("../../etc/passwd", {}))
        refused(lambda: trigger("acme/widgets", {"event": "bogus"}))

        root = Path(sys.argv[1])
        (root / "good.yaml").write_text("workers: 2\n")
        (root / "bad.yaml").write_text("- not-a-flow\n")
        good_flow = FlowTarget(kind="flow", file="good.yaml")
        assert _resolve_flow_target(good_flow, root)["content"] == "workers: 2\n"
        refused(lambda: _resolve_flow_target(FlowTarget(kind="flow", file="bad.yaml"), root))

        os.environ["LIONAGI_SCHEDULER_COMMAND_ALLOWLIST"] = "refresh-index"
        command = CommandTarget(kind="command", executable="refresh-index", args=["--repo", "x/y"])
        assert _resolve_command_target(command)["args"] == ["--repo", "x/y"]
        refused(lambda: _resolve_command_target(
            CommandTarget(kind="command", executable="unlisted-command", args=[])
        ))
        refused(lambda: CommandTarget(kind="command", executable="refresh-index", args="bad"))

        assert all(count > 0 for count in calls.values()), calls
        assert attempts == [], attempts
        assert router not in sys.modules
        """
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
