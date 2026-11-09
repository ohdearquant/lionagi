# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Value-category path refusals do not retain the working directory."""

from pathlib import Path

import pytest

from lionagi._errors import CLIInputRefusedError, ContainmentRefusedError
from lionhub.providers.openai import codex


@pytest.mark.parametrize("method", ["construct", "model_validate"])
@pytest.mark.parametrize("real_path", [True, False])
def test_value_path_refusal_does_not_expose_input(tmp_path, monkeypatch, method, real_path):
    marker = "private fixture working directory"
    path = tmp_path / (marker + "\ud800")
    if not real_path:

        def refuse(path, field):
            raise ValueError(str(path))

        monkeypatch.setattr(codex, "_resolve_cli_path", refuse)
    error = None
    try:
        fields = {"prompt": "hello", "repo": path, "contain_account_integrations": True}
        if method == "construct":
            codex.CodexCodeRequest(**fields)
        else:
            codex.CodexCodeRequest.model_validate(fields)
    except Exception as caught:
        error = caught
    if not isinstance(error, CLIInputRefusedError):
        pytest.fail("working-directory value refusal lost its safe type", pytrace=False)
    expected = (
        "contain_account_integrations cannot use the working directory: it cannot be resolved."
        " To run without containment, launch through an agent profile that sets"
        " contain_account_integrations: false."
    )
    assert str(error) == expected
    assert isinstance(error.__cause__, ContainmentRefusedError)
    assert str(error.__cause__) == expected
    assert error.__context__ is None
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if marker in str(current) or marker in repr(current) or marker in repr(current.args):
            pytest.fail("working-directory refusal retains the path", pytrace=False)
        pending.extend(x for x in (current.__cause__, current.__context__) if x is not None)


def test_value_path_refusal_without_containment_keeps_validation_behavior(tmp_path, monkeypatch):
    def refuse(path, field):
        raise ValueError("invalid path")

    monkeypatch.setattr(codex, "_resolve_cli_path", refuse)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        codex.CodexCodeRequest(
            prompt="hello", repo=Path(tmp_path), contain_account_integrations=False
        )
