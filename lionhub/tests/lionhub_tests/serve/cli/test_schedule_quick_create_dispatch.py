# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li schedule create` routes a quick-create kind token to typed quick-create and every
other first argument to the legacy create command."""

from __future__ import annotations


def test_legacy_create_dispatch_unaffected_by_quick_create_kinds(monkeypatch):
    """`li schedule create NAME ...` (NAME not a reserved kind token) must
    still be routed to the legacy HTTP-backed _cmd_create, not quick-create."""
    import lionhub.serve.cli.schedule as schedule_cli
    from lionhub.serve.cli.main import main

    captured = {}

    def fake_api(path, method="GET", body=None):
        captured.update(path=path, method=method, body=body)
        return {"id": "legacy-id", "name": body.get("name") if body else None}

    monkeypatch.setattr(schedule_cli, "_api", fake_api)
    rc = main(["schedule", "create", "my-legacy-sched", "--cron", "0 * * * *", "--prompt", "ping"])
    assert rc == 0
    assert captured["method"] == "POST"
    assert captured["body"]["name"] == "my-legacy-sched"


def test_quick_create_kind_tokens_reserved_from_legacy_positional(monkeypatch):
    """A quick-create kind token as the first arg after 'create' is always
    routed to typed quick-create, even without further matching flags --
    this is the documented additive-compatibility tradeoff."""
    import lionhub.serve.cli.schedule as schedule_cli

    called = {}
    monkeypatch.setattr(
        schedule_cli,
        "run_schedule_quick_create",
        lambda kind, argv: called.update(kind=kind, argv=argv) or 0,
    )
    from lionhub.serve.cli.main import main

    rc = main(
        ["schedule", "create", "agent", "foo", "--profile", "x", "--prompt", "y", "--every", "1h"]
    )
    assert rc == 0
    assert called["kind"] == "agent"
    assert called["argv"] == ["foo", "--profile", "x", "--prompt", "y", "--every", "1h"]
