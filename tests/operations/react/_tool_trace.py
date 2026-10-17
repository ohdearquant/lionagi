# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Script only model replies while exercising real ReAct, parsing and tools."""

import json

from lionagi.testing import TestBranch


def tool_branch(rounds, tools, answer):
    """Build a branch whose model replies are scripted and whose tools really run.

    Each tool-executing branch logs its events and would dump them to ``./data/logs``
    at interpreter exit; the config below keeps a test run from writing there.
    """
    responses = [
        json.dumps({"analysis": "Use the requested tools", "extension_needed": False, **data})
        for data in rounds
    ]
    responses.append(json.dumps({"answer": answer}))
    return TestBranch.from_text(
        responses,
        tools=tools,
        log_config={"auto_save_on_exit": False},
    )
