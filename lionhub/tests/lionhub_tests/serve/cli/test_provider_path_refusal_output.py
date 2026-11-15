# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The agent CLI does not print provider input when its request is refused."""

from types import SimpleNamespace

import pytest

from lionagi.service.imodel import iModel
from lionhub.serve.cli import agent as agent_cli


@pytest.mark.parametrize(
    "provider,field,batch",
    [
        ("claude_code", "system_prompt_file", False),
        ("codex", "output_schema", False),
        ("gemini_code", "include_directories", True),
        ("pi", "extension", True),
    ],
    ids=["claude", "codex", "gemini", "pi"],
)
def test_agent_cli_reports_provider_refusal_without_input(
    tmp_path, monkeypatch, capsys, caplog, provider, field, batch
):
    marker = "sensitive fixture content"
    value = "../" + marker
    model = iModel(provider=provider, model="test-model", api_key="test-key")

    async def fail_request(*_args, **_kwargs):
        await model.invoke(prompt="hello", repo=tmp_path, **{field: [value] if batch else value})

    monkeypatch.setattr(agent_cli, "_resolve_model_and_prompt", lambda _args: ("model", "hello"))
    monkeypatch.setattr(agent_cli, "_run_agent", fail_request)
    messages = []
    monkeypatch.setattr(agent_cli, "log_error", messages.append)
    args = SimpleNamespace(
        form=None,
        image=None,
        agent=None,
        resume=None,
        continue_last=False,
        yolo=False,
        verbose=False,
        theme=None,
        effort=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
    )
    error = None
    try:
        agent_cli.run_agent(args)
    except Exception as caught:
        error = caught
    if not isinstance(error, ValueError):
        pytest.fail("CLI did not report provider validation refusal", pytrace=False)
    output = capsys.readouterr()
    if (
        marker in output.out
        or marker in output.err
        or marker in caplog.text
        or any(marker in message for message in messages)
    ):
        pytest.fail("CLI output retained caller input", pytrace=False)
    assert any("validation error" in message for message in messages)
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        values = [str(current), repr(current)]
        if callable(getattr(current, "errors", None)):
            values.append(repr(current.errors()))
        if callable(getattr(current, "json", None)):
            values.append(current.json())
        if any(marker in value for value in values):
            pytest.fail("CLI error chain retained caller input", pytrace=False)
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
