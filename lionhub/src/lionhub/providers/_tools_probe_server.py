# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The stdio stand-in MCP server the tools probes hand a coding CLI, and the reader of its log.

A probe writes :data:`MCP_SERVER_SOURCE` to a file and names ``python -I <file> <log> <tool>`` as
an MCP server. The server offers one tool, answers every call of it with :data:`ANSWER`, and
writes each method it receives to the log, one JSON object per line, and nothing but protocol to
its output. :func:`logged_methods` reads the methods back.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ("ANSWER", "MCP_SERVER_SOURCE", "TOOL", "logged_methods")

#: The one tool the server offers, and the text every call of it returns.
TOOL = "ping"
ANSWER = "pong"

MCP_SERVER_SOURCE = """import json
import sys

LOG, TOOL = sys.argv[1], sys.argv[2]


def log(method):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"method": method}) + "\\n")


def reply(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if not isinstance(message, dict):
        continue
    method = message.get("method")
    log(method)
    if "id" not in message:
        continue
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lionagi-tools-probe", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": TOOL,
                    "description": "Answers pong.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        }
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "pong"}]}
    elif method == "ping":
        result = {}
    else:
        reply(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "method not found"},
            }
        )
        continue
    reply({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""


def logged_methods(path: Path) -> list[str]:
    """The methods a stand-in server logged, in order; none when it never started."""
    if not path.is_file():
        return []
    methods = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("method"), str):
            methods.append(entry["method"])
    return methods
