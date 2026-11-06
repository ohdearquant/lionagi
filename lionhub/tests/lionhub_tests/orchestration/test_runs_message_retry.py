# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Run teardown retry-queue outcome propagation."

from __future__ import annotations

import logging

from lionagi.hooks._message_retry import MessagePersistRetryQueue
from tests._runs_message_retry_test_support import _deferred_queue as _deferred_queue
from tests._runs_message_retry_test_support import _event as _event
from tests._runs_message_retry_test_support import _FakeDB as _FakeDB
from tests._runs_message_retry_test_support import _RefusingDB as _RefusingDB


async def test_the_run_teardown_reports_whether_its_queues_emptied():
    """The second teardown that discarded it. What runs after this reads the
    run's completion evidence."""
    from lionhub.orchestration.runs import _flush_pending_message_events

    logger = logging.getLogger("test")
    lost = await _deferred_queue(_RefusingDB(), logger)
    assert await _flush_pending_message_events({"message_retry_queues": [lost]}) is False

    healthy = MessagePersistRetryQueue(_FakeDB(fail_ids=set()), logger=logger, owner="b2")
    await healthy.submit(_event("good-1"))
    assert await _flush_pending_message_events({"message_retry_queues": [healthy]}) is True
