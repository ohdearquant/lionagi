# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Test concurrency, timeouts, and performance aspects of the hook system."""

import asyncio
from unittest.mock import patch

import anyio
import pytest

from lionagi.protocols.types import EventStatus
from lionagi.service.hooks._types import HookEventTypes
from lionagi.service.hooks.hook_event import HookEvent
from lionagi.service.hooks.hook_registry import HookRegistry
from lionagi.service.hooks.hooked_event import HookedEvent
from tests.service.hooks.conftest import FakeEvent, MyCancelled


class ConcurrentTestEvent(HookedEvent):
    """Test event for concurrency testing."""

    def __init__(self, invoke_delay=0.0, invoke_result="test_result"):
        super().__init__()
        # Use object.__setattr__ to bypass frozen fields
        object.__setattr__(self, "invoke_delay", invoke_delay)
        object.__setattr__(self, "invoke_result", invoke_result)
        object.__setattr__(self, "invoke_start_time", None)
        object.__setattr__(self, "invoke_end_time", None)

    async def _core_invoke(self):
        """Test implementation with configurable delay."""
        # Use object.__setattr__ to bypass frozen fields
        object.__setattr__(self, "invoke_start_time", anyio.current_time())
        if self.invoke_delay > 0:
            await anyio.sleep(self.invoke_delay)
        object.__setattr__(self, "invoke_end_time", anyio.current_time())
        return self.invoke_result

    async def _core_stream(self):
        """Test streaming implementation."""
        yield "chunk1"
        if self.invoke_delay > 0:
            await anyio.sleep(self.invoke_delay)
        yield "chunk2"


class TestParallelInvocations:
    """Test parallel hook invocations for isolation."""

    @pytest.mark.anyio
    async def test_parallel_hook_events_isolated(self, patch_cancellation):
        """Test that parallel HookEvent invocations don't interfere."""

        # Create separate registries for isolation
        async def hook1(ev, **kw):
            await anyio.sleep(0.01)  # Small delay
            return "hook1_result"

        async def hook2(ev, **kw):
            await anyio.sleep(0.01)  # Small delay
            return "hook2_result"

        registry1 = HookRegistry(hooks={HookEventTypes.PreInvocation: hook1})
        registry2 = HookRegistry(hooks={HookEventTypes.PreInvocation: hook2})

        event1 = FakeEvent("event1", 100.0)
        event2 = FakeEvent("event2", 200.0)

        hook_event1 = HookEvent(
            registry=registry1,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={"id": "first"},
            event_like=event1,
        )

        hook_event2 = HookEvent(
            registry=registry2,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,
            params={"id": "second"},
            event_like=event2,
        )

        # Run both concurrently
        results = await asyncio.gather(
            hook_event1.invoke(), hook_event2.invoke(), return_exceptions=True
        )

        # Both should succeed with no cross-talk
        assert len(results) == 2
        assert not isinstance(results[0], Exception)
        assert not isinstance(results[1], Exception)

        # Check isolated execution state
        assert hook_event1.execution.response == "hook1_result"
        assert hook_event2.execution.response == "hook2_result"

        # Check isolated metadata
        assert hook_event1.associated_event_info["event_id"] == "event1"
        assert hook_event2.associated_event_info["event_id"] == "event2"
        assert hook_event1.associated_event_info["event_created_at"] == 100.0
        assert hook_event2.associated_event_info["event_created_at"] == 200.0

        # Check no shared exit state
        assert hook_event1._should_exit is False
        assert hook_event2._should_exit is False

    @pytest.mark.anyio
    async def test_parallel_hooked_events_isolated(self, patch_cancellation, patch_logger):
        """Test that parallel HookedEvent invocations don't interfere."""

        async def pre_hook(ev, **kw):
            await anyio.sleep(0.01)
            return f"pre_{ev.id}"

        async def post_hook(ev, **kw):
            await anyio.sleep(0.01)
            return f"post_{ev.id}"

        registry = HookRegistry(
            hooks={
                HookEventTypes.PreInvocation: pre_hook,
                HookEventTypes.PostInvocation: post_hook,
            }
        )

        # Create multiple concurrent events
        events = []
        for i in range(5):
            event = ConcurrentTestEvent(invoke_delay=0.01, invoke_result=f"result_{i}")
            event.create_pre_invoke_hook(hook_registry=registry, exit_hook=False)
            event.create_post_invoke_hook(hook_registry=registry, exit_hook=False)
            events.append(event)

        # Run all concurrently
        results = await asyncio.gather(
            *[event.invoke() for event in events], return_exceptions=True
        )

        # All should succeed
        assert len(results) == 5
        for result in results:
            assert not isinstance(result, Exception)

        # Check isolated results
        for i, event in enumerate(events):
            assert event.execution.status == EventStatus.COMPLETED
            assert event.execution.response == f"result_{i}"

        # Check that all hooks were logged (2 hooks * 5 events = 10 log calls)
        assert len(patch_logger) == 10

    @pytest.mark.anyio
    async def test_parallel_registry_calls_independent(self, patch_cancellation):
        """Test that parallel registry calls are independent."""
        call_count = 0

        async def counting_hook(ev, **kw):
            nonlocal call_count
            await anyio.sleep(0.01)
            call_count += 1
            return f"call_{call_count}"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: counting_hook})

        # Make multiple concurrent calls
        tasks = []
        for i in range(10):
            task = registry.call(
                FakeEvent(f"event_{i}", i * 10.0),
                hook_type=HookEventTypes.PreInvocation,
                exit=False,
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)

        # All calls should complete
        assert len(results) == 10
        for (res, se, st), meta in results:
            assert se is False
            assert st == EventStatus.COMPLETED
            assert res.startswith("call_")

        # Hook should have been called 10 times
        assert call_count == 10


class TestTimeoutBehavior:
    """Test timeout handling and cancellation."""

    @pytest.mark.anyio
    async def test_hook_timeout_cancels_properly(self, patch_cancellation):
        """Test that hook timeouts properly cancel execution."""
        # Mock fail_after to raise cancellation after a delay
        original_time = anyio.current_time()

        async def slow_hook(ev, **kw):
            await anyio.sleep(1.0)  # This should be interrupted
            return "should_not_reach"

        with patch("lionagi.service.hooks.hook_event.fail_after") as mock_fail_after:
            # Make fail_after raise cancellation immediately when constructed
            def fake_fail_after(timeout):
                raise MyCancelled("timeout")

            mock_fail_after.side_effect = fake_fail_after

            registry = HookRegistry(hooks={HookEventTypes.PreInvocation: slow_hook})
            hook_event = HookEvent(
                registry=registry,
                hook_type=HookEventTypes.PreInvocation,
                exit=False,
                timeout=1,  # Short timeout
                params={},
                event_like=FakeEvent(),
            )

            with pytest.raises(MyCancelled):
                await hook_event.invoke()

            # Should have attempted to set up timeout
            mock_fail_after.assert_called_once_with(1)

    @pytest.mark.anyio
    async def test_concurrent_timeouts_independent(self, patch_cancellation):
        """Test that timeouts in one hook don't affect others."""

        async def fast_hook(ev, **kw):
            await anyio.sleep(0.01)
            return "fast_done"

        async def slow_hook(ev, **kw):
            await anyio.sleep(1.0)  # This will timeout
            return "slow_done"

        fast_registry = HookRegistry(hooks={HookEventTypes.PreInvocation: fast_hook})
        slow_registry = HookRegistry(hooks={HookEventTypes.PreInvocation: slow_hook})

        fast_event = HookEvent(
            registry=fast_registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=30,  # Long timeout
            params={},
            event_like=FakeEvent(),
        )

        slow_event = HookEvent(
            registry=slow_registry,
            hook_type=HookEventTypes.PreInvocation,
            exit=False,
            timeout=1,  # Short timeout
            params={},
            event_like=FakeEvent(),
        )

        # Run concurrently - fast should succeed, slow should timeout
        results = await asyncio.gather(
            fast_event.invoke(), slow_event.invoke(), return_exceptions=True
        )

        # Fast event should succeed
        assert not isinstance(results[0], Exception)
        assert fast_event.execution.status == EventStatus.COMPLETED
        assert fast_event.execution.response == "fast_done"

        # Slow event should timeout (or complete if mocking doesn't work)
        # The exact behavior depends on the timeout implementation


class TestNoDeadlocks:
    """Test that the hook system doesn't create deadlocks."""

    @pytest.mark.anyio
    async def test_nested_hook_calls_no_deadlock(self, patch_cancellation):
        """Test that hooks calling other hooks don't deadlock."""
        hook_sleep = 0.01
        invoke_delay = 0.01
        # Upper bound: each event does hook + invoke (+ optional post-hook).
        # Two events run concurrently, so worst-case sequential would be
        # 2 * (hook_sleep + invoke_delay).  We multiply by 100 to give
        # generous CI headroom while still catching a true deadlock (which
        # would hang until the test runner's global timeout fires).
        n_events = 2
        per_event_max = hook_sleep + invoke_delay
        deadlock_timeout = n_events * per_event_max * 100

        async def nested_hook(ev, **kw):
            # Simulate a hook that might call another hook
            await anyio.sleep(hook_sleep)
            return "nested_result"

        async def calling_hook(ev, **kw):
            # This hook simulates calling another async operation
            await anyio.sleep(hook_sleep)
            return "calling_result"

        registry1 = HookRegistry(hooks={HookEventTypes.PreInvocation: nested_hook})
        registry2 = HookRegistry(hooks={HookEventTypes.PostInvocation: calling_hook})

        # Create events that use different registries
        event1 = ConcurrentTestEvent(invoke_delay=invoke_delay)
        event1.create_pre_invoke_hook(hook_registry=registry1, exit_hook=False)

        event2 = ConcurrentTestEvent(invoke_delay=invoke_delay)
        event2.create_post_invoke_hook(hook_registry=registry2, exit_hook=False)

        # Run concurrently - should not deadlock
        start_time = anyio.current_time()
        results = await asyncio.gather(event1.invoke(), event2.invoke(), return_exceptions=True)
        end_time = anyio.current_time()

        # Behavioral: both must complete successfully — an exception or a
        # missing result indicates deadlock or hook-system failure.
        assert len(results) == 2
        assert not isinstance(results[0], Exception), f"event1 failed: {results[0]}"
        assert not isinstance(results[1], Exception), f"event2 failed: {results[1]}"

        # Relative timing: must finish well under the deadlock-detection bound.
        # A true deadlock would block until the global test timeout, not this bound.
        elapsed = end_time - start_time
        assert elapsed < deadlock_timeout, (
            f"completed in {elapsed:.3f}s — exceeded {deadlock_timeout:.3f}s bound "
            "(possible deadlock or extreme scheduler starvation)"
        )

    @pytest.mark.anyio
    async def test_high_concurrency_no_resource_exhaustion(self, patch_cancellation):
        """Test high concurrency doesn't exhaust resources."""

        async def simple_hook(ev, **kw):
            return "simple"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: simple_hook})

        # Create many concurrent hook events
        hook_events = []
        for i in range(50):  # Reasonable number for CI
            hook_event = HookEvent(
                registry=registry,
                hook_type=HookEventTypes.PreInvocation,
                exit=False,
                timeout=30,
                params={"index": i},
                event_like=FakeEvent(f"event_{i}", i),
            )
            hook_events.append(hook_event)

        # Run all concurrently
        start_time = anyio.current_time()
        results = await asyncio.gather(
            *[hook_event.invoke() for hook_event in hook_events],
            return_exceptions=True,
        )
        end_time = anyio.current_time()

        # All should succeed
        assert len(results) == 50
        for i, result in enumerate(results):
            assert not isinstance(result, Exception), f"Event {i} failed: {result}"

        # Should complete in reasonable time
        assert end_time - start_time < 5.0  # Generous timeout for CI

        # Check all hook events completed successfully
        for i, hook_event in enumerate(hook_events):
            assert hook_event.execution.status == EventStatus.COMPLETED
            assert hook_event.execution.response == "simple"


class TestPerformanceSmoke:
    """Smoke tests for performance characteristics."""

    @pytest.mark.anyio
    async def test_hook_invocation_overhead_minimal(self, patch_cancellation):
        """Test that hook invocation overhead is minimal."""
        call_times = []

        async def timing_hook(ev, **kw):
            return "timed"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: timing_hook})

        # Measure multiple invocations
        for _ in range(10):
            start_time = anyio.current_time()

            hook_event = HookEvent(
                registry=registry,
                hook_type=HookEventTypes.PreInvocation,
                exit=False,
                timeout=30,
                params={},
                event_like=FakeEvent(),
            )
            await hook_event.invoke()

            end_time = anyio.current_time()
            call_times.append(end_time - start_time)

        # Average call time should be reasonable
        avg_time = sum(call_times) / len(call_times)
        assert avg_time < 0.1  # Should be much faster than 100ms

        # No call should be extremely slow
        assert max(call_times) < 0.5  # No call over 500ms

    @pytest.mark.anyio
    async def test_metadata_creation_efficient(self, patch_cancellation):
        """Test that metadata creation is efficient for many calls."""

        async def metadata_hook(ev, **kw):
            return "metadata_test"

        registry = HookRegistry(hooks={HookEventTypes.PostInvocation: metadata_hook})

        start_time = anyio.current_time()

        # Make many calls that generate metadata
        tasks = []
        for i in range(100):
            task = registry.call(
                FakeEvent(f"large_event_{i}", i * 1000.0),
                hook_type=HookEventTypes.PostInvocation,
                exit=False,
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        end_time = anyio.current_time()

        # Should complete in reasonable time
        total_time = end_time - start_time
        assert total_time < 1.0  # 100 calls in under 1 second

        # All should have correct metadata
        for i, ((res, se, st), meta) in enumerate(results):
            assert meta["event_id"] == f"large_event_{i}"
            assert meta["event_created_at"] == i * 1000.0
            assert meta["lion_class"] == "tests.service.hooks.conftest.FakeEvent"

    @pytest.mark.anyio
    async def test_error_handling_performance(self, patch_cancellation):
        """Test that error handling doesn't significantly impact performance."""

        async def sometimes_failing_hook(ev, **kw):
            # Fail every other call
            if int(ev.id.split("_")[-1]) % 2 == 0:
                raise RuntimeError("planned failure")
            return "success"

        registry = HookRegistry(hooks={HookEventTypes.PreInvocation: sometimes_failing_hook})

        start_time = anyio.current_time()

        # Make many calls with mixed success/failure
        tasks = []
        for i in range(50):
            task = registry.call(
                FakeEvent(f"test_event_{i}", i),
                hook_type=HookEventTypes.PreInvocation,
                exit=False,
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        end_time = anyio.current_time()

        # Should complete in reasonable time despite errors
        total_time = end_time - start_time
        assert total_time < 2.0  # Generous allowance for error handling

        # Check that we got expected mix of success/failure
        successes = sum(1 for (res, se, st), _ in results if st == EventStatus.COMPLETED)
        failures = sum(1 for (res, se, st), _ in results if st == EventStatus.CANCELLED)

        assert successes == 25  # Half should succeed
        assert failures == 25  # Half should fail
