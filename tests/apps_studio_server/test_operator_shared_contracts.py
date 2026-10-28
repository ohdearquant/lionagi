# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Shared project projection, selection vocabulary and frame transactions."""

from __future__ import annotations

import importlib.util
import sys
from typing import Literal, get_args

import pytest


def test_application_project_projection_uses_the_shared_redactor(monkeypatch):
    from lionagi.studio.operator import application_mcp, redact

    class ProjectPath:
        name = "shared-project"

        def __init__(self, _value):
            pass

        def is_absolute(self):
            return True

    monkeypatch.setattr(redact, "Path", ProjectPath)
    assert redact.public_project("/private/original") == "shared-project"
    assert application_mcp.public_project("/private/original") == "shared-project", (
        "application project projection must follow the shared redactor"
    )
    assert application_mcp.public_project is redact.public_project


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (42, None),
        ("/private/workspace/project", "project"),
        ("/", "external-project"),
        (r"C:\private\workspace", "workspace"),
        ("C:\\", "external-project"),
        ("organization/project", "organization/project"),
        ("x" * 161, "x" * 160),
    ],
    ids=[
        "none",
        "empty",
        "non-string",
        "posix",
        "posix-root",
        "windows",
        "windows-root",
        "relative",
        "bounded-relative",
    ],
)
def test_both_project_import_paths_preserve_projection(value, expected):
    from lionagi.studio.operator import application_mcp, redact

    assert redact.public_project(value) == expected
    assert application_mcp.public_project(value) == expected


@pytest.mark.parametrize(
    ("alias_name", "field_name", "vocabulary", "value"),
    [
        ("OperatorProvider", "provider", Literal["new_provider"], "new_provider"),
        ("OperatorEffort", "effort", Literal["new_effort"], "new_effort"),
    ],
    ids=["provider", "effort"],
)
def test_request_vocabulary_follows_the_catalog(
    monkeypatch, alias_name, field_name, vocabulary, value
):
    from pydantic import ValidationError

    from lionagi.studio.operator import catalog, types

    original_request = types.OperatorTurnRequest
    module_name = f"{types.__package__}._types_catalog_contract"
    assert module_name not in sys.modules
    spec = importlib.util.spec_from_file_location(module_name, types.__file__)
    assert spec is not None and spec.loader is not None
    isolated = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(catalog, alias_name, vocabulary)
        patch.setitem(sys.modules, module_name, isolated)
        spec.loader.exec_module(isolated)
        try:
            request = isolated.OperatorTurnRequest.model_validate(
                {
                    "instruction": "Inspect the current view",
                    "context": {"space": "mission", "route": "/", "filters": {}},
                    "expectedLastSequence": 0,
                    field_name: value,
                }
            )
        except ValidationError as error:
            raise AssertionError(
                "wire selection must accept vocabulary from the catalog"
            ) from error
        assert getattr(request, field_name) == value
        assert get_args(getattr(isolated, alias_name)) == (value,)
    assert types.OperatorTurnRequest is original_request
    assert module_name not in sys.modules


@pytest.mark.parametrize(
    ("alias_name", "expected"),
    [
        ("OperatorProvider", ("claude_code", "codex", "gemini_code")),
        ("OperatorEffort", ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")),
    ],
    ids=["provider", "effort"],
)
def test_existing_selection_import_paths_keep_their_vocabulary(alias_name, expected):
    from lionagi.studio.operator import catalog, types

    assert get_args(getattr(catalog, alias_name)) == expected
    assert get_args(getattr(types, alias_name)) == expected


async def _turn_at_limit(tmp_path, monkeypatch, summary_exists):
    from lionagi.studio.operator import store as store_mod

    store = store_mod.OperatorStore(tmp_path / "state.db")
    conversation = await store.create_conversation(title="Transaction ownership")
    conversation_id = conversation["id"]
    accepted = await store.submit_turn(
        conversation_id,
        instruction="hello",
        context={"space": "mission", "route": "/", "filters": {}},
        expected_last_sequence=0,
    )
    request_id = accepted["requestId"]
    assert await store.mark_running(request_id)
    monkeypatch.setattr(store_mod, "MAX_FRAMES_PER_TURN", 1)
    if summary_exists:
        await store.append_frame(conversation_id, request_id, "text", {"content": "first"})
    return store, conversation_id, request_id


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_exists", [False, True], ids=["create-summary", "update-summary"])
async def test_elision_helper_leaves_commit_with_the_transaction_owner(
    tmp_path, monkeypatch, summary_exists
):
    from lionagi.studio.services._db import open_db

    store, conversation_id, request_id = await _turn_at_limit(tmp_path, monkeypatch, summary_exists)
    before_frames = await store.list_frames(conversation_id)
    before_conversation = await store.get_conversation(conversation_id)
    async with open_db(str(store.path())) as db:
        await db.execute("BEGIN IMMEDIATE")
        await store._record_elided_frame(
            db,
            conversation_id=conversation_id,
            request_id=request_id,
            frame_type="text",
            payload_bytes=23,
            sequence=before_conversation["nextSequence"],
            reason="frames_per_turn",
            now=1_700_000_123.0,
        )
        await db.rollback()
    assert await store.list_frames(conversation_id) == before_frames, (
        "elision must not commit work owned by its surrounding transaction"
    )
    assert await store.get_conversation(conversation_id) == before_conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_exists", [False, True], ids=["create-summary", "update-summary"])
async def test_frame_caller_keeps_elision_inside_its_transaction(
    tmp_path, monkeypatch, summary_exists
):
    from lionagi.studio.services._db import open_db

    store, conversation_id, request_id = await _turn_at_limit(tmp_path, monkeypatch, summary_exists)
    before_frames = await store.list_frames(conversation_id)
    before_conversation = await store.get_conversation(conversation_id)
    async with open_db(str(store.path())) as db:
        await db.execute("BEGIN IMMEDIATE")
        await store._append_frame_in_tx(
            db,
            conversation_id=conversation_id,
            request_id=request_id,
            frame_type="text",
            payload={"content": "rolled back"},
            now=1_700_000_123.0,
        )
        await db.rollback()
    assert await store.list_frames(conversation_id) == before_frames, (
        "the frame caller must retain ownership of the elision transaction"
    )
    assert await store.get_conversation(conversation_id) == before_conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_exists", [False, True], ids=["create-summary", "update-summary"])
async def test_public_frame_append_commits_the_elision_summary(
    tmp_path, monkeypatch, summary_exists
):
    from lionagi.studio.operator.store import TRUNCATION_FRAME_TYPE

    store, conversation_id, request_id = await _turn_at_limit(tmp_path, monkeypatch, summary_exists)
    await store.append_frame(conversation_id, request_id, "text", {"content": "committed"})
    frames = await store.list_frames(conversation_id)
    summaries = [frame for frame in frames if frame["type"] == TRUNCATION_FRAME_TYPE]
    assert len(summaries) == 1
    assert summaries[0]["payload"]["elidedFrames"] == (2 if summary_exists else 1)
    assert summaries[0]["payload"]["elidedFrameTypes"] == {"text": 2 if summary_exists else 1}
