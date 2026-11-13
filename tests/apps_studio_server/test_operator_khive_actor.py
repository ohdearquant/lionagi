# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Servers an operator turn is handed carry the operator as their khive actor."""

from __future__ import annotations

import json


def _turn(tmp_path):
    from lionagi.studio.operator.types import OperatorEngineTurn

    async def request_permission(*_args):
        raise AssertionError("branch construction cannot request permission")

    return OperatorEngineTurn(
        conversation_id="conversation",
        request_id="request",
        instruction="inspect recent failures",
        context={},
        history=(),
        request_permission=request_permission,
        store_path=str(tmp_path / "state.db"),
    )


def test_operator_khive_servers_act_as_the_operator(tmp_path, monkeypatch):
    """A knowledge server named in operator_mcp.json is attributed to the operator and
    loses the messaging pack; any other server in that file is handed over untouched."""
    import lionagi._paths as paths_mod
    from lionagi.studio.operator.engine import build_operator_branch

    home = tmp_path / "lionagi-home"
    home.mkdir()
    notes_env = {"KHIVE_ACTOR": "someone-else", "KHIVE_PACKS": "kg,comm"}
    (home / "operator_mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "khive": {
                        "command": "/opt/knowledge/bin/kkernel",
                        "args": ["mcp"],
                        "env": {
                            "KHIVE_ACTOR": "someone-else",
                            "KHIVE_PACKS": "kg, comm ,memory",
                            "OTHER": "kept",
                        },
                    },
                    "kernel": {"command": "/usr/local/bin/kkernel", "args": ["mcp"]},
                    "notes": {
                        "command": "/opt/notes/bin/server",
                        "args": ["serve"],
                        "env": notes_env,
                    },
                },
                "allowed_tools": ["mcp__khive__request"],
            }
        )
    )
    monkeypatch.setattr(paths_mod, "LIONAGI_HOME", home)

    branch = build_operator_branch(_turn(tmp_path))

    servers = branch.chat_model.endpoint.config.kwargs["mcp_servers"]
    assert {"studio_permission", "studio_operator"} <= set(servers)
    assert servers["khive"]["env"] == {
        "KHIVE_ACTOR": "agent:operator",
        "KHIVE_PACKS": "kg,memory",
        "OTHER": "kept",
    }
    assert servers["kernel"]["env"] == {
        "KHIVE_ACTOR": "agent:operator",
        "KHIVE_PACKS": "kg,gtd,memory,brain,schedule,knowledge,session,tool,exec,git,code,workspace,blob",
    }
    assert servers["notes"]["env"] == {"KHIVE_ACTOR": "someone-else", "KHIVE_PACKS": "kg,comm"}
