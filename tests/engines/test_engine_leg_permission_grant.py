# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""An engine-spawned CLI leg can be granted its provider's tool permissions.

The loss being closed: the per-provider permission table was applied only under
``spec.yolo``, and the engine composed every spec without it, so the table was
unreachable from this path by any route. A headless CLI cannot prompt for a tool
permission, so a leg spawned that way was denied every tool call and reported
success carrying nothing.

No LLM: these read the kwargs the engine put on the built branch's model.
"""

from __future__ import annotations

import pytest

from lionagi.engines.engine import Engine


def _model_kwargs(branch) -> dict:
    """The provider kwargs the factory resolved for this branch's CLI model."""
    return dict(branch.chat_model.endpoint.config.kwargs)


class TestNothingIsGrantedWithoutAsking:
    def test_an_engine_defaults_to_no_auto_approval(self):
        """Auto-approving tool execution is not something a caller receives by
        default. The defect was that asking was impossible, not that the answer
        was no."""
        assert Engine().yolo is False

    @pytest.mark.asyncio
    async def test_a_default_engines_leg_carries_no_permission_grant(self):
        """The companion arm. Every assertion about the grant arriving would
        also pass with the grant hardcoded on, and that would be a worse defect
        than the one being fixed."""
        run = Engine(model="gemini_code/gemini-3.6-flash").new_run()
        branch = await run.make_agent("researcher", name="r")
        assert _model_kwargs(branch).get("yolo") is not True


class TestTheGrantReachesTheLeg:
    @pytest.mark.asyncio
    async def test_an_opted_in_engine_grants_its_gemini_leg_its_tools(self):
        """The defect arm. Before the engine could pass yolo, this kwarg could
        not be produced on this path however the caller asked."""
        run = Engine(model="gemini_code/gemini-3.6-flash", yolo=True).new_run()
        branch = await run.make_agent("researcher", name="r")
        assert _model_kwargs(branch).get("yolo") is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "kwarg", "expected"),
        [
            ("gemini_code/gemini-3.6-flash", "yolo", True),
            ("claude_code/claude-sonnet-5", "permission_mode", "bypassPermissions"),
            ("codex/gpt-5.6", "full_auto", True),
        ],
    )
    async def test_every_cli_provider_gets_its_own_grant_not_geminis(self, model, kwarg, expected):
        """The failure was noticed through gemini because gemini is the provider
        whose default fails loudly. The gate it was stuck behind is the same one
        that delivers bypassPermissions and full_auto, so a gemini-shaped fix
        would have left the other two silently ungranted. Parametrised so a
        provider added to the table without a route here is visible.
        """
        run = Engine(model=model, yolo=True).new_run()
        branch = await run.make_agent("researcher", name="r")
        assert _model_kwargs(branch).get(kwarg) == expected


class TestTheEngineIsWhatDecides:
    @pytest.mark.asyncio
    async def test_the_grant_follows_the_engine_and_not_a_module_default(self):
        """Two runs of two engines, differing only in this switch, must differ
        in the built leg. Asserting the True case alone cannot tell a threaded
        value from a constant.
        """
        granted = (
            await Engine(model="gemini_code/gemini-3.6-flash", yolo=True)
            .new_run()
            .make_agent("researcher", name="a")
        )
        withheld = (
            await Engine(model="gemini_code/gemini-3.6-flash", yolo=False)
            .new_run()
            .make_agent("researcher", name="b")
        )

        assert _model_kwargs(granted).get("yolo") is True
        assert _model_kwargs(withheld).get("yolo") is not True
