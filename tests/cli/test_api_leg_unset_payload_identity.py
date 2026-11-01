# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Capture API-leg payload bytes before changing unset presentation policy.

Only the provider transport is scripted. Set P13_IDENTITY_CAPTURE_DIR to a fresh
private output directory to retain the complete captures; set
P13_IDENTITY_REFERENCE_DIR as well to compare a later run with those exact bytes.
No payload field is normalized, redacted, sorted or removed for comparison.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens
from lionagi.cli.agent import _api_loop_pass, _install_api_input_cap, _profile_api_launch
from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import ActionResponse, Instruction, Message
from lionagi.service._prompt_cache import cache_anchors, protection
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.tools.coding import CodingToolkit
from lionagi.tools.context.context import _result_text
from lionhub.providers.anthropic.messages import (
    AnthropicMessagesEndpoint,
    CreateMessageRequest,
)
from tests.cli.test_agent_api_provider import (
    _LNDL_READ,
    _answer,
    _last_round,
    _last_user_text,
    _lndl_answer,
    _profile,
    _read_round,
    _text,
)

_NEW_KEYS = (
    "tool_result_max_chars",
    "context_fold_at",
    "context_fold_target",
    "context_fold_tail_margin",
    "prompt_caching",
)
_TIME = 1_700_000_000.0
_EVIDENCE = ("evidence é🦁 0123456789 " * 6000)[:100045]
_ANNOTATION = "Context status: continue with the recorded evidence."
_PHASES = (
    "initial_read",
    "after_clip",
    "after_read_result",
    "after_restore_original",
    "after_restore_range",
    "fold_summary",
    "after_fold_summary",
    "context_summary",
    "after_context_summary",
)
_SOURCE_PATHS = (
    "lionagi/cli/agent.py",
    "lionagi/agent/context_fold.py",
    "lionagi/operations/chat/_prepare.py",
    "lionagi/operations/chat/chat.py",
    "lionhub/src/lionhub/lndl/middle/__init__.py",
    "lionhub/src/lionhub/lndl/middle/lndl_middle.py",
    "lionagi/service/connections/endpoint.py",
    "lionagi/service/connections/endpoint_config.py",
    "lionagi/protocols/action/function_calling.py",
    "lionagi/tools/file/reader.py",
    "lionagi/service/imodel.py",
    "lionhub/src/lionhub/providers/anthropic/messages.py",
    "lionagi/tools/coding.py",
    "lionagi/tools/context/context.py",
    "tests/cli/test_agent_api_provider.py",
    "tests/cli/test_api_leg_unset_payload_identity.py",
)


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _FixedNudge:
    def __init__(self):
        self.calls = []

    def record_call(self, tool_name, action, ok):
        self.calls.append((tool_name, action, ok))

    def evaluate(self, *, files_tracked):
        return _ANNOTATION


class _Capture:
    def __init__(self, root: Path, reference: Path | None, case: str):
        self.directory = root / case
        self.directory.mkdir(parents=True, exist_ok=False)
        self.reference = reference / case if reference is not None else None
        self.rows = []

    def add(self, phase: str, kind: str, payload: dict):
        raw = _json_bytes(payload)
        name = f"{len(self.rows):03d}-{phase}-{kind}.json"
        (self.directory / name).write_bytes(raw)
        self.rows.append(
            {
                "ordinal": len(self.rows),
                "phase": phase,
                "kind": kind,
                "file": name,
                "bytes": len(raw),
                "sha256": _digest(raw),
            }
        )
        if self.reference is not None:
            expected = self.reference / name
            assert expected.is_file(), f"missing reference payload: {name}"
            assert raw == expected.read_bytes(), f"payload bytes changed: {name}"

    def finish(self, metadata: dict):
        (self.directory / "payload-index.json").write_bytes(_json_bytes(self.rows))
        (self.directory / "metadata.json").write_bytes(_json_bytes(metadata))
        if self.reference is not None:
            expected = json.loads((self.reference / "payload-index.json").read_bytes())
            assert self.rows == expected, "payload order, phase, content or count changed"


def _private_directory(name: str, fallback: Path | None = None) -> Path | None:
    value = os.environ.get(name)
    if value is None:
        return fallback
    path = Path(value)
    assert path.is_absolute(), f"{name} must be absolute"
    return path


def _payload_text(payload: dict) -> str:
    # Decode the existing YAML representation only for semantic source assertions.
    # _Capture still stores and compares every original payload byte unchanged.
    def texts(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from texts(item)
        elif isinstance(value, list):
            for item in value:
                yield from texts(item)

    parts = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            try:
                parts.extend(texts(yaml.safe_load(content)))
            except yaml.YAMLError:
                parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    try:
                        parts.extend(texts(yaml.safe_load(block["text"])))
                    except yaml.YAMLError:
                        parts.append(block["text"])
    return "\n".join(parts)


def _active_results(branch: Branch) -> list[ActionResponse]:
    return [
        branch.msgs.messages[uid]
        for uid in branch.progression
        if isinstance(branch.msgs.messages[uid], ActionResponse)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", ["react", "lndl"])
@pytest.mark.parametrize(
    ("stored_cache", "filtered", "capability"),
    [(False, True, True), (True, False, True), (True, True, False)],
    ids=["default-filtered", "stored-cache-unfiltered", "stored-cache-capability-off"],
)
async def test_unset_api_leg_payload_bytes(
    tmp_path, monkeypatch, loop, stored_cache, filtered, capability
):
    root = _private_directory("P13_IDENTITY_CAPTURE_DIR", tmp_path / "captures")
    reference = _private_directory("P13_IDENTITY_REFERENCE_DIR")
    case = f"{loop}-cache{int(stored_cache)}-filter{int(filtered)}-cap{int(capability)}"
    repo = Path(__file__).resolve().parents[2]
    assert not root.resolve().is_relative_to(repo), "capture output must stay outside the checkout"
    capture = _Capture(root, reference, case)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text(_EVIDENCE, encoding="utf-8")

    # Fix construction inputs, rather than rewriting identifiers in captured output.
    message_init = Message.__init__
    next_id = 100

    def fixed_message_init(self, **kwargs):
        nonlocal next_id
        kwargs.setdefault("id", UUID(int=next_id))
        next_id += 1
        kwargs.setdefault("created_at", _TIME)
        message_init(self, **kwargs)

    monkeypatch.setattr(Message, "__init__", fixed_message_init)
    monkeypatch.setattr(AnthropicMessagesEndpoint, "caches_system_prefix", capability)
    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        id=UUID(int=2),
        created_at=_TIME,
        prompt_caching=stored_cache,
    )
    if stored_cache:
        model = iModel.from_dict(model.to_dict(), api_key="test-not-a-key")
    # Serialization deliberately omits the request-options class. Install the
    # requested fixture capability explicitly, without mutating another view.
    model.endpoint.config = model.endpoint.config.model_copy(deep=True)
    model.endpoint.config.request_options = CreateMessageRequest if filtered else None
    assert model.is_cli is False
    assert (model.endpoint.config.request_options is not None) is filtered
    assert model.endpoint.config.prompt_caching is stored_cache
    assert model.endpoint.places_prompt_cache is (stored_cache and capability)

    branch = Branch(
        id=UUID(int=1),
        created_at=_TIME,
        system="Keep the recorded evidence and write a concise answer.",
        chat_model=model,
        parse_model=model,
    )
    nudge = _FixedNudge()
    toolkit = CodingToolkit(
        workspace_root=workspace,
        tools=("reader", "search", "context"),
        notify=True,
        nudge_engine=nudge,
    )
    bound_tools = toolkit.bind(branch)
    branch.register_tools(bound_tools)
    context_tool = next(tool for tool in bound_tools if tool.function == "context")
    assert context_tool.postprocessor is not None
    schemas_before = _json_bytes([tool.tool_schema for tool in bound_tools])
    profile = _profile(api_tools="read", api_loop=loop, max_input_tokens="none")
    assert not any(key in profile.extra for key in _NEW_KEYS)
    launch = _profile_api_launch(profile)
    branch.metadata["api_launch"] = dict(launch)
    assert _install_api_input_cap(branch, launch["max_input_tokens"]) is None
    assert branch.chat_model.endpoint.config.prompt_caching is stored_cache
    assert branch.chat_model.endpoint.places_prompt_cache is (stored_cache and capability)

    phase = ""
    replies = []
    sent = []

    async def scripted_call(self, request, *args, **kwargs):
        assert phase in _PHASES, f"unexpected request phase: {phase}"
        capture.add(phase, "sent", request)
        sent.append((phase, _json_bytes(request)))
        assert replies, f"unexpected extra request in {phase}"
        return replies.pop(0)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", scripted_call)

    def compile_checkpoint(label):
        latest = next(
            branch.msgs.messages[uid]
            for uid in reversed(list(branch.progression))
            if isinstance(branch.msgs.messages[uid], Instruction)
        )
        _instruction, request = _prepare_run_kwargs(
            branch, None, ChatParam.from_branch(branch), ins=latest, imodel=branch.chat_model
        )
        payload, _headers = branch.chat_model.endpoint.create_payload(request=request)
        capture.add(label, "compiled-view", payload)
        return payload

    async def main_request(label, *, read=False):
        nonlocal phase
        phase = label
        answer = f"{label} complete"
        if loop == "react":
            replies.extend(
                ([_read_round("notes.txt")] if read else []) + [_last_round(), _answer(answer)]
            )
        else:
            replies.extend(([_text(_LNDL_READ)] if read else []) + [_lndl_answer(answer)])
        before_sent = len(sent)
        result = await _api_loop_pass(branch, "Inspect the recorded evidence.", loop, 3)
        assert getattr(result, "answer", result) == answer
        assert not replies, f"unconsumed scripted response in {label}"
        assert len(sent) > before_sent
        return compile_checkpoint(label)

    async def context_action(arguments):
        before_calls = len(nudge.calls)
        results = await branch.act(
            {"function": "context", "arguments": arguments},
            strategy="sequential",
            suppress_errors=False,
        )
        assert len(results) == 1
        assert len(nudge.calls) == before_calls + 1
        assert nudge.calls[-1] == ("context", "", True)
        result = results[0].output
        assert result["system"] == _ANNOTATION
        return result

    try:
        await main_request("initial_read", read=True)
        originals = [m for m in _active_results(branch) if m.content.function == "reader"]
        assert len(originals) == 1
        original = originals[0]
        original_before = original.to_dict()
        assert original.content.output["success"] is True
        assert _EVIDENCE in original.content.output["content"]
        assert original.content.output["system"] == _ANNOTATION
        assert nudge.calls == [("reader", "", True)]
        source_text, bodies = _result_text(original.content.output)
        index = branch.msgs.progression.index(original.id)
        cap = math.ceil(estimate_next_request_tokens(branch) / 0.8)
        fold = ContextFold(branch, cap=cap)
        if branch.chat_model.endpoint.places_prompt_cache:
            # The read is in the first round, which every fold leaves alone until a request
            # decides it (ADR-0130 Amendment 4); decide it as the next request's compile would.
            view: dict = {}
            _instruction, request = _prepare_run_kwargs(
                branch,
                None,
                ChatParam.from_branch(branch),
                ins=next(
                    branch.msgs.messages[uid]
                    for uid in reversed(list(branch.progression))
                    if isinstance(branch.msgs.messages[uid], Instruction)
                ),
                imodel=branch.chat_model,
                cache_view=view,
            )
            cache_anchors(branch.metadata, view, request["messages"], branch._prompt_cache_protect)
            assert protection(branch.metadata)["included"] is False
        assert await fold._clip_once("threshold")
        assert fold.records[-1]["outcome"] == "landed"
        assert fold.records[-1]["estimate_after"] < fold.records[-1]["estimate_before"]
        copies = [m for m in _active_results(branch) if m.metadata.get("clipped_from")]
        assert len(copies) == 1
        copy = copies[0]
        assert copy.metadata["clipped_from"] == str(original.id)
        assert copy.content.action_request_id is None
        assert original.id not in branch.progression
        assert original.to_dict() == original_before
        clipped_payload = await main_request("after_clip")
        assert 'context(action="read_result"' in _payload_text(clipped_payload)

        body_number = next(i for i, body in enumerate(bodies) if body.key == "content")
        body = bodies[body_number]
        kept = copy.metadata["context_clip_kept"][body_number]
        offset = body.start + 3 * kept // 4
        assert offset < body.end - (kept - 3 * kept // 4)
        page = await context_action(
            {"action": "read_result", "index": index, "offset": offset, "limit": 16000}
        )
        assert page["success"] is True
        assert "limit" not in page
        assert page["text"] == source_text[offset : offset + 16000]
        assert page["total"] == len(source_text)
        page_before = _json_bytes(page)
        page_payload = await main_request("after_read_result")
        assert page["text"] in _payload_text(page_payload)
        assert _json_bytes(page) == page_before

        # The recovery page also occupies the view. Give the real restore admission
        # check its own roomy cap; this fixture characterizes restoration, not refusal.
        restore_guard = ContextFold(branch, cap=1_000_000)
        restored = await context_action({"action": "restore", "start": index})
        assert restored["success"] is True and restored["restored"] == 1
        assert original.id in branch.progression and copy.id not in branch.progression
        await main_request("after_restore_original")
        copy_index = branch.msgs.progression.index(copy.id)
        restored_range = await context_action(
            {"action": "restore", "start": index, "end": copy_index + 1}
        )
        assert restored_range["success"] is True
        assert original.id in branch.progression and copy.id not in branch.progression
        assert list(branch.progression).count(original.id) == 1
        await main_request("after_restore_range")

        phase = "fold_summary"
        replies.append(_text("Recorded fold summary."))
        before_summary = len(sent)
        assert await fold._model_summary([original]) == "Recorded fold summary."
        assert len(sent) == before_summary + 1 and not replies
        summary_payload = json.loads(sent[-1][1])
        assert len(summary_payload["messages"]) == 1
        assert "evidence é" in _last_user_text(summary_payload)
        await main_request("after_fold_summary")

        phase = "context_summary"
        replies.append(_text("Recorded context summary."))
        before_summary = len(sent)
        compacted = await context_action(
            {"action": "compact", "start": 1, "mode": "all", "auto": True}
        )
        assert compacted["success"] is True and compacted["compacted"] > 0
        assert len(sent) == before_summary + 1 and not replies
        assert "evidence é" in _last_user_text(json.loads(sent[-1][1]))
        final_payload = await main_request("after_context_summary")
        assert "[CONTEXT COMPACTION] Recorded context summary." in _payload_text(final_payload)
        assert original.to_dict() == original_before
        assert _json_bytes([tool.tool_schema for tool in bound_tools]) == schemas_before
        assert {label for label, _raw in sent} == set(_PHASES)
        assert all(key not in json.loads(raw) for _label, raw in sent for key in _NEW_KEYS)
        metadata = {
            "loop": loop,
            "profile_extra": profile.extra,
            "stored_prompt_caching": stored_cache,
            "filtered": filtered,
            "capability": capability,
            "effective_prompt_caching": branch.chat_model.endpoint.places_prompt_cache,
            "encoding": "UTF-8 JSON, insertion order, no sorting or field transformation",
            "http_wire_capture": False,
            "fixture_source_sha256": _digest(_EVIDENCE.encode("utf-8")),
            "message_created_at": _TIME,
            "branch_id": str(branch.id),
            "original_id": str(original.id),
            "original_index": index,
            "copy_id": str(copy.id),
            "read_offset": offset,
            "read_text_sha256": _digest(page["text"].encode("utf-8")),
            "clip_record": fold.records[-1],
            "restore_admission_cap": restore_guard.cap,
            "phases_reached": list(dict.fromkeys(label for label, _raw in sent)),
            "tool_postprocessor_calls": nudge.calls,
            "source_sha256": {path: _digest((repo / path).read_bytes()) for path in _SOURCE_PATHS},
        }
        capture.finish(metadata)
    finally:
        await branch.mdls.shutdown()
