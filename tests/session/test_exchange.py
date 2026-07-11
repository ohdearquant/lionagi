# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.session.exchange message routing."""

from uuid import UUID, uuid4

import pytest

from lionagi.session import Exchange, Message


class TestExchangeCreation:
    def test_empty_exchange(self):
        exchange = Exchange()

        assert len(exchange) == 0
        assert len(exchange.flows) == 0
        assert exchange.owner_ids == []

    def test_exchange_has_uuid(self):
        exchange = Exchange()
        assert isinstance(exchange.id, UUID)

    def test_exchange_repr(self):
        exchange = Exchange()

        repr_str = repr(exchange)
        assert "Exchange(" in repr_str
        assert "entities=" in repr_str
        assert "pending_out=" in repr_str


class TestExchangeRegistration:
    def test_register_entity(self):
        exchange = Exchange()
        owner_id = uuid4()

        flow = exchange.register(owner_id)

        assert flow is not None
        assert len(exchange) == 1
        assert owner_id in exchange
        assert exchange.has(owner_id)
        assert owner_id in exchange.owner_ids

    def test_register_multiple_entities(self):
        exchange = Exchange()
        owner1 = uuid4()
        owner2 = uuid4()
        owner3 = uuid4()

        exchange.register(owner1)
        exchange.register(owner2)
        exchange.register(owner3)

        assert len(exchange) == 3
        assert owner1 in exchange
        assert owner2 in exchange
        assert owner3 in exchange

    def test_register_duplicate_raises(self):
        exchange = Exchange()
        owner_id = uuid4()

        exchange.register(owner_id)

        with pytest.raises(ValueError, match="already registered"):
            exchange.register(owner_id)

    def test_unregister_entity(self):
        exchange = Exchange()
        owner_id = uuid4()

        exchange.register(owner_id)
        assert owner_id in exchange

        flow = exchange.unregister(owner_id)

        assert flow is not None
        assert owner_id not in exchange
        assert len(exchange) == 0

    def test_unregister_nonexistent(self):
        exchange = Exchange()
        owner_id = uuid4()

        result = exchange.unregister(owner_id)

        assert result is None

    def test_get_entity_flow(self):
        exchange = Exchange()
        owner_id = uuid4()

        registered_flow = exchange.register(owner_id)
        retrieved_flow = exchange.get(owner_id)

        assert retrieved_flow is registered_flow

    def test_get_nonexistent_returns_none(self):
        exchange = Exchange()

        result = exchange.get(uuid4())

        assert result is None


class TestMessageRouting:
    def test_send_creates_message(self):
        exchange = Exchange()
        sender_id = uuid4()
        recipient_id = uuid4()

        exchange.register(sender_id)
        exchange.register(recipient_id)

        msg = exchange.send(sender_id, recipient_id, content={"text": "Hello!"})

        assert isinstance(msg, Message)
        assert msg.sender == sender_id
        assert msg.recipient == recipient_id
        assert msg.content == {"text": "Hello!"}

    def test_send_broadcast_message(self):
        exchange = Exchange()
        sender_id = uuid4()

        exchange.register(sender_id)

        msg = exchange.send(sender_id, None, content={"text": "Broadcast!"})

        assert msg.is_broadcast
        assert msg.recipient is None

    def test_send_from_unregistered_raises(self):
        exchange = Exchange()

        with pytest.raises(ValueError, match="not registered"):
            exchange.send(uuid4(), uuid4(), content={"text": "test"})

    def test_send_with_channel(self):
        exchange = Exchange()
        sender_id = uuid4()

        exchange.register(sender_id)

        msg = exchange.send(sender_id, None, content={"text": "test"}, channel="updates")

        assert msg.channel == "updates"

    @pytest.mark.anyio
    async def test_send_direct(self):
        exchange = Exchange()
        sender_id = uuid4()
        recipient_id = uuid4()

        exchange.register(sender_id)
        exchange.register(recipient_id)

        exchange.send(sender_id, recipient_id, content={"text": "Direct message"})

        count = await exchange.collect(sender_id)
        assert count == 1

        mail = exchange.receive(recipient_id, sender=sender_id)
        assert len(mail) == 1
        assert mail[0].content == {"text": "Direct message"}

    @pytest.mark.anyio
    async def test_send_broadcast(self):
        exchange = Exchange()
        sender_id = uuid4()
        recipient1 = uuid4()
        recipient2 = uuid4()

        exchange.register(sender_id)
        exchange.register(recipient1)
        exchange.register(recipient2)

        exchange.send(sender_id, None, content={"text": "Broadcast to all"})

        count = await exchange.collect(sender_id)
        assert count == 1

        mail1 = exchange.receive(recipient1, sender=sender_id)
        mail2 = exchange.receive(recipient2, sender=sender_id)
        assert len(mail1) == 1
        assert len(mail2) == 1
        assert mail1[0].content == {"text": "Broadcast to all"}
        assert mail2[0].content == {"text": "Broadcast to all"}

    @pytest.mark.anyio
    async def test_receive_messages(self):
        exchange = Exchange()
        sender_id = uuid4()
        recipient_id = uuid4()

        exchange.register(sender_id)
        exchange.register(recipient_id)

        exchange.send(sender_id, recipient_id, content={"text": "Message 1"})
        exchange.send(sender_id, recipient_id, content={"text": "Message 2"})

        await exchange.collect(sender_id)

        mail = exchange.receive(recipient_id, sender=sender_id)
        assert len(mail) == 2
        contents = [m.content["text"] for m in mail]
        assert "Message 1" in contents
        assert "Message 2" in contents

    @pytest.mark.anyio
    async def test_receive_from_multiple_senders(self):
        exchange = Exchange()
        sender1 = uuid4()
        sender2 = uuid4()
        recipient = uuid4()

        exchange.register(sender1)
        exchange.register(sender2)
        exchange.register(recipient)

        exchange.send(sender1, recipient, content={"text": "From sender 1"})
        exchange.send(sender2, recipient, content={"text": "From sender 2"})

        await exchange.collect(sender1)
        await exchange.collect(sender2)

        mail_from_1 = exchange.receive(recipient, sender=sender1)
        mail_from_2 = exchange.receive(recipient, sender=sender2)

        assert len(mail_from_1) == 1
        assert len(mail_from_2) == 1
        assert mail_from_1[0].content == {"text": "From sender 1"}
        assert mail_from_2[0].content == {"text": "From sender 2"}

        all_mail = exchange.receive(recipient)
        assert len(all_mail) == 2

    @pytest.mark.anyio
    async def test_pop_message(self):
        exchange = Exchange()
        sender_id = uuid4()
        recipient_id = uuid4()

        exchange.register(sender_id)
        exchange.register(recipient_id)

        exchange.send(sender_id, recipient_id, content={"text": "Pop me!"})
        await exchange.collect(sender_id)

        msg = exchange.pop_message(recipient_id, sender_id)
        assert msg is not None
        assert msg.content == {"text": "Pop me!"}

        next_msg = exchange.pop_message(recipient_id, sender_id)
        assert next_msg is None


class TestExchangeAsync:
    @pytest.mark.anyio
    async def test_collect_all(self):
        exchange = Exchange()
        sender1 = uuid4()
        sender2 = uuid4()
        recipient = uuid4()

        exchange.register(sender1)
        exchange.register(sender2)
        exchange.register(recipient)

        exchange.send(sender1, recipient, content={"text": "From 1"})
        exchange.send(sender2, recipient, content={"text": "From 2"})

        total = await exchange.collect_all()
        assert total == 2

        mail = exchange.receive(recipient)
        assert len(mail) == 2

    @pytest.mark.anyio
    async def test_sync(self):
        exchange = Exchange()
        sender_id = uuid4()
        recipient_id = uuid4()

        exchange.register(sender_id)
        exchange.register(recipient_id)

        exchange.send(sender_id, recipient_id, content={"text": "Sync this"})

        count = await exchange.sync()
        assert count == 1

        mail = exchange.receive(recipient_id)
        assert len(mail) == 1

    @pytest.mark.anyio
    async def test_collect_unregistered_raises(self):
        exchange = Exchange()

        with pytest.raises(ValueError, match="not registered"):
            await exchange.collect(uuid4())

    @pytest.mark.anyio
    async def test_message_to_unregistered_dropped(self):
        exchange = Exchange()
        sender_id = uuid4()
        unregistered = uuid4()

        exchange.register(sender_id)

        exchange.send(sender_id, unregistered, content={"text": "Lost message"})
        count = await exchange.collect(sender_id)

        assert count == 0

    @pytest.mark.anyio
    async def test_stop_run_loop(self):
        import asyncio

        exchange = Exchange()

        task = asyncio.create_task(exchange.run(interval=0.01))
        await asyncio.sleep(0.05)
        exchange.stop()

        try:
            await asyncio.wait_for(task, timeout=0.5)
        except TimeoutError:
            task.cancel()
            pytest.fail("run() did not stop after stop() was called")

        assert exchange._stop is True
        assert task.done()


class TestExchangeEdgeCases:
    def test_receive_from_nonexistent_owner(self):
        exchange = Exchange()

        result = exchange.receive(uuid4())

        assert result == []

    def test_pop_message_from_nonexistent_owner(self):
        exchange = Exchange()

        result = exchange.pop_message(uuid4(), uuid4())

        assert result is None

    def test_pop_message_no_inbox(self):
        exchange = Exchange()
        owner_id = uuid4()
        sender_id = uuid4()

        exchange.register(owner_id)

        result = exchange.pop_message(owner_id, sender_id)

        assert result is None

    @pytest.mark.anyio
    async def test_stop_before_first_tick_task_group_exits_promptly(self):
        """Mirrors fanout.py's task-group shape: exchange.run() is scheduled via
        start_soon, then the DAG body raises immediately (before run() gets its
        first turn). stop() in `finally` must make run() return right away
        instead of clearing the stop signal and looping forever."""
        from lionagi.ln.concurrency import create_task_group, fail_after

        exchange = Exchange()

        async def _failing_dag():
            raise RuntimeError("boom")

        # anyio's task group wraps a body-raised exception in an
        # ExceptionGroup even with a single failure — same as the real
        # fanout.py call site. fail_after(2) is the regression guard: before
        # the fix, exchange.run() cleared the stop signal on its first turn
        # and looped forever, so the task group's __aexit__ never returned
        # and this would hit the deadline instead of raising promptly.
        with pytest.raises(BaseException) as exc_info:
            with fail_after(2):
                async with create_task_group() as tg:
                    tg.start_soon(exchange.run, 0.01)
                    try:
                        await _failing_dag()
                    finally:
                        exchange.stop()

        raised = exc_info.value
        sub_exceptions = getattr(raised, "exceptions", (raised,))
        assert any(isinstance(e, RuntimeError) and str(e) == "boom" for e in sub_exceptions)
        assert exchange._stop is True

    @pytest.mark.anyio
    async def test_stop_before_first_tick_ensure_future_await_completes(self):
        """Mirrors flow.py's ensure_future + explicit-await shape: exchange.run()
        is scheduled as a task, the DAG raises before it ticks, stop() fires in
        `finally`, and awaiting the runner task must complete promptly (not hang
        forever) so the original exception still propagates to the caller."""
        import asyncio

        from lionagi.ln.concurrency import fail_after

        exchange = Exchange()
        exch_task = asyncio.ensure_future(exchange.run(0.01))

        async def _failing_dag():
            raise RuntimeError("boom")

        try:
            with pytest.raises(RuntimeError, match="boom"):
                await _failing_dag()
        finally:
            exchange.stop()
            with fail_after(2):
                await exch_task

        assert exch_task.done()

    @pytest.mark.anyio
    async def test_collect_empty_outbox(self):
        exchange = Exchange()
        owner_id = uuid4()

        exchange.register(owner_id)

        count = await exchange.collect(owner_id)

        assert count == 0
