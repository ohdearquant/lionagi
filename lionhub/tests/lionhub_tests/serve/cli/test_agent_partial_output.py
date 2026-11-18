# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A turn that a provider error ends after it delivered text: `li agent` writes the text,
then one line naming the error, and exits with the partial exit code; run.json keeps
``status: failed`` and adds ``partial: true``. With no text before the error the run
fails as before."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lionagi._provider_errors import ProviderAdapterError
from lionhub.orchestration.job import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_PARTIAL_OUTPUT,
    EXIT_CODE_SUBAGENT_REFUSED,
)

from .test_agent_resume_model_override import _wire_agent_stubs

_ERROR = "status=ERROR: The stream was interrupted. Please continue the task."


def _ended_after(text: str | None) -> ProviderAdapterError:
    exc = ProviderAdapterError(_ERROR)
    exc.partial_text = text
    return exc


def _main(argv: list[str], exc: BaseException) -> int:
    import lionhub.serve.cli.agent as agent_mod
    from lionhub.serve.cli.main import main

    async def ended(model_str, prompt, **kwargs):
        raise exc

    with patch.object(agent_mod, "_run_agent", ended):
        return main(argv)


def test_the_partial_code_is_outside_the_status_map_and_distinct():
    taken = {*EXIT_CODE_BY_STATUS.values(), EXIT_CODE_SUBAGENT_REFUSED, 2, 3, 78}
    assert EXIT_CODE_PARTIAL_OUTPUT not in taken


def test_text_delivered_before_the_error_is_written_then_marked_partial(capsys):
    code = _main(["agent", "gemini", "do it"], _ended_after("first half of the answer"))
    out, err = capsys.readouterr()
    assert code == EXIT_CODE_PARTIAL_OUTPUT
    assert "first half of the answer" in out
    assert "output is partial" in err
    assert "ProviderAdapterError" in err
    assert "The stream was interrupted" in err


def test_a_verbose_run_does_not_write_the_streamed_text_twice(capsys):
    code = _main(["agent", "gemini", "-v", "do it"], _ended_after("already streamed"))
    out, err = capsys.readouterr()
    assert code == EXIT_CODE_PARTIAL_OUTPUT
    assert "already streamed" not in out
    assert "output is partial" in err


def test_an_error_before_any_text_keeps_the_existing_exit(capsys):
    with pytest.raises(ProviderAdapterError):
        _main(["agent", "gemini", "do it"], _ended_after(None))
    _out, err = capsys.readouterr()
    assert "output is partial" not in err


@pytest.mark.asyncio
@pytest.mark.parametrize(("partial", "expected"), [("delivered text", True), (None, None)])
async def test_run_json_records_partial_beside_failed(monkeypatch, tmp_path, partial, expected):
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch

    _wire_agent_stubs(monkeypatch, tmp_path)
    manifests: list[dict] = []
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
            write_manifest=lambda manifest: manifests.append(dict(manifest)),
        ),
    )

    async def ended(self, instruction=None, **kw):
        raise _ended_after(partial)

    monkeypatch.setattr(Branch, "operate", ended)

    with pytest.raises(ProviderAdapterError):
        await agent_mod._run_agent("gemini-cli/gemini-4-argon", "do it", no_mcp_config=True)

    final = manifests[-1]
    assert final["status"] == "failed"
    assert final.get("partial") is expected
    assert "ProviderAdapterError" in final["error"]
