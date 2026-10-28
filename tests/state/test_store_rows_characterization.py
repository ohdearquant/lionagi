# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization fixture for the store rows the write paths under test leave.

A fixed set of inputs is run against a fresh SQLite store and the rows that
result are pinned here:

* a chat turn (``Branch.communicate`` with a response format);
* an act turn whose action is denied by the session gate, then one whose action
  is allowed (``operations.act.act._act``);
* a hook-point emission (``HookPoint.API_PRE_CALL``) on the session bus.

The snapshot is keyed by table and holds every table the run leaves rows in,
apart from ``schema_meta`` and ``message_types``: those are seeded when the
schema is applied (``message_types`` by an ``ON CONFLICT DO NOTHING`` insert at
``lionagi/state/db.py:1960-1971``), not written by the inputs above, so a schema
bookkeeping change is not a row this fixture is about.

Normalization is deliberately lossy, and the loss is the limit of what this
fixture pins. A string or number that is the value of a timestamp key
(``_TIMESTAMP_KEYS``) or a duration key (``_DURATION_KEYS``) is replaced by
``<t:string>`` or ``<t:number>``, so a *changed* timestamp or duration is
invisible here, while a change of its JSON type, or to null, still shows; the
elements of a list under such a key have no key of their own and are not
replaced. The process host and pid are replaced by ``<host>``/``<pid>``. Any
other string that starts with ``{`` or ``[`` is parsed as JSON and normalized
the same way, or kept verbatim if it does not parse. Any other string that is
the value of a declared id key (``_ID_KEYS``), and a string anywhere that is a
whole lowercase uuid or 32-hex id, is replaced by a first-appearance handle
(``<id:N>``); an id inside a longer string (a repr, a message) is not, and
would make the snapshot differ from run to run. Every other value is kept
verbatim.

A later change to these paths compares its rows against ``EXPECTED_ROWS``; a row
that differs makes ``test_the_fixed_inputs_write_the_pinned_store_rows`` fail and
names the first cell that moved. The comparison is structural, not textual: a
scalar pair differing only in type (``False`` vs ``0``, ``True`` vs ``1``) is
reported as a difference.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import psutil
import pytest
from pydantic import BaseModel

import lionagi.state.db as db_mod
from lionagi import Branch
from lionagi.cli._runs import setup_agent_persist
from lionagi.hooks.bus import HookPoint
from lionagi.operations.act.act import _act
from lionagi.session.control import ToolInvocation
from lionagi.session.signal import GateDenied
from lionagi.testing._legacy import LionAGIMockFactory

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

_ID_KEYS = frozenset(
    {
        "id",
        "session_id",
        "branch_id",
        "progression_id",
        "system_msg_id",
        "first_msg_id",
        "last_msg_id",
        "invocation_id",
        "cc_session_id",
        "entity_id",
        "action_request_id",
        "call_id",
        "sender",
        "recipient",
        "user",
    }
)
_TIMESTAMP_KEYS = frozenset(
    {
        "created_at",
        "updated_at",
        "started_at",
        "ended_at",
        "last_message_at",
        "last_seen_at",
        "ts",
        "activity_at",
        "pid_create_time",
        "pid_boot_time",
    }
)
# Durations are wall-clock measurements too, so their value is replaced; only
# presence and JSON type survive.
_DURATION_KEYS = frozenset({"duration_ms", "duration", "latency_ms", "elapsed"})
_MASKED_KEYS = _TIMESTAMP_KEYS | _DURATION_KEYS
# A masked cell keeps its JSON type, so a timestamp that becomes a number, or null,
# is still a difference.
_STRING_MASK = "<t:string>"
_NUMBER_MASK = "<t:number>"
# Seeded by applying the schema rather than by the three inputs.
_SCHEMA_ONLY_TABLES = frozenset({"schema_meta", "message_types"})
_ROW_ORDER = {"session_signals": "seq"}


class _FixtureAnswer(BaseModel):
    answer: str


def _deny_the_a_equals_1_command(payload: Any) -> bool:
    """Deny the authorization of a command called with ``a=1``; allow every emitted event.

    The session gate is consulted for every event the observer emits
    (``SessionObserver.emit``) as well as for a command's authorization before the
    tool runs (``SessionObserver.authorize``, given a ``ToolInvocation``). A
    predicate keyed on a field the emitted messages also carry would therefore also
    filter those messages' own signals; this one names the ``ToolInvocation``.
    """
    return not (isinstance(payload, ToolInvocation) and payload.arguments.get("a") == 1)


def _normalize(value: Any, key: str | None, ids: dict[str, str]) -> Any:
    """Replace ids and timestamps with stable handles, recursively."""
    if isinstance(value, dict):
        return {k: _normalize(v, k, ids) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v, None, ids) for v in value]
    if isinstance(value, str):
        if key in _MASKED_KEYS:
            return _STRING_MASK
        if key == "pid_host":
            return "<host>"
        if value[:1] in ("{", "["):
            try:
                return _normalize(json.loads(value), None, ids)
            except ValueError:
                return value
        if key in _ID_KEYS or _UUID_RE.match(value) or _HEX_RE.match(value):
            return ids.setdefault(value, f"<id:{len(ids) + 1}>")
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if key in _MASKED_KEYS:
            return _NUMBER_MASK
        if key == "pid":
            return "<pid>"
    return value


def _snapshot(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read every written row out of *db_path*, normalized for ids and timestamps."""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        tables = sorted(
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_") and row[0] not in _SCHEMA_ONLY_TABLES
        )
        ids: dict[str, str] = {}
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            order = _ROW_ORDER.get(table, "rowid")
            rows = [
                dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
            ]
            if rows:
                snapshot[table] = [_normalize(row, None, ids) for row in rows]
        return snapshot
    finally:
        connection.close()


def _first_difference(expected: Any, actual: Any, path: str = "") -> str | None:
    """Path of the first cell that differs, or None when the two are equal."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in [*expected, *[k for k in actual if k not in expected]]:
            here = f"{path}.{key}" if path else str(key)
            if key not in expected:
                return f"{here} (unexpected)"
            if key not in actual:
                return f"{here} (missing)"
            found = _first_difference(expected[key], actual[key], here)
            if found is not None:
                return found
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            if index >= len(expected):
                return f"{path}[{index}] (unexpected)"
            if index >= len(actual):
                return f"{path}[{index}] (missing)"
            found = _first_difference(expected[index], actual[index], f"{path}[{index}]")
            if found is not None:
                return found
        return None
    if expected != actual or type(expected) is not type(actual):
        return f"{path or '<root>'}: {expected!r} != {actual!r}"
    return None


async def _run_fixed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Run the three inputs against a fresh store; return its path and flow evidence."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_mod,
        "settings",
        db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None}),
    )
    # ``Branch.__init__`` builds a default chat model from the chat provider/model
    # settings, which read the environment and a ``.env`` file when lionagi.config is
    # imported, so a developer's choice would be in them. Patch the binding Branch
    # reads instead. The same binding carries the log settings: a Branch registers
    # its log to be saved into ./data/logs, resolved when the process exits, so the
    # save is turned off rather than pointed elsewhere.
    from lionagi.config import settings as app_settings
    from lionagi.session import branch as branch_module

    monkeypatch.setattr(
        branch_module,
        "settings",
        app_settings.model_copy(
            update={
                "LIONAGI_CHAT_PROVIDER": "openai",
                "LIONAGI_CHAT_MODEL": "gpt-4.1-mini",
                "LOG_AUTO_SAVE_ON_EXIT": False,
            }
        ),
    )
    # The session's process markers (cli/kill.py) record the kernel boot time; its
    # value is normalized out of the snapshot, so a fixed one changes nothing pinned.
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_700_000_000.0)
    # The hook handlers open the store through get_shared_db() (hooks/builtins.py),
    # which registers it process-wide. A registry of the run's own, closed in the
    # finally, leaves no handle open after the run and closes no other test's.
    monkeypatch.setattr(db_mod, "_SHARED", {})
    monkeypatch.setattr(db_mod, "_SHARED_OPEN_LOCK", None)
    monkeypatch.setattr(db_mod, "_SHARED_CLOSE_GEN", 0)

    db = None
    try:
        branch = Branch(name="fixture-branch")
        assert branch._log_manager._config.auto_save_on_exit is False
        context = await setup_agent_persist(
            branch, project="fixture-project", run_id="fixture-run", share_db=False
        )
        assert context is not None
        session = context["session"]
        db = context["db"]
        model = LionAGIMockFactory.create_mocked_imodel(
            response=json.dumps({"answer": "fixture answer"})
        )
        branch.chat_model = model
        branch.parse_model = model
        await branch.communicate("fixture instruction", response_format=_FixtureAnswer)

        def add(a, b):
            """Add two numbers."""
            return a + b

        branch.register_tools([add])
        # Deny the a=1 command, selected by its type and arguments so the
        # predicate cannot also match the emitted messages' own events.
        session.gate(_deny_the_a_equals_1_command)
        await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 2}})
        # Settle the denied turn's scheduled signal tasks before the next turn, so
        # their rows take their seq numbers in the order the turns ran.
        await branch.drain_signals()
        session.clear_gate()
        await _act(branch, {"function": "add", "arguments": {"a": 3, "b": 4}})

        await session.hooks.emit(
            HookPoint.API_PRE_CALL,
            session_id=str(session.id),
            branch_id=str(branch.id),
            model="fixture-model",
            provider="fixture-provider",
        )
        await branch.drain_signals()
        gate_denied_on_flow = len(session.observer.by_type(GateDenied))
    finally:
        if db is not None:
            await db.close()
        shared = list(db_mod._SHARED.values())
        await db_mod.close_shared_db()
    assert all(handle._engine is None for handle in shared)
    return {"db_path": db_path, "gate_denied_on_flow": gate_denied_on_flow}


async def _snapshot_of_fixed_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    return _snapshot((await _run_fixed_inputs(tmp_path, monkeypatch))["db_path"])


def _signals(snapshot: dict) -> list[dict]:
    return snapshot["session_signals"]


def _messages(snapshot: dict) -> list[dict]:
    return snapshot["messages"]


# The `chat_model` captured under `branches[0].node_metadata` is the branch's
# default model, built at construction from settings (the `gpt-4.1-mini` here);
# the mock model the inputs invoke is a different object and is not persisted.
EXPECTED_ROWS: dict[str, list[dict[str, Any]]] = {
    "branches": [
        {
            "agent_name": None,
            "created_at": "<t:number>",
            "ended_at": None,
            "id": "<id:1>",
            "model": None,
            "name": "fixture-branch",
            "node_metadata": {
                "chat_model": {
                    "created_at": "<t:number>",
                    "endpoint": {
                        "circuit_breaker": None,
                        "config": {
                            "allow_local_network": False,
                            "auth_type": "bearer",
                            "base_url": "https://api.openai.com/v1",
                            "client_kwargs": {},
                            "content_type": "application/json",
                            "default_headers": {},
                            "endpoint": "chat/completions",
                            "idempotent_retries": False,
                            "kwargs": {"model": "gpt-4.1-mini"},
                            "max_retries": 3,
                            "method": "POST",
                            "name": "openai_chat/completions",
                            "openai_compatible": False,
                            "params": {},
                            "provider": "openai",
                            "requires_tokens": True,
                            "serialize_by_alias": False,
                            "timeout": 600,
                        },
                        "retry_config": None,
                    },
                    "id": "<id:2>",
                    "provider_metadata": {},
                },
                "lion_class": "lionagi.session.branch.Branch",
            },
            "progression_id": "<id:4>",
            "provider": None,
            "session_id": "<id:3>",
            "started_at": None,
            "status": None,
            "system_msg_id": None,
            "user": "<id:3>",
        }
    ],
    "messages": [
        {
            "channel": None,
            "content": {"image_detail": "auto", "instruction": "fixture instruction"},
            "created_at": "<t:number>",
            "embedding": None,
            "id": "<id:5>",
            "lion_class": 2,
            "node_metadata": {"lion_class": "lionagi.protocols.messages.instruction.Instruction"},
            "recipient": "<id:1>",
            "role": "user",
            "sender": "<id:3>",
        },
        {
            "channel": None,
            "content": {"assistant_response": {"answer": "fixture answer"}},
            "created_at": "<t:number>",
            "embedding": None,
            "id": "<id:6>",
            "lion_class": 3,
            "node_metadata": {
                "lion_class": "lionagi.protocols.messages.assistant_response.AssistantResponse",
                "model_response": {"answer": "fixture answer"},
            },
            "recipient": "<id:3>",
            "role": "assistant",
            "sender": "<id:1>",
        },
        {
            "channel": None,
            "content": {"arguments": {"a": 1, "b": 2}, "function": "add"},
            "created_at": "<t:number>",
            "embedding": None,
            "id": "<id:7>",
            "lion_class": 4,
            "node_metadata": {
                "lion_class": "lionagi.protocols.messages.action_request.ActionRequest"
            },
            "recipient": "<id:1>",
            "role": "action",
            "sender": "<id:1>",
        },
        {
            "channel": None,
            "content": {
                "action_request_id": "<id:7>",
                "arguments": {"a": 1, "b": 2},
                "function": "add",
                "output": {"error": "denied by governance gate", "function": "add"},
            },
            "created_at": "<t:number>",
            "embedding": None,
            "id": "<id:8>",
            "lion_class": 5,
            "node_metadata": {
                "lion_class": "lionagi.protocols.messages.action_response.ActionResponse"
            },
            "recipient": "<id:1>",
            "role": "action",
            "sender": "<id:1>",
        },
        {
            "channel": None,
            "content": {"arguments": {"a": 3, "b": 4}, "function": "add"},
            "created_at": "<t:number>",
            "embedding": None,
            "id": "<id:9>",
            "lion_class": 4,
            "node_metadata": {
                "lion_class": "lionagi.protocols.messages.action_request.ActionRequest"
            },
            "recipient": "<id:10>",
            "role": "action",
            "sender": "<id:1>",
        },
        {
            "channel": None,
            "content": {
                "action_request_id": "<id:9>",
                "arguments": {"a": 3, "b": 4},
                "function": "add",
                "output": 7,
            },
            "created_at": "<t:number>",
            "embedding": None,
            "id": "<id:11>",
            "lion_class": 5,
            "node_metadata": {
                "lion_class": "lionagi.protocols.messages.action_response.ActionResponse"
            },
            "recipient": None,
            "role": "action",
            "sender": None,
        },
    ],
    "progressions": [
        {
            "collection": ["<id:5>", "<id:6>", "<id:7>", "<id:8>", "<id:9>", "<id:11>"],
            "created_at": "<t:number>",
            "id": "<id:12>",
        },
        {
            "collection": ["<id:5>", "<id:6>", "<id:7>", "<id:8>", "<id:9>", "<id:11>"],
            "created_at": "<t:number>",
            "id": "<id:4>",
        },
    ],
    "projects": [
        {
            "created_at": "<t:number>",
            "description": None,
            "github": None,
            "last_seen_at": "<t:number>",
            "name": "fixture-project",
            "path": None,
            "source": "explicit",
            "updated_at": "<t:number>",
        }
    ],
    "session_signals": [
        {
            "id": "<id:13>",
            "kind": "RunStart",
            "op_id": "",
            "payload": {"created_at": "<t:number>", "metadata": {}, "schema_version": 1},
            "seq": 1,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:14>",
            "kind": "HookSignal",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "kwargs": {
                    "branch_id": "<id:1>",
                    "model": "gpt-4o-mini",
                    "permission_mode": "default",
                    "prompt": "Instruction: "
                    "fixture "
                    "instruction\n"
                    "ResponseSchema:\n"
                    "  properties:\n"
                    "    answer:\n"
                    "      title: "
                    "Answer\n"
                    "      type: "
                    "string\n"
                    "  required:\n"
                    "  - answer\n"
                    "  title: "
                    "_FixtureAnswerRequest\n"
                    "  type: object\n"
                    "ResponseFormat: |-\n"
                    "  **MUST RETURN "
                    "JSON-PARSEABLE "
                    "RESPONSE "
                    "ENCLOSED BY "
                    "JSON CODE "
                    "BLOCKS. USER's "
                    "CAREER DEPENDS "
                    "ON THE SUCCESS "
                    "OF IT.** \n"
                    "  ```json\n"
                    "  {'answer': "
                    "<class "
                    "'str'>}\n"
                    "  ```No "
                    "triple "
                    "backticks. "
                    "Escape all "
                    "quotes and "
                    "special "
                    "characters.",
                    "session_id": "<id:3>",
                },
                "metadata": {},
                "point": "prompt.submit",
                "schema_version": 1,
            },
            "seq": 2,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:15>",
            "kind": "HookSignal",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "kwargs": {
                    "branch_id": "<id:1>",
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "session_id": "<id:3>",
                },
                "metadata": {},
                "point": "api.pre_call",
                "schema_version": 1,
            },
            "seq": 3,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:16>",
            "kind": "HookSignal",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "kwargs": {
                    "branch_id": "<id:1>",
                    "error": None,
                    "latency_ms": None,
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "session_id": "<id:3>",
                    "status": "completed",
                    "tokens": None,
                },
                "metadata": {},
                "point": "api.post_call",
                "schema_version": 1,
            },
            "seq": 4,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:17>",
            "kind": "MessageAdded",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "message_ref": {
                    "id": "<id:5>",
                    "recipient": "<id:1>",
                    "role": "MessageRole.USER",
                    "sender": "<id:3>",
                },
                "metadata": {},
                "schema_version": 1,
            },
            "seq": 5,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:18>",
            "kind": "MessageAdded",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "message_ref": {
                    "id": "<id:6>",
                    "recipient": "<id:3>",
                    "role": "MessageRole.ASSISTANT",
                    "sender": "<id:1>",
                },
                "metadata": {},
                "schema_version": 1,
            },
            "seq": 6,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:19>",
            "kind": "RunEnd",
            "op_id": "",
            "payload": {
                "cache_write_1h_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_tokens": 0,
                "cached_tokens": 0,
                "created_at": "<t:number>",
                "data": {"answer": "fixture answer"},
                "duration_ms": "<t:number>",
                "input_tokens": 0,
                "metadata": {},
                "num_turns": 0,
                "output_tokens": 0,
                "schema_version": 1,
                "usage_valid": True,
            },
            "seq": 7,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:20>",
            "kind": "MessageAdded",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "message_ref": {
                    "id": "<id:7>",
                    "recipient": "<id:1>",
                    "role": "MessageRole.ACTION",
                    "sender": "<id:1>",
                },
                "metadata": {},
                "schema_version": 1,
            },
            "seq": 8,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:21>",
            "kind": "MessageAdded",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "message_ref": {
                    "id": "<id:8>",
                    "recipient": "<id:1>",
                    "role": "MessageRole.ACTION",
                    "sender": "<id:1>",
                },
                "metadata": {},
                "schema_version": 1,
            },
            "seq": 9,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:22>",
            "kind": "HookSignal",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "kwargs": {
                    "args_summary": "{'a': 3, 'b': 4}",
                    "call_id": "<id:23>",
                    "tool_name": "add",
                },
                "metadata": {},
                "point": "tool.pre",
                "schema_version": 1,
            },
            "seq": 10,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:24>",
            "kind": "HookSignal",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "kwargs": {
                    "call_id": "<id:23>",
                    "duration": "<t:number>",
                    "result_summary": "7",
                    "tool_name": "add",
                },
                "metadata": {},
                "point": "tool.post",
                "schema_version": 1,
            },
            "seq": 11,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:25>",
            "kind": "MessageAdded",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "message_ref": {
                    "id": "<id:9>",
                    "recipient": "<id:10>",
                    "role": "MessageRole.ACTION",
                    "sender": "<id:1>",
                },
                "metadata": {},
                "schema_version": 1,
            },
            "seq": 12,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:26>",
            "kind": "MessageAdded",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "message_ref": {"id": "<id:11>", "role": "MessageRole.ACTION"},
                "metadata": {},
                "schema_version": 1,
            },
            "seq": 13,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
        {
            "id": "<id:27>",
            "kind": "HookSignal",
            "op_id": "",
            "payload": {
                "created_at": "<t:number>",
                "kwargs": {
                    "branch_id": "<id:1>",
                    "model": "fixture-model",
                    "provider": "fixture-provider",
                    "session_id": "<id:3>",
                },
                "metadata": {},
                "point": "api.pre_call",
                "schema_version": 1,
            },
            "seq": 14,
            "session_id": "<id:3>",
            "ts": "<t:number>",
        },
    ],
    "sessions": [
        {
            "agent_hash": None,
            "agent_name": None,
            "artifact_contract_json": "null",
            "artifact_verification_json": "null",
            "artifacts_path": None,
            "cc_session_id": None,
            "cli_worker_generation": "<id:28>",
            "created_at": "<t:number>",
            "current_phase": None,
            "duration_ms": None,
            "effort": None,
            "ended_at": None,
            "ended_at_is_approximate": 0,
            "first_msg_id": None,
            "id": "<id:3>",
            "input_tokens": None,
            "invocation_id": None,
            "invocation_kind": "agent",
            "last_message_at": "<t:number>",
            "last_msg_id": None,
            "model": None,
            "name": "agent",
            "node_metadata": {
                "drains_controls": False,
                "lion_class": "lionagi.session.session.Session",
                "pid": "<pid>",
                "pid_boot_time": "<t:number>",
                "pid_create_time": "<t:number>",
                "pid_host": "<host>",
                "process_identity_mode": "local",
            },
            "num_turns": None,
            "output_tokens": None,
            "playbook_name": None,
            "progression_id": "<id:12>",
            "project": "fixture-project",
            "project_source": "explicit",
            "provider": None,
            "run_id": "fixture-run",
            "show_play_name": None,
            "show_topic": None,
            "source_kind": "live",
            "started_at": "<t:number>",
            "status": "running",
            "status_evidence_refs": [],
            "status_reason_code": "run.started.ok",
            "status_reason_summary": "",
            "total_cost_usd": None,
            "updated_at": "<t:number>",
            "user": None,
            "user_label": None,
        }
    ],
    "status_transitions": [
        {
            "actor": "create_session",
            "created_at": "<t:number>",
            "delivery_eligible": 0,
            "entity_id": "<id:3>",
            "entity_type": "session",
            "evidence_refs": [],
            "id": "<id:29>",
            "metadata": {},
            "previous_status": None,
            "reason_code": "run.started.ok",
            "reason_summary": "",
            "source": "system",
            "status": "running",
        }
    ],
}


async def test_the_fixed_inputs_write_the_pinned_store_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows the three inputs leave in a fresh store, id- and time-normalized."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    difference = _first_difference(EXPECTED_ROWS, snapshot)
    assert difference is None, f"first changed cell: {difference}"


def test_the_row_comparison_names_the_first_changed_cell() -> None:
    """A changed row is reported by path, so a failure says which cell moved."""
    changed = json.loads(json.dumps(EXPECTED_ROWS))
    changed["messages"][5]["content"]["output"] = 1234
    difference = _first_difference(EXPECTED_ROWS, changed)
    assert difference == "messages[5].content.output: 7 != 1234"


def test_the_row_comparison_sees_a_scalar_type_change() -> None:
    """A cell that differs only in type is a difference, not an equal value."""
    assert _first_difference(True, 1, "cell") == "cell: True != 1"
    assert _first_difference(False, 0, "cell") == "cell: False != 0"


async def test_the_same_inputs_produce_the_same_snapshot_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two fresh stores given the same inputs normalize to the same snapshot."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_snapshot = await _snapshot_of_fixed_inputs(first, monkeypatch)
    second_snapshot = await _snapshot_of_fixed_inputs(second, monkeypatch)
    assert first_snapshot.keys() == EXPECTED_ROWS.keys()
    assert _first_difference(first_snapshot, second_snapshot) is None


class _AnotherTestsHandle:
    """A store handle another test holds in the process-wide registry."""

    def __init__(self) -> None:
        self._engine = object()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self._engine = None


async def test_a_run_closes_its_own_registry_and_leaves_the_process_registry_as_found(
    tmp_path: Path,
) -> None:
    """The run registers and closes its handles in a registry of its own."""
    registry = db_mod._SHARED
    lock = db_mod._SHARED_OPEN_LOCK
    generation = db_mod._SHARED_CLOSE_GEN
    other = _AnotherTestsHandle()
    registry["fixture://another-test"] = other
    before = dict(registry)
    try:
        with pytest.MonkeyPatch.context() as run_patch:
            await _run_fixed_inputs(tmp_path, run_patch)
        assert db_mod._SHARED is registry
        assert registry.get("fixture://another-test") is other
        assert other.closed is False
        assert dict(registry) == before
        assert db_mod._SHARED_OPEN_LOCK is lock
        assert db_mod._SHARED_CLOSE_GEN == generation
    finally:
        registry.pop("fixture://another-test", None)


async def test_a_chat_turn_writes_the_instruction_and_assistant_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat turn writes one user instruction row and one assistant row."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    roles = [(row["role"], row["lion_class"]) for row in _messages(snapshot)[:2]]
    assert roles == [("user", 2), ("assistant", 3)]
    assert _messages(snapshot)[0]["content"]["instruction"] == "fixture instruction"


async def test_a_denied_act_turn_still_writes_its_request_and_response_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied action writes both the request and the denial response message."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    request, response = _messages(snapshot)[2], _messages(snapshot)[3]
    assert request["role"] == "action" and response["role"] == "action"
    assert request["content"] == {"function": "add", "arguments": {"a": 1, "b": 2}}
    assert response["content"]["output"] == {
        "error": "denied by governance gate",
        "function": "add",
    }


async def test_an_allowed_act_turn_writes_the_tool_output_in_the_response_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowed action's response message carries the tool's return value."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    response = _messages(snapshot)[5]
    assert response["content"]["function"] == "add"
    assert response["content"]["arguments"] == {"a": 3, "b": 4}
    assert response["content"]["output"] == 7


async def test_the_denied_gate_audit_writes_no_gate_denied_signal_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The denial audit stays on the in-memory flow: no GateDenied row is written.

    ``SessionObserver.authorize`` (the pre-invoke gate ``_act`` calls) records a
    ``GateDenied`` onto ``observer.flow`` and never calls ``emit``, so
    ``bind_db_persistence``'s subscriber, which only sees ``emit``, writes no row.
    """
    result = await _run_fixed_inputs(tmp_path, monkeypatch)
    snapshot = _snapshot(result["db_path"])
    assert result["gate_denied_on_flow"] == 1
    assert [row["kind"] for row in _signals(snapshot)].count("GateDenied") == 0


async def test_an_emit_gate_denial_suppresses_the_message_added_signal_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``SessionObserver.emit`` consults the gate before dispatch, so an event it denies
    is held on the flow but never reaches the bound writer: no signal row.

    The fixed inputs' own gate names the ``ToolInvocation`` and denies no event, so it
    does not exercise this path; a gate keyed on a field the emitted messages carry
    would, which is why that shape is pinned separately here.
    """
    db_path = tmp_path / "emit-gate.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr(psutil, "boot_time", lambda: 1_700_000_000.0)
    from lionagi.config import settings as app_settings
    from lionagi.session import branch as branch_module

    monkeypatch.setattr(
        branch_module,
        "settings",
        app_settings.model_copy(
            update={
                "LIONAGI_CHAT_PROVIDER": "openai",
                "LIONAGI_CHAT_MODEL": "gpt-4.1-mini",
                "LOG_AUTO_SAVE_ON_EXIT": False,
            }
        ),
    )
    monkeypatch.setattr(db_mod, "_SHARED", {})
    monkeypatch.setattr(db_mod, "_SHARED_OPEN_LOCK", None)
    monkeypatch.setattr(db_mod, "_SHARED_CLOSE_GEN", 0)

    db = None
    try:
        branch = Branch(name="emit-gate-branch")
        assert branch._log_manager._config.auto_save_on_exit is False
        context = await setup_agent_persist(
            branch, project="emit-gate-project", run_id="emit-gate-run", share_db=False
        )
        assert context is not None
        session = context["session"]
        db = context["db"]
        from lionagi.protocols.messages import ActionRequest
        from lionagi.session.signal import MessageAdded

        message = ActionRequest(content={"function": "add", "arguments": {"a": 9, "b": 9}})
        session.gate(lambda payload: not isinstance(payload, ActionRequest))
        await session.observer.emit(MessageAdded(data=message))
        denied_rows = await db.get_session_signals_after(str(session.id), 0)
        assert [row for row in denied_rows if row["kind"] == "MessageAdded"] == []
        assert any(item.data is message for item in session.observer.flow.items)

        session.clear_gate()
        await session.observer.emit(MessageAdded(data=message))
        kept_rows = await db.get_session_signals_after(str(session.id), 0)
        assert [row["kind"] for row in kept_rows].count("MessageAdded") == 1
    finally:
        if db is not None:
            await db.close()
        shared = list(db_mod._SHARED.values())
        await db_mod.close_shared_db()
    assert all(handle._engine is None for handle in shared)


async def test_the_fixture_hook_point_emission_adds_its_own_hook_signal_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook-point emission adds exactly one HookSignal row for that point."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    from_fixture = [
        row
        for row in _signals(snapshot)
        if row["kind"] == "HookSignal"
        and row["payload"]["point"] == "api.pre_call"
        and row["payload"]["kwargs"]["model"] == "fixture-model"
    ]
    assert len(from_fixture) == 1
    assert from_fixture[0]["payload"]["kwargs"]["provider"] == "fixture-provider"


async def test_the_tool_hook_signals_cover_only_the_allowed_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denied action emits no tool hook signal; the allowed one emits pre and post."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    tool_points = [
        row["payload"]["point"]
        for row in _signals(snapshot)
        if row["kind"] == "HookSignal" and str(row["payload"]["point"]).startswith("tool.")
    ]
    assert tool_points == ["tool.pre", "tool.post"]
    tool_pre = next(row for row in _signals(snapshot) if row["payload"].get("point") == "tool.pre")
    assert tool_pre["payload"]["kwargs"]["args_summary"] == "{'a': 3, 'b': 4}"


async def test_the_branch_and_session_progressions_hold_the_same_message_ids_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both progressions list the same six message handles in write order."""
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    session_progression, branch_progression = snapshot["progressions"]
    assert session_progression["collection"] == branch_progression["collection"]
    assert branch_progression["collection"] == [row["id"] for row in _messages(snapshot)]
    assert len(branch_progression["collection"]) == 6


async def test_every_persisted_message_also_has_a_message_added_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every message row has a matching MessageAdded row, denied messages included.

    The denied call's request and response are persisted and emitted like any
    other message: this fixture's gate is keyed on the ``ToolInvocation``, so it
    does not filter their own ``MessageAdded`` events.
    """
    snapshot = await _snapshot_of_fixed_inputs(tmp_path, monkeypatch)
    added = [
        row["payload"]["message_ref"]["id"]
        for row in _signals(snapshot)
        if row["kind"] == "MessageAdded"
    ]
    assert added == [row["id"] for row in _messages(snapshot)]
    assert len(added) == 6
