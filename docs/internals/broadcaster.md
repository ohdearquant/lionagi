# Broadcaster subscriber results

`Broadcaster.broadcast(event)` validates the event type, snapshots live subscribers,
and invokes them sequentially in registration order. A returned coroutine is awaited
before the next subscriber runs. Results that are not coroutines, including Futures,
Tasks and custom awaitables, are left to the subscriber to manage; broadcasting does
not await, cancel or retrieve their failures.

The shared `maybe_await` helper accepts a keyword-only `classifier`. Its default checks
`inspect.isawaitable` at call time; Broadcaster explicitly supplies `asyncio.iscoroutine`
to preserve its narrower contract. The helper awaits an accepted result once and returns
an unaccepted result unchanged. It does not recursively await nested results.

An ordinary exception raised while invoking or awaiting a subscriber is logged, and
later subscribers still run. Cancellation from an invoked or awaited coroutine propagates
and stops dispatch; cancellation of the broadcasting task also propagates. A returned
Task or Future remains subscriber-owned, including its cancellation and cleanup.
