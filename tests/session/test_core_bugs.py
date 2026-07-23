# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for core bug fixes: InstructionContent serialization, concurrent observer dispatch, reactive bus role filter."""

from __future__ import annotations

import asyncio

import pytest

from lionagi.protocols.messages.instruction import InstructionContent
from lionagi.session.observer import SessionObserver
from lionagi.session.session import Session
from lionagi.session.signal import Signal, StructuredOutput

# ---------------------------------------------------------------------------
# InstructionContent dict response_format survives serialization
# ---------------------------------------------------------------------------


class TestResponseFormatSerialization:
    def test_dict_response_format_survives_round_trip(self):
        """A plain dict response_format must be present after to_dict → from_dict."""
        fmt = {"name": "str", "score": "float"}
        content = InstructionContent(instruction="test", response_format=fmt)
        serialized = content.to_dict()
        assert "response_format" in serialized, (
            "response_format must be serialized when it is a dict"
        )
        restored = InstructionContent.from_dict(serialized)
        assert restored.response_format == fmt

    def test_nested_dict_response_format_survives(self):
        """Nested dict values in response_format survive serialization."""
        fmt = {"result": {"value": "float", "label": "str"}, "confidence": "float"}
        content = InstructionContent(instruction="test", response_format=fmt)
        serialized = content.to_dict()
        assert serialized["response_format"] == fmt
        restored = InstructionContent.from_dict(serialized)
        assert restored.response_format == fmt

    def test_pydantic_class_response_format_excluded(self):
        """A Pydantic class reference for response_format is still excluded (not serializable)."""
        from pydantic import BaseModel

        class MyModel(BaseModel):
            x: int = 0

        content = InstructionContent(instruction="test", response_format=MyModel)
        serialized = content.to_dict()
        assert "response_format" not in serialized, "Pydantic class refs must be excluded"

    def test_none_response_format_excluded(self):
        """None response_format is excluded (sentinel)."""
        content = InstructionContent(instruction="test", response_format=None)
        serialized = content.to_dict()
        assert "response_format" not in serialized

    def test_non_response_format_fields_still_serialized(self):
        """Other fields like instruction and guidance are still serialized correctly."""
        fmt = {"answer": "str"}
        content = InstructionContent(instruction="hello", guidance="be brief", response_format=fmt)
        serialized = content.to_dict()
        assert serialized["instruction"] == "hello"
        assert serialized["guidance"] == "be brief"
        assert serialized["response_format"] == fmt

    def test_structure_still_excluded(self):
        """The structure field is still excluded even when response_format is a dict."""
        fmt = {"x": "int"}
        content = InstructionContent(instruction="test", response_format=fmt)
        serialized = content.to_dict()
        assert "structure" not in serialized
        assert "_structure_instance" not in serialized


# ---------------------------------------------------------------------------
# Observer handlers run concurrently (non-blocking)
# ---------------------------------------------------------------------------


class TestConcurrentObserverDispatch:
    async def test_multiple_async_handlers_run_concurrently(self):
        """Two async handlers for the same event run concurrently, not sequentially."""
        from lionagi.protocols.generic.event import Event

        class Ping(Event):
            pass

        order: list[str] = []
        started: list[asyncio.Event] = [asyncio.Event(), asyncio.Event()]

        async def handler_a(event, ctx):
            order.append("a_start")
            started[0].set()
            await started[1].wait()  # wait until b also started
            order.append("a_end")
            return "a"

        async def handler_b(event, ctx):
            order.append("b_start")
            started[1].set()
            await started[0].wait()  # wait until a also started
            order.append("b_end")
            return "b"

        obs = SessionObserver()
        obs.observe(Ping, handler_a)
        obs.observe(Ping, handler_b)

        results = await obs.emit(Ping())
        # Both started before either ended → concurrent execution
        assert "a_start" in order and "b_start" in order
        assert set(results) == {"a", "b"}

    async def test_emit_returns_all_handler_results(self):
        """emit() still returns all handler results after making dispatch concurrent."""
        from lionagi.protocols.generic.event import Event

        class MyEvent(Event):
            pass

        s = Session()
        results_collected: list = []

        @s.observe(MyEvent)
        async def h1(event, session):
            return "first"

        @s.observe(MyEvent)
        async def h2(event, session):
            return "second"

        results = await s.emit(MyEvent())
        assert set(results) == {"first", "second"}

    async def test_sync_handlers_still_work(self):
        """Sync handlers run inline and their results are included."""
        from lionagi.protocols.generic.event import Event

        class Ev(Event):
            pass

        s = Session()
        seen = []

        @s.observe(Ev)
        def sync_h(event, session):
            seen.append("sync")
            return "sync_result"

        results = await s.emit(Ev())
        assert "sync" in seen
        assert "sync_result" in results

    async def test_ordering_preserved_in_flow(self):
        """Events are recorded in the Flow in emission order."""
        from lionagi.protocols.generic.event import Event

        class Ordered(Event):
            seq: int = 0

        s = Session()
        obs = s.observer

        for i in range(5):
            await s.emit(Ordered(seq=i))

        stored = obs.by_type(Ordered)
        assert len(stored) == 5
        seqs = [e.seq for e in stored]
        assert seqs == sorted(seqs), "Events must be recorded in emission order"


# ---------------------------------------------------------------------------
# Observe by emitting agent role (RoleFilter)
# ---------------------------------------------------------------------------


class TestObserveByRole:
    async def test_role_filter_matches_signal_with_correct_role(self):
        """RoleFilter fires when emitter_role matches."""
        from pydantic import BaseModel

        class Finding(BaseModel):
            text: str = ""

        s = Session()
        seen: list = []

        @s.observe(role="researcher")
        async def on_researcher(payload, session):
            seen.append(payload)
            return "matched"

        # Emit with matching role
        signal = StructuredOutput(data=Finding(text="discovery"), emitter_role="researcher")
        results = await s.emit(signal)
        assert len(seen) == 1
        assert isinstance(seen[0], Finding)
        assert results == ["matched"]

    async def test_role_filter_does_not_match_different_role(self):
        """RoleFilter does not fire when emitter_role differs."""
        from pydantic import BaseModel

        class Finding(BaseModel):
            text: str = ""

        s = Session()
        seen: list = []

        @s.observe(role="researcher")
        def on_researcher(payload, session):
            seen.append(payload)

        signal = StructuredOutput(data=Finding(text="x"), emitter_role="writer")
        await s.emit(signal)
        assert seen == [], "Role filter must not fire for a different role"

    async def test_role_filter_does_not_match_signal_without_role(self):
        """RoleFilter does not fire when emitter_role is None."""
        from pydantic import BaseModel

        class Finding(BaseModel):
            text: str = ""

        s = Session()
        seen: list = []

        @s.observe(role="researcher")
        def on_researcher(payload, session):
            seen.append(payload)

        # No emitter_role set
        signal = StructuredOutput(data=Finding(text="y"))
        await s.emit(signal)
        assert seen == []

    async def test_role_and_type_combined_filter(self):
        """Combining key and role creates a conjunction filter."""
        from pydantic import BaseModel

        class Finding(BaseModel):
            text: str = ""

        class Citation(BaseModel):
            url: str = ""

        s = Session()
        seen: list = []

        @s.observe(Finding, role="researcher")
        def on_finding_from_researcher(payload, session):
            seen.append(payload)

        # Matching type AND role
        await s.emit(StructuredOutput(data=Finding(text="hit"), emitter_role="researcher"))
        # Matching role but wrong type
        await s.emit(StructuredOutput(data=Citation(url="http://x"), emitter_role="researcher"))
        # Matching type but wrong role
        await s.emit(StructuredOutput(data=Finding(text="miss"), emitter_role="writer"))

        assert len(seen) == 1
        assert seen[0].text == "hit"

    async def test_multiple_role_subscriptions(self):
        """Multiple role subscriptions fire independently."""
        from pydantic import BaseModel

        class Event(BaseModel):
            name: str = ""

        s = Session()
        by_researcher: list = []
        by_writer: list = []

        @s.observe(role="researcher")
        def on_r(payload, session):
            by_researcher.append(payload)

        @s.observe(role="writer")
        def on_w(payload, session):
            by_writer.append(payload)

        await s.emit(StructuredOutput(data=Event(name="r_event"), emitter_role="researcher"))
        await s.emit(StructuredOutput(data=Event(name="w_event"), emitter_role="writer"))
        await s.emit(StructuredOutput(data=Event(name="both"), emitter_role="researcher"))

        assert len(by_researcher) == 2
        assert len(by_writer) == 1
        assert by_writer[0].name == "w_event"

    def test_observe_requires_key_or_role(self):
        """observe() without key or role raises TypeError."""
        obs = SessionObserver()
        with pytest.raises(TypeError):
            obs.observe(None, lambda e, ctx: None)


# ---------------------------------------------------------------------------
# Branch / Session serialization and split regressions
# ---------------------------------------------------------------------------


class TestBranchStateRegressions:
    def test_restored_clone_can_be_reserialized(self):
        """A clone restored via from_dict must serialize again.

        The metadata serializer wrote clone_from as a dict; a restored clone
        keeps that dict, so re-serializing must be idempotent instead of
        dereferencing source.id on a dict.
        """
        import copy

        from lionagi.session.branch import Branch

        s = Session()
        b = s.new_branch()
        b.msgs.add_message(instruction="hi")
        clone = s.split(b.id)

        d1 = clone.to_dict()
        restored = Branch.from_dict(copy.deepcopy(d1))
        # Previously raised AttributeError: 'dict' object has no attribute 'id'.
        d2 = restored.to_dict()
        assert d2["metadata"]["clone_from"] == d1["metadata"]["clone_from"]

    def test_from_dict_does_not_mutate_snapshot(self):
        """Branch.from_dict must not strip fields out of the caller's snapshot."""
        import copy

        from lionagi.session.branch import Branch

        s = Session()
        b = s.new_branch()
        b.msgs.add_message(instruction="hello")
        snap = b.to_dict(include_log_config=True)
        before = copy.deepcopy(snap)

        Branch.from_dict(snap)

        assert snap == before  # reusable for a retry / second restoration
        assert "messages" in snap and "chat_model" in snap

    async def test_asplit_clones_under_message_lock(self, monkeypatch):
        """asplit must clone via aclone (holds the message snapshot lock), not
        the bare sync clone path that can race a concurrent message removal."""
        from lionagi.session.branch import Branch

        s = Session()
        b = s.new_branch()
        b.msgs.add_message(instruction="hi")

        aclone_calls = 0
        real_aclone = Branch.aclone

        async def spy_aclone(self, sender=None):
            nonlocal aclone_calls
            aclone_calls += 1
            return await real_aclone(self, sender)

        monkeypatch.setattr(Branch, "aclone", spy_aclone)

        clone = await s.asplit(b.id)
        assert aclone_calls == 1
        assert clone.id != b.id
        assert len(clone.messages) == len(b.messages)
