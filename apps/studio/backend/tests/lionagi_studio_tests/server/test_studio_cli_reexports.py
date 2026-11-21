# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's command module still exposes the definitions that moved into lionhub, as the same objects."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        ("lionhub.orchestration.schedule.parsing", "build_create_body"),
        ("lionhub.orchestration.schedule.parsing", "add_schedule_subparser"),
        ("lionhub.orchestration.schedule.parsing", "suggest_schedule_flag"),
        ("lionhub.orchestration.schedule.parsing", "_normalize_schedule_reason"),
        ("lionhub.orchestration.schedule.parsing", "QUICK_CREATE_KINDS"),
        ("lionhub.orchestration.schedule.parsing", "_quick_create_add_trigger_flags"),
        ("lionhub.orchestration.schedule.parsing", "_quick_create_add_policy_flags"),
        ("lionhub.orchestration.schedule.parsing", "build_quick_create_parser"),
        ("lionhub.orchestration.console", "_refused_inside_an_agent"),
    ],
)
def test_studio_cli_still_exposes_the_definitions_moved_into_lionhub_orchestration(owner, name):
    """The schedule command parsing, the quick-create parser pieces and the nested-agent refusal
    printer left the Studio command module for their shared owners; the Studio module still
    exposes each one as the very same object."""
    import importlib

    import lionagi_studio.cli as studio_cli

    obj = getattr(importlib.import_module(owner), name)
    assert getattr(studio_cli, name) is obj


def test_studio_reexports_the_shared_url_functions_by_identity():
    from lionagi_studio import cli as compatibility
    from lionhub.orchestration.schedule import parsing as _schedule_parsing

    assert compatibility._base_url is _schedule_parsing._base_url
    assert compatibility._url_authority is _schedule_parsing._url_authority
