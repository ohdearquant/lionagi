# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Provider working-directory errors must not retain path values."""

import pytest

from lionagi.service.imodel import iModel
from lionhub.providers.anthropic.claude_code import ClaudeCodeRequest
from lionhub.providers.google.gemini_code import GeminiCodeRequest
from lionhub.providers.openai.codex import CodexCodeRequest
from lionhub.providers.pi.cli import PiCodeRequest

_CASES = [
    ("claude_code", ClaudeCodeRequest, {}),
    ("codex", CodexCodeRequest, {}),
    ("gemini_code", GeminiCodeRequest, {"include_directories": ["."]}),
    ("pi", PiCodeRequest, {}),
]


def _chain_contains(error, marker):
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
            return True
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,request_type,extra", _CASES, ids=["claude", "codex", "gemini", "pi"]
)
@pytest.mark.parametrize("mode", ["constructor", "model_validate", "invoke", "stream"])
async def test_unresolvable_repo_does_not_expose_its_path(
    tmp_path, capsys, caplog, provider, request_type, extra, mode
):
    marker = "sensitive fixture content"
    repo = tmp_path / marker
    repo.symlink_to(repo.name, target_is_directory=True)
    request = {"prompt": "hello", "repo": repo, **extra}
    error = None
    try:
        if mode == "constructor":
            request_type(**request)
        elif mode == "model_validate":
            request_type.model_validate(request)
        else:
            model = iModel(provider=provider, model="test-model", api_key="test-key")
            if mode == "invoke":
                await model.invoke(**request)
            else:
                async for _ in model.stream(**request):
                    pass
    except Exception as caught:
        error = caught
    if error is None:
        pytest.fail("unresolvable working directory was accepted", pytrace=False)
    if _chain_contains(error, marker):
        pytest.fail("working-directory error retains caller input", pytrace=False)
    output = capsys.readouterr()
    if marker in output.out or marker in output.err or marker in caplog.text:
        pytest.fail("working-directory output retains caller input", pytrace=False)
