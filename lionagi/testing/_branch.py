# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``TestBranch`` and ``scripted_imodel`` — ergonomic builders for tests.

Idiomatic usage::

    from lionagi.testing import TestBranch

    branch = TestBranch.from_responses([
        {"type": "text", "content": "hi"},
    ])
    assert await branch.chat("hello") == "hi"

    # Inspect what the agent sent:
    assert TestBranch.scripted(branch).calls[0].last_user_message == "hello"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

from ._endpoint import ScriptedEndpoint
from ._script import ScriptModel
from ._types import RecordedCall

__all__ = ("TestBranch", "scripted_imodel")


def scripted_imodel(
    script: Any,
    *,
    model: str = "scripted-test",
    **imodel_kwargs: Any,
) -> iModel:
    """Build an iModel wired to a ``ScriptedEndpoint``.

    Args:
        script: a path (str/Path), dict, list of response dicts, or
            ``ScriptModel`` instance.
        model: model name to record in payloads (cosmetic for tests).
        **imodel_kwargs: forwarded to ``iModel(...)``.

    Returns:
        ``iModel`` ready to plug into a ``Branch``.
    """

    script_obj = ScriptModel.coerce(script)
    # iModel doesn't know about ``script``; the endpoint pops it. We pass it
    # through kwargs so EndpointRegistry.match → ScriptedEndpoint sees it.
    return iModel(
        provider="scripted",
        endpoint="chat",
        model=model,
        script=script_obj,
        **imodel_kwargs,
    )


class TestBranch:
    """Factory + introspection helpers for scripted branches.

    Don't instantiate — use the classmethods. They return real ``Branch``
    objects with a ``ScriptedEndpoint`` underneath, so every operation
    (chat, parse, operate, ReAct) goes through the production code path.
    """

    # ─────────────────────────────── factories ────────────────────────────

    @staticmethod
    def from_script(
        script: ScriptModel | dict | list | str | Path,
        *,
        model: str = "scripted-test",
        name: str | None = "TestBranch",
        user: str | None = "tester",
        tools: Any = None,
        system: Any = None,
        **branch_kwargs: Any,
    ) -> Branch:
        """Build a branch backed by ``script``.

        ``script`` accepts any input ``ScriptModel.coerce`` accepts: a path
        (YAML/JSON), a dict, a list of response dicts, or a ``ScriptModel``.
        """
        chat_model = scripted_imodel(script, model=model)
        return Branch(
            chat_model=chat_model,
            parse_model=chat_model,
            name=name,
            user=user,
            tools=tools,
            system=system,
            **branch_kwargs,
        )

    @staticmethod
    def from_responses(
        responses: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Branch:
        """Shortcut for inline tests — pass a flat list of response dicts."""
        return TestBranch.from_script(ScriptModel.from_responses(responses), **kwargs)

    @staticmethod
    def from_text(
        text: str | list[str],
        **kwargs: Any,
    ) -> Branch:
        """Cheapest fixture: one or more pure-text responses, in order.

        ``TestBranch.from_text("hi")`` is equivalent to
        ``TestBranch.from_responses([{"type": "text", "content": "hi"}])``.
        """
        if isinstance(text, str):
            responses = [{"type": "text", "content": text}]
        else:
            responses = [{"type": "text", "content": t} for t in text]
        return TestBranch.from_responses(responses, **kwargs)

    @staticmethod
    def from_yaml(path: str | Path, **kwargs: Any) -> Branch:
        return TestBranch.from_script(ScriptModel.from_yaml(path), **kwargs)

    @staticmethod
    def from_json(path: str | Path, **kwargs: Any) -> Branch:
        return TestBranch.from_script(ScriptModel.from_json(path), **kwargs)

    # ─────────────────────────────── introspection ────────────────────────

    @staticmethod
    def scripted(branch: Branch) -> ScriptedEndpoint:
        """Return the ``ScriptedEndpoint`` driving this branch.

        Raises ``TypeError`` if the branch isn't scripted — useful for
        defending shared fixtures against accidental real-API leaks.
        """
        endpoint = branch.chat_model.endpoint
        if not isinstance(endpoint, ScriptedEndpoint):
            raise TypeError(
                f"branch.chat_model.endpoint is {type(endpoint).__name__}, "
                "not ScriptedEndpoint — was the branch created via TestBranch?"
            )
        return endpoint

    @staticmethod
    def calls(branch: Branch) -> list[RecordedCall]:
        """Recorded API calls so far. Shortcut for ``TestBranch.scripted(branch).calls``."""
        return TestBranch.scripted(branch).calls

    @staticmethod
    def attach_script(branch: Branch, script: Any) -> None:
        """Replace the underlying script in place. Useful when the same branch
        is reused across multiple test steps with different scripts."""
        TestBranch.scripted(branch).attach_script(script)
