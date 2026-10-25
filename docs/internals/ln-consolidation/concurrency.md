# Concurrency contracts

Issue #3586 baseline: `ee5783cc689fbff53afef49f772e6584121b70e6`. Positions use this revision. Changed entries are implemented; focused validation passed. KEEP rows describe source contracts.

Pinned source: `ee5783cc689fbff53afef49f772e6584121b70e6`. Existing shared calls are separate from the fourteen implemented conversions with focused validation passed. The remaining native operations below retain their existing behavior.

| Unit | Current complete-scope disposition|
| --- | ---|
| A1 | Residual shared-helper conversion is implemented, focused validation passed.|
| A2 | Original compatible counterparts already use shared helpers; retain existing behavior.|
| A3 | Residual shared-helper conversion is implemented, focused validation passed.|
| A4 | Original compatible counterparts already use shared helpers; retain existing behavior.|
| A5 | Original compatible counterparts already use shared helpers; retain existing behavior.|
| A6 | Original compatible counterparts already use shared helpers; retain existing behavior.|
| A7 | Original compatible counterparts already use shared helpers; retain existing behavior.|
| A8 | Original compatible counterparts already use shared helpers; retain existing behavior.|
| A9 | Residual shared-helper conversion is implemented, focused validation passed.|
| B1 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B2 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B3 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B4 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B5 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B6 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B7 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B8 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| B9 | Keep existing native behavior; exact per-site differences and consumer statements follow.|
| H1 | Helper prerequisite already merged; fourteen implemented caller conversions are listed separately.|
| H2 | Keep three native asyncio queues; therefore add no no-argument/unbounded/default/exception parity API to public ln.Queue.|
| H3 | Keep13 native locks/1native semaphore and their locked() consumers; therefore add no new public locked() API or fast-acquire mode.|
| H4 | Keep all7 native asyncio.run entries; therefore add no new main-thread mode/public name or signal policy.|

The baseline address index uses `U:` plus the unit name for these original unit populations.

## Native-call contracts at the baseline

**Contracts**

- **K2.1** — KEEP. Keep exact native thread, event/local storage or affinity object: daemon/target/join/wait/`is_set` and per-thread ownership are synchronous contracts, not AnyIO task ownership.
- **K2.2** — KEEP. Keep native synchronous cross-thread exclusion and with/acquire/release semantics for this exact state/cache/registry. An async lock cannot replace synchronous or reentrant critical sections without changing callers.
- **K2.3** — KEEP. Keep the returned native Future/Task handle and its lifetime beyond the current lexical scope; consumers retain/cancel/drain the handle. A structured task-group substitution changes ownership/cancellation/result access.
- **K2.4** — KEEP. Keep caller-thread event-loop/context and signal ownership, and native refusal inside an already-running loop. `run_async` creates a separate non-daemon thread/AnyIO loop and temporarily takes signal handlers; no main-thread helper mode is added.
- **K2.5** — KEEP. Keep native Task/Future ownership, copied-task context when a coroutine is wrapped, zero/negative timeout no-start behavior, and Python3.10 asyncio.TimeoutError class. Scope timeout helpers change these observable boundaries; existing catch clauses remain as recorded.
- **K2.6** — Implemented, focused validation passed. Local import retained; exact no-argument status worker/result identity, ContextVar copy and default shield. OSError alone becomes {error: class name}; other errors retain identity.
- **K2.7** — Implemented, focused validation passed. Keep explicit `abandon_on_cancel`=True, same envelope, `maybe_await` outside offload, Exception logging/outcome recording and external cancellation.
- **K2.8** — KEEP. Keep exact done/pending task sets, timeout-as-result and `return_when` semantics. Neither ln.gather nor a cancel scope returns this ownership/membership protocol.
- **K2.9** — KEEP. Keep `return_exceptions`=True ordered native result collection and native KeyboardInterrupt/SystemExit behavior; shared gather stores BaseException values. Preserve these retained task handles and shutdown/checkpoint drain order.
- **K2.10** — KEEP. Keep Future-style asyncio shield of this existing task, including survival after waiter cancellation and later explicit reap. ln.shield is a scope/context mechanism, not this returned Future.
- **K2.11** — KEEP. Keep concrete current asyncio Task identity used by engine/dispatch origin or scheduler ownership; no helper supplies equivalent object identity and lineage.
- **K2.12** — KEEP. Keep first-error propagation with siblings left running and original exception/cause shape; ln.gather has task-group sibling cancellation/group policy. Preserve the existing drain/reap lifetime.
- **K2.13** — KEEP. Keep asyncio lock uncontended-acquire scheduling, native owner/reacquire behavior, exposed lock type and locked() API. The ln wrapper adds a checkpoint and omits locked(); do not change the current admission/serialization window.
- **K2.14** — KEEP. Keep concrete asyncio Process/StreamReader handles and exact argv/env/cwd/stdin/stdout/stderr/session options. Process spawning/identity/reaping is explicitly outside this concurrency issue and cannot be replaced with a generic worker helper.
- **K2.15** — KEEP. Keep this exact callable/args/businesskwargs in the loop default executor, copied context and immediate cancelled-wait behavior. Even `abandon_on_cancel`=True does not preserve the executor concurrency bound when replaced with AnyIO `run_sync`.
- **K2.16** — KEEP. Keep loop-object ownership for scheduling, cache/lock affinity or isolated task operations; this call is not an elapsed-time read eligible for A5.
- **K2.17** — KEEP. Keep the explicit unbounded send/receive stream pair and its async-iterator/close/EndOfStream ownership; ln.Queue is a different aggregate interface, not an interchangeable pair.
- **K2.18** — KEEP. Keep the existing AnyIO event object within the streaming driver, including its native type and checkpoint behavior; no new wrapper identity or broader task/stream refactor is required.
- **K2.19** — KEEP. Keep asynchronous path-object methods at snapshot write/unlink seams. ln synchronous file/path helpers are not replacements for this object's awaited I/O protocol.
- **K2.20** — KEEP. Keep asynchronous file context/awaited write/close ordering at this snapshot/chunk boundary; a synchronous file helper would change blocking/cancellation and resource lifetime.
- **K2.21** — KEEP. Keep native already-set wait scheduling and exposed/reset-capable Event identity; ln.Event adds a checkpoint and has no clear(). Do not alter this completion/startup readiness boundary.
- **K2.22** — KEEP. Keep asyncio unbounded-default/maxsize0 meaning, immediate nonempty get, QueueFull/QueueEmpty and qsize/empty APIs. The memory-stream Queue constructor/buffering/checkpoint/errors are not equivalent.
- **K2.23** — KEEP. Keep named native Task ownership and concrete handle/cancel/done-callback/drain behavior; these detached/supervised tasks are explicitly outside the issue's helper substitution list.
- **K2.24** — Implemented, focused validation passed. Keep random 0.005..0.05 spacing, monotonic admission-lock deadline and BaseException fd close/rethrow. No process/provider launched.
- **K2.25** — Implemented, focused validation passed. Keep quick retry random 0.001..0.005, all-or-none partial slot release and reversed admission unlocks before await; admission notification order stays False then True.
- **K2.26** — Implemented, focused validation passed. Keep min(remaining, poll*jitter), jitter 0.75..1.25, slept/retry/deadline order, final read and exact refusal message/class/pool/cap/holders.
- **K2.27** — KEEP. Keep invoke-first coroutine-only classification, sequential awaiting, ordinary Exception isolation/logging, propagated cancellation, no deadline, and event-type preflight. Futures/Tasks/general awaitables remain unawaited under accepted ADR0120Amendment1.
- **K2.28** — Implemented, focused validation passed. Keep explicit `abandon_on_cancel`=True, exact envelope, `maybe_await` outside offload, cancellation propagation, swallowed/logged handler failure, sibling concurrency and existing shared deadline.
- **K2.29** — Implemented, focused validation passed. Asyncio-only boundary. Keep same delay, suppress only asyncio.CancelledError, check stopping after the await and spawn only afterwards; no Trio compatibility claim.
- **K2.30** — Implemented, focused validation passed. Asyncio-only boundary. Keep original monotonic deadline and remaining calculation; stray cancel retries remaining interval; stopping cancel rethrows. No task/context wrapper or deadline helper.
- **K2.31** — KEEP. Keep gather as a retained future for stdout/stderr tail draining; the future is explicitly cancelled/awaited through that handle in finally. Task-group gather is not a substitutable handle.
- **K2.32** — Implemented, focused validation passed. Same no-argument snapshot worker, ContextVar and result/error identity/default shield. Cache sequence, duration, counters and publication stay after worker in original caller.
- **K2.33** — Implemented, focused validation passed. Preserve prebound partial exactly: row, now, `stale_seconds`, `ps_snapshot`, shared `lock_cache` and `lock_budget`. Same phantom result, exception identity, copied context and shield.
- **K2.34** — Implemented, focused validation passed. Keep lazy `code_identity` import inside existing try. Exact worker result and context/default shield; existing catch still returns identical unknown drift class/message string.
- **K2.35** — Implemented, focused validation passed. Preserve prebound stale-lock partial with artifacts/cutoff/cache/budget and scan result/truncation interpretation. Error identity, context and shield stay unchanged.
- **K2.36** — Implemented, focused validation passed. Preserve prebound stale-lock partial, cutoff and shared cache/budget. Health, later phantom classifier, CAS and audit writes remain original; default shield/context/error identity.
- **K2.37** — Implemented, focused validation passed. Preserve prebound phantom partial including liveness snapshot and same cache/budget as prior scan. Same classifier result used in existing reason/CAS/audit path; context/error identity/shield.
- **K2.38** — KEEP. Keep native uncontended admission and locked() precheck. `launch_detached_argv` checks locked then acquires without an intervening suspension; ln.Semaphore differs in checkpoint/API.
- **K2.39** — KEEP. Keep explicit asyncio.TimeoutError construction for the scripted endpoint test double. Python3.10 class identity participates in timeout/retry contracts; this is not a timeout scope.
- **K2.40** — KEEP. Keep native loop-task snapshots for cleanup/leak comparison. Replacing task introspection with a task-group abstraction loses the exact baseline/current task-set protocol.

**Operations**

- **2.1** — threading.Thread; K2.1.
- **2.2** — threading.Lock; K2.2.
- **2.3** — `asyncio.ensure_future`; K2.3.
- **2.4** — asyncio.run; K2.4.
- **2.5** — `asyncio.wait_for`; K2.5.
- **2.6** — `anyio.to_thread.run_sync`; K2.6.
- **2.7** — `anyio.to_thread.run_sync`; K2.7.
- **2.8** — asyncio.wait; K2.8.
- **2.9** — asyncio.gather; K2.9.
- **2.10** — asyncio.shield; K2.10.
- **2.11** — `asyncio.current_task`; K2.11.
- **2.12** — asyncio.gather; K2.12.
- **2.13** — asyncio.Lock; K2.13.
- **2.14** — `asyncio.create_subprocess_exec`; K2.14.
- **2.15** — `asyncio.to_thread`; K2.15.
- **2.16** — threading.RLock; K2.2.
- **2.17** — `asyncio.get_running_loop`; K2.16.
- **2.18** — `anyio.create_memory_object_stream`; K2.17.
- **2.19** — anyio.Event; K2.18.
- **2.20** — anyio.Path; K2.19.
- **2.21** — `anyio.open_file`; K2.20.
- **2.22** — asyncio.Event; K2.21.
- **2.23** — asyncio.Queue; K2.22.
- **2.24** — `asyncio.create_task`; K2.23.
- **2.25** — anyio.sleep; K2.24.
- **2.26** — anyio.sleep; K2.25.
- **2.27** — anyio.sleep; K2.26.
- **2.28** — asyncio.iscoroutine; K2.27.
- **2.29** — threading.local; K2.1.
- **2.30** — `threading.current_thread`; K2.1.
- **2.31** — `anyio.to_thread.run_sync`; K2.28.
- **2.32** — threading.Event; K2.1.
- **2.33** — asyncio.sleep; K2.29.
- **2.34** — asyncio.sleep; K2.30.
- **2.35** — asyncio.gather; K2.31.
- **2.36** — `anyio.to_thread.run_sync`; K2.32.
- **2.37** — `anyio.to_thread.run_sync`; K2.33.
- **2.38** — `anyio.to_thread.run_sync`; K2.34.
- **2.39** — `anyio.to_thread.run_sync`; K2.35.
- **2.40** — `anyio.to_thread.run_sync`; K2.36.
- **2.41** — `anyio.to_thread.run_sync`; K2.37.
- **2.42** — asyncio.Semaphore; K2.38.
- **2.43** — asyncio.TimeoutError; K2.39.
- **2.44** — `asyncio.all_tasks`; K2.40.

The checkpoint lock `default_factory` and native primitive methods/types, Futures/Tasks, cancellation classes and ContextVar/loop references are separately enumerated in the tables below. They are not additional original issue counts. Existing detached tasks, subprocess APIs, wait sets and shielded Futures retain their explicit ownership protocols.

Broadcaster retains the Accepted ADR0120Amendment1 coroutine-only classifier, sequential dispatch, ordinary-error isolation, propagated cancellation, no deadline and event-type preflight. Its Future/Task ownership is not broadened. Queue parity, locked(), and a new `run_async` mode are not added while their native consumers remain.

The process-group timeout rewrites landed in #3617/#3618, and the offload helper extension landed in #3733. Focused validation passed for the fourteen caller conversions below; this inventory makes no per-KEEP runtime claim.

## Already-shared compatible counterparts

A1 has44 existing shared calls in the original source-path set:40 source lines attributed to#3658 and4 to#3701. A3 has28:4 attributed to#3658 and24 to#3701; its two scheduler calls are implemented, focused validation passed. A2 has6 current shared calls (five#3658 and one#3629). A5 has11 (#3629), and A8 has its one scheduler returned-value dispatch (#3658). The baseline address index lists the current shared-helper sites.

A4 currently has9 shared clock reads in its original source paths, including one later liveness/admission read; the historical issue count was8. A6 currently has6 shared scopes in its original paths, including the additional API-turn scope; the historical count was5. A7 has5 current shared timeout scopes, including a later liveness rewrite. These current locations are source facts, not extra issue-credit counts. Additional helper use elsewhere is recorded separately and is not automatically an original migration.

## Implemented caller conversions

The following stable functions use existing shared helpers; focused validation passed. Module/line indexes refer to the baseline expression. A ×N suffix retains N occurrences at the same location; JSON line:column indexes preserve individual calls.

**Contracts**

- **K3.1** — Implemented; focused validation passed. The same caller-specific argument, context, result and error contract is retained in the native-contract section above.

**Operations**

- **3.1** — `_subagent_status`; K3.1.
- **3.2** — `deliver_flow_notify_now`; K3.1.
- **3.3** — `admit_subagent`; K3.1.
- **3.4** — `_lock_admission`; K3.1.
- **3.5** — `TerminalCallbackRegistry.emit._run_one`; K3.1.
- **3.6** — `SchedulerEngine._restart_tick_loop`; K3.1.
- **3.7** — `SchedulerEngine._sleep_between_ticks`; K3.1.
- **3.8** — `_capture_ps_snapshot`; K3.1.
- **3.9** — `list_phantom_sessions`; K3.1.
- **3.10** — `_code_identity_report`; K3.1.
- **3.11** — `transition_sessions`; K3.1.
- **3.12** — `health_report`; K3.1.

## Factories, task types, cancellation and context references

These 179 records include the checkpoint lock `default_factory` and type/context/loop references; they are not extra original issue calls.

**Contracts**

- **K4.1** — Keep native ContextVar/Context/`copy_context` construction and context-token/thread/task provenance. Existing ln worker helpers copy context only for offload; they are not replacements for this state container.
- **K4.2** — Retain native exception/type/annotation or context ownership at this exact consumer. Replacing it changes concrete class, catch/subclass behavior, task identity or context-token scope; this is reference accounting, not an extra call count.
- **K4.3** — CheckpointWriter uses asyncio.Lock as a dataclass `default_factory`. Preserve native uncontended atomic ordering and concrete lock behavior; do not silently omit the thirteenth B1 factory just because it is not ast.Call.
- **K4.4** — Keep this native loop-owned future/task/callback/executor method and its handle/thread-affinity semantics. No ln wrapper exposes the same control object; this is not a clock-read substitution.

**Operations**

- **4.1** — `ADDITIONAL_NATIVE_METHOD_OR_CONTEXT_CALL`: contextvars.ContextVar; K4.1.
- **4.2** — REFERENCE: contextvars.ContextVar; K4.1.
- **4.3** — REFERENCE: asyncio.TimeoutError; K4.2.
- **4.4** — REFERENCE: asyncio.CancelledError; K4.2.
- **4.5** — REFERENCE: asyncio.Event; K4.2.
- **4.6** — REFERENCE: asyncio.Lock; K4.3.
- **4.7** — REFERENCE: asyncio.Task; K4.2.
- **4.8** — REFERENCE: asyncio.Future; K4.2.
- **4.9** — `ADDITIONAL_NATIVE_METHOD_OR_CONTEXT_CALL`: `loop.create_task`; K4.4.
- **4.10** — REFERENCE: threading.Lock; K4.2.
- **4.11** — REFERENCE: threading.RLock; K4.2.
- **4.12** — REFERENCE: asyncio.QueueFull; K4.2.
- **4.13** — REFERENCE: asyncio.Queue; K4.2.
- **4.14** — `ADDITIONAL_NATIVE_METHOD_OR_CONTEXT_CALL`: `asyncio.get_running_loop`().`create_task`; K4.4.
- **4.15** — REFERENCE: asyncio.Lock; K4.2.
- **4.16** — REFERENCE: asyncio.Semaphore; K4.2.

## Native primitive API consumers

**Contracts**

- **K5.1** — Existing Event.set consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.2** — Existing Queue.put consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.3** — Existing `Queue.put_nowait` consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.4** — Existing Queue.qsize consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.5** — Existing Queue.get consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.6** — Existing Queue.empty consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.7** — Existing `Queue.get_nowait` consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.8** — Existing Event.wait consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.9** — Existing Lock.locked consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.10** — Existing Semaphore.locked consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.11** — Existing Semaphore.acquire consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.12** — Existing Semaphore.release consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.13** — Existing Lock.release consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.
- **K5.14** — Existing Lock.acquire consumption stays coupled to this native factory, preserving return/exception/checkpoint and synchronous-precheck ordering.

**Operations**

- **5.1** — .set; K5.1.
- **5.2** — .put; K5.2.
- **5.3** — .`put_nowait`; K5.3.
- **5.4** — .qsize; K5.4.
- **5.5** — .get; K5.5.
- **5.6** — .empty; K5.6.
- **5.7** — .`get_nowait`; K5.7.
- **5.8** — .wait; K5.8.
- **5.9** — queue.put( OperatorEngineEvent( "`tool_call`", { "callId": getattr(chunk, "`tool_id`", None) or "", "tool": `tool_name`, # A native tool call's own arguments are the # model's choice, not something this bridge # validated -- they can carry the same secrets or # host paths a tool's output can. "arguments": `redact_arguments`(getattr(chunk, "`tool_input`", None) or {}), "mode": mode, }, ) ); K5.2.
- **5.10** — queue.put( OperatorEngineEvent( "`tool_result`", { "callId": getattr(chunk, "`tool_id`", None) or "", "ok": not bool(getattr(chunk, "`is_error`", False)), # Native output may contain secrets or unbounded logs. "result": {"nativeToolCompleted": True}, }, ) ); K5.2.
- **5.11** — .locked; K5.9.
- **5.12** — .locked; K5.10.
- **5.13** — .acquire; K5.11.
- **5.14** — .release; K5.12.
- **5.15** — .release; K5.13.
- **5.16** — .acquire; K5.14.

## Additional existing shared-helper use

These current shared calls are an inventory distinct from historical migration counts. Each retains its existing enclosing consumer contract. A last-changing line commit is not necessarily its first migration.

**Contracts**

- **K6.1** — Already shared; preserve exact arguments, backend, admission, result and error policy.

**Operations**

- **6.1** — `lionagi.ln.concurrency.is_coro_func`; K6.1.
- **6.2** — `lionagi.ln.concurrency.maybe_await`; K6.1.
- **6.3** — `lionagi.ln.concurrency.run_sync`; K6.1.
- **6.4** — lionagi.ln.concurrency.sleep; K6.1.
- **6.5** — `lionagi.ln.concurrency.fail_after`; K6.1.
- **6.6** — `lionagi.ln.concurrency.run_async`; K6.1.
- **6.7** — lionagi.ln.concurrency.CancelScope; K6.1.
- **6.8** — lionagi.ln.concurrency.Lock; K6.1.
- **6.9** — `lionagi.ln.concurrency.create_task_group`; K6.1.
- **6.10** — `lionagi.ln.concurrency.current_time`; K6.1.
- **6.11** — lionagi.ln.concurrency.Semaphore; K6.1.
- **6.12** — lionagi.ln.concurrency.gather; K6.1.
- **6.13** — lionagi.ln.gather; K6.1.
- **6.14** — lionagi.ln.concurrency.CapacityLimiter; K6.1.
- **6.15** — `lionagi.ln.is_coro_func`; K6.1.
- **6.16** — `lionagi.ln.concurrency.utils.maybe_await`; K6.1.
- **6.17** — `lionagi.ln.fail_after`; K6.1.

## Baseline address index

Entries use `contract=lines`, or `line:column` for JSON calls. `U:` identifies unit rows; `×N` preserves occurrences. Ranges cover original spans only. All positions use the baseline.

- `lionagi/agent/context_fold.py`: `4.1=201; 4.2=201`.
- `lionagi/agent/factory.py`: `6.1=479,480,496,497`.
- `lionagi/agent/gate.py`: `6.2=85,119`.
- `lionagi/agent/hooks.py`: `6.3=169`.
- `lionagi/agent/settings.py`: `4.3=256,279`.
- `lionagi/cli/_runs.py`: `2.1=589; 6.4=877; U:A2=877`.
- `lionagi/cli/_util.py`: `2.2=36`.
- `lionagi/cli/agent.py`: `2.3=1891; 4.4=2010; 6.4=1875; 6.5=499; 6.6=2559; 6.7=536,1836,2015; U:A6=536,1836,2015`.
- `lionagi/cli/dispatch.py`: `6.6=318,320,322,324,327,331,409,420,608,619,648`.
- `lionagi/cli/engine.py`: `6.6=225`.
- `lionagi/cli/invoke.py`: `6.6=260,279,308,375`.
- `lionagi/cli/kill.py`: `6.6=1348,1366`.
- `lionagi/cli/machine.py`: `6.6=659`.
- `lionagi/cli/machine_schedule.py`: `2.4=401; U:B7=401`.
- `lionagi/cli/mirror.py`: `2.5=925,931; 4.3=926,932; 4.5=857; 6.4=1005; 6.6=1016; U:A2=1005; U:B8=925,931`.
- `lionagi/cli/monitor.py`: `2.6=1030; 3.1=1030; 6.4=1353; 6.6=1153,1155,1594,1938,1946,2056`.
- `lionagi/cli/orchestrate/__init__.py`: `6.6=942`.
- `lionagi/cli/orchestrate/_checkpoint.py`: `4.6=54; U:B1=54`.
- `lionagi/cli/orchestrate/_control.py`: `2.5=269,365; 4.3=273,374; 6.6=280,364; U:B8=269,365`.
- `lionagi/cli/orchestrate/_notify.py`: `2.7=276; 3.2=276; 6.1=270; 6.2=271,275; U:A9=276`.
- `lionagi/cli/orchestrate/_orchestration.py`: `6.4=1889; 6.8=2133`.
- `lionagi/cli/orchestrate/fanout.py`: `6.7=323; 6.9=532`.
- `lionagi/cli/orchestrate/flow.py`: `2.10=1724,1741; 2.3=322,1722,1820,1839,1896,1956,2035,2185,2428,2429,2431; 2.8=290,324,1601,1609,1613,2477,2482; 2.9=1695; 4.4=1725,1740,2456,2479,2483,2488,2489,2870; 4.7=272; 6.10=1728; 6.4=1988,2161,2194; 6.7=1738,2471,3332; U:B5=1695`.
- `lionagi/cli/state.py`: `6.6=1627,1637,1646,1655,1659,1664,1669,1684,1711,1734,1746,1820,1830`.
- `lionagi/cli/stats.py`: `6.6=200,265`.
- `lionagi/cli/status.py`: `2.5=662,742; 4.3=666,743; 6.6=677,742; U:B8=662,742`.
- `lionagi/cli/wait.py`: `6.4=216; 6.6=269`.
- `lionagi/dispatch/outbox.py`: `4.3=148`.
- `lionagi/engines/coding.py`: `2.3=953; 2.5=910,972; 4.3=914,974; 4.4=659; 4.7=920; 6.3=682,719,993,1010,1029,1052,1067; 6.4=937; U:B8=910,972`.
- `lionagi/engines/engine.py`: `2.10=1692,1817,1825; 2.11=356,525,1773; 2.12=940; 2.3=779,949,969,1689,1748,1759,1815; 2.5=1691; 2.8=973; 4.1=341; 4.2=341; 4.3=1703; 4.4=1114,1695,1700,1707,1770,1804,1818,1826; 4.7=472,494,772,787,1746; 6.11=471; 6.12=1101; 6.4=1090,1105; U:B5=940; U:B8=1691`.
- `lionagi/engines/flow_signals.py`: `2.3=133; 4.8=61; 6.12=154`.
- `lionagi/engines/hypothesis.py`: `2.11=624,930; 4.1=578; 4.2=578`.
- `lionagi/engines/review.py`: `6.13=583; 6.5=197; U:A7=197`.
- `lionagi/hooks/_message_retry.py`: `2.13=61; U:B1=61`.
- `lionagi/hooks/bus.py`: `4.1=24; 4.2=24; 6.2=152,189`.
- `lionagi/hooks/external.py`: `2.12=525; 2.14=560,561; 2.5=579,601; 4.3=583; U:B5=525; U:B8=579,601`.
- `lionagi/integrations/ruvector/memory.py`: `2.15=108,138; U:B6=108,138`.
- `lionagi/integrations/ruvector/store.py`: `2.15=312,321,324; 2.16=91; U:B6=312,321,324`.
- `lionagi/mcp/jobs.py`: `6.10=3066,3097; 6.4=3104; U:A2=3104; U:A4=3066,3097`.
- `lionagi/operations/_observe.py`: `6.12=100`.
- `lionagi/operations/chat/_prepare.py`: `4.1=32; 4.2=32`.
- `lionagi/operations/fields.py`: `2.2=415`.
- `lionagi/operations/flow.py`: `2.17=420; 2.18=1223; 2.19=1235; 2.2=1143; 2.3=1270; 4.1=71; 4.2=71; 4.7=227; 4.9=426; 6.14=254,1157,1222; 6.7=1234; 6.9=260,1167,1245; U:A6=1234`.
- `lionagi/operations/run/run.py`: `2.20=62,798; 2.21=63,77; 6.10=95,207,221,288,574; 6.15=526; 6.3=67; 6.5=98,225,303; U:A4=95,207,221,288,574; U:A7=98,225,303`.
- `lionagi/plugins/registry.py`: `2.2=434; 4.10=434`.
- `lionagi/protocols/action/function_calling.py`: `6.1=92,129,135`.
- `lionagi/protocols/action/tool_hooks.py`: `6.2=121,191`.
- `lionagi/protocols/generic/event.py`: `2.22=237; 4.5=221,235; 5.1=239,267; U:B2=237`.
- `lionagi/protocols/generic/flow.py`: `2.16=136; 4.11=41`.
- `lionagi/protocols/generic/log.py`: `6.3=311`.
- `lionagi/protocols/generic/pile.py`: `2.16=529,545; 4.11=220; 6.3=1019; 6.4=565,866; 6.8=530,546`.
- `lionagi/protocols/generic/processor.py`: `2.23=45; 4.12=77; 5.2=71; 5.3=75; 5.4=84,122,162,164; 5.5=87; 5.6=111,161,164; 6.11=50; 6.4=165,181; 6.9=110; U:A2=165,181; U:B4=45`.
- `lionagi/protocols/graph/graph.py`: `4.11=36`.
- `lionagi/protocols/messages/manager.py`: `6.1=138,434`.
- `lionagi/providers/_cli_subprocess.py`: `2.10=910,1167,1171,1209; 2.14=888; 2.24=1033,1065,1073,1090,1096; 2.3=908; 2.5=901,1105,1171,1208; 2.8=1076,1078,1098; 4.1=109; 4.2=107; 4.3=1106,1172; 4.4=1087,1174,1211,1234; 4.7=1063,1091; 4.8=344,915; 6.16=981; U:B8=901,1105,1171,1208`.
- `lionagi/providers/_secret_resolution.py`: `2.12=278; 2.14=228,327; 2.5=240,339; 4.3=243,340; 6.7=251,346,351; U:B5=278; U:B8=240,339`.
- `lionagi/providers/_subagent_admission.py`: `2.25=526; 2.26=754; 2.27=770; 3.3=754,770; 3.4=526; 4.1=254; 4.2=254`.
- `lionagi/providers/ag2/agent.py`: `2.23=199; 2.24=233; 2.5=238; 4.13=199; 4.3=240; 4.4=276; 5.2=203,216; 5.5=238; 5.6=243; 5.7=244; U:B4=199; U:B8=238`.
- `lionagi/providers/ag2/groupchat.py`: `6.16=300,309,315,318,322,329`.
- `lionagi/providers/anthropic/claude_code.py`: `6.16=795,854,882,894,905,926,952,1014`.
- `lionagi/providers/anthropic/messages.py`: `4.3=647`.
- `lionagi/providers/google/gemini_code.py`: `6.10=587,719; 6.16=682,722; U:A5=587,719`.
- `lionagi/providers/ollama/chat.py`: `6.3=58`.
- `lionagi/providers/openai/codex.py`: `6.10=3003,3474; 6.16=3055,3075,3100,3116,3139,3164,3187,3352,3368,3387,3477; U:A5=3003,3474`.
- `lionagi/providers/pi/cli.py`: `6.10=441,595; 6.16=482,523,557,598; U:A5=441,595`.
- `lionagi/service/_api_telemetry.py`: `4.1=55,58; 4.2=55,58`.
- `lionagi/service/accounts/_ledger.py`: `6.3=188`.
- `lionagi/service/accounts/_router.py`: `2.2=56; 6.7=127,172,188`.
- `lionagi/service/accounts/_scope.py`: `4.1=13; 4.2=13`.
- `lionagi/service/broadcaster.py`: `2.2=25,38; 2.28=90; 4.10=25; U:B9=90`.
- `lionagi/service/connections/api_calling.py`: `4.1=29; 4.2=29`.
- `lionagi/service/connections/endpoint.py`: `4.3=925`.
- `lionagi/service/connections/mcp_wrapper.py`: `2.10=1911,1925; 2.2=1565; 2.24=1908,1918; 4.10=1565; 4.4=1926,1941,1945; 4.7=1935; 6.7=1922,1936; 6.8=1651,1661`.
- `lionagi/service/connections/registry.py`: `2.16=128; 2.29=129; 4.11=128`.
- `lionagi/service/hooks/_utils.py`: `6.15=27; 6.3=30`.
- `lionagi/service/hooks/hook_event.py`: `6.10=36,77; 6.5=40; U:A4=36,77`.
- `lionagi/service/hooks/hooked_event.py`: `2.10=253,292; 2.17=251; 4.1=60; 4.14=251; 4.2=59; 4.7=279; 6.7=224,289; U:A6=224,289`.
- `lionagi/service/imodel.py`: `2.5=723; 4.3=727; 6.15=472; U:B8=723`.
- `lionagi/service/manager.py`: `2.5=51; 4.3=52; 6.12=68; U:B8=51`.
- `lionagi/service/rate_limited_processor.py`: `2.24=105; 4.7=43; 6.4=52; 6.8=44; U:A2=52`.
- `lionagi/service/resilience.py`: `4.3=40; 6.8=98`.
- `lionagi/session/_lifecycle_ctx.py`: `4.1=10; 4.2=10`.
- `lionagi/session/branch.py`: `2.17=457; 4.1=119; 4.2=118; 4.9=460; 6.12=469; 6.2=374`.
- `lionagi/session/exchange.py`: `4.11=38; 6.12=114; 6.4=262`.
- `lionagi/session/observer.py`: `6.12=393; 6.2=368`.
- `lionagi/state/compat.py`: `4.1=1904; 4.2=1904`.
- `lionagi/state/db.py`: `2.17=981,8074; 2.2=856,8049,8261; 4.1=781,792; 4.2=781,792; 6.3=1033,1081; 6.7=972,1036,1100,1257; 6.8=852,877,7077,8071`.
- `lionagi/state/engine.py`: `2.30=200`.
- `lionagi/state/lifecycle/callbacks.py`: `2.31=189; 3.5=189; 6.1=184; 6.2=185,188; 6.9=206; U:A9=189`.
- `lionagi/state/lifecycle/notify_settings.py`: `2.14=484; 2.5=436,496; 4.3=499; 6.7=526; U:B8=436,496`.
- `lionagi/studio/app.py`: `2.15=364,399; 2.22=256; 2.24=257,392; 2.5=293; 4.3=294; 4.4=296,341; 4.5=225,287; 4.7=225,269,287,332; 5.1=291; U:B2=256; U:B6=364,399; U:B8=293`.
- `lionagi/studio/cli.py`: `2.1=304,1013; 2.2=994; 2.32=993; 2.4=1867,1985,2359; U:B7=1867,1985,2359`.
- `lionagi/studio/daemon_log.py`: `2.2=41`.
- `lionagi/studio/launch_access.py`: `2.2=75`.
- `lionagi/studio/operator/application_mcp.py`: `2.15=1361; 2.4=1375; 6.3=1036,1058,1144,1195; 6.4=1245; U:B6=1361; U:B7=1375`.
- `lionagi/studio/operator/cancel_run.py`: `6.4=428`.
- `lionagi/studio/operator/coordinator.py`: `2.15=557,908; 2.22=377; 2.24=378; 2.3=556,651,822,844,875,907; 2.8=571,813,829,853,886,896,927; 2.9=302; 4.4=673,814,830,854,887,895,928; 4.5=401; 4.7=234,383,412; 4.8=413; 5.1=384,621,939; 5.8=398; 6.4=65,211,478; U:B2=377; U:B5=302; U:B6=557,908`.
- `lionagi/studio/operator/engine.py`: `2.15=360; 2.23=434; 2.24=532; 4.13=434; 4.4=544; 5.10=494; 5.2=444,451,521,528,530; 5.5=535; 5.9=478; U:B4=434; U:B6=360`.
- `lionagi/studio/operator/permission_mcp.py`: `2.15=200; 2.4=215; 6.4=114; U:B6=200; U:B7=215`.
- `lionagi/studio/operator/rename_session.py`: `6.4=180`.
- `lionagi/studio/operator/resume_run.py`: `6.4=241`.
- `lionagi/studio/operator/run_control.py`: `6.4=386`.
- `lionagi/studio/operator/store.py`: `2.13=217; U:B1=217`.
- `lionagi/studio/reconnect.py`: `2.1=199; 2.32=183`.
- `lionagi/studio/scheduler/engine.py`: `2.10=1048; 2.11=1695; 2.13=451,478,484,488,492; 2.24=542,565,872,1110,1682,1732; 2.3=1045; 2.33=569; 2.34=998; 2.8=1062; 2.9=856,865; 3.6=569; 3.7=998; 4.4=568,838,843,850,1000,1024,1050,1061,1084,3218; 4.7=440,443,452,456,476,541,546,870,1687; 5.11=614,617; 6.10=1059,1060; U:A3=569,998; U:A5=1059,1060; U:B1=451,478,484,488,492; U:B5=856,865`.
- `lionagi/studio/scheduler/github.py`: `2.14=244`.
- `lionagi/studio/scheduler/signals.py`: `4.4=44; 6.12=224; 6.2=219; U:A8=219`.
- `lionagi/studio/scheduler/subprocess.py`: `2.14=892; 2.35=909; 2.5=929; 4.3=930; 4.4=937,957,964; U:B5=909; U:B8=929`.
- `lionagi/studio/services/admin.py`: `2.10=392; 2.17=373; 2.2=280,285; 2.24=386; 2.36=333; 2.37=872; 2.38=923; 2.39=1027; 2.40=1210; 2.41=1241; 3.10=923; 3.11=1210,1241; 3.12=1027; 3.8=333; 3.9=872; 4.7=272; 6.7=165,195; U:A1=333,872,923,1027,1210,1241`.
- `lionagi/studio/services/agents.py`: `6.3=388,396,407,420,433`.
- `lionagi/studio/services/definitions.py`: `2.13=37,43; 4.15=36,40; 6.3=178,244,248,291,295,439,449,469,522,593,597; U:B1=37,43`.
- `lionagi/studio/services/launches.py`: `2.24=226; 2.42=52; 2.9=259; 4.16=37,48; 4.4=299; 4.7=34; 5.12=202; 5.13=207; 5.14=231,234; U:B3=52; U:B5=259`.
- `lionagi/studio/services/leo.py`: `2.13=44; 5.11=56,67,390; 5.15=377; 5.16=397; 6.3=199,207; U:B1=44`.
- `lionagi/studio/services/mcp_servers.py`: `2.16=97; 6.3=599,605,621,636,646,654,662; 6.5=331; U:A7=331`.
- `lionagi/studio/services/operator.py`: `6.10=286,300,302; 6.4=306; U:A5=286,300,302`.
- `lionagi/studio/services/playbooks.py`: `6.3=337,343,354,367,403,414,428`.
- `lionagi/studio/services/plugins.py`: `6.3=352,358,366,377`.
- `lionagi/studio/services/run_resume.py`: `2.13=103; 2.15=192,216; U:B1=103; U:B6=192,216`.
- `lionagi/studio/services/run_resume_worker.py`: `2.14=112; 2.4=122; 6.4=74; U:B7=122`.
- `lionagi/studio/services/scheduler_state.py`: `2.13=136; U:B1=136`.
- `lionagi/studio/services/sessions.py`: `6.4=1802,1863`.
- `lionagi/studio/services/shows.py`: `6.4=800`.
- `lionagi/studio/services/skills.py`: `6.3=144,150,158`.
- `lionagi/studio/services/workflow_run.py`: `4.4=194`.
- `lionagi/testing/_endpoint.py`: `2.43=320; 6.4=115,143`.
- `lionagi/testing/helpers.py`: `2.44=80,84,86,90; 4.7=65; 6.10=34,35; 6.13=70; 6.17=61,69; 6.4=38,83`.
- `lionagi/tools/_subprocess.py`: `2.1=93,94`.
- `lionagi/tools/code/ast_search.py`: `6.3=177`.
- `lionagi/tools/code/bash.py`: `6.3=120`.
- `lionagi/tools/code/check.py`: `6.3=255`.
- `lionagi/tools/code/nav.py`: `6.3=192,196,200`.
- `lionagi/tools/code/search.py`: `6.3=222,231`.
- `lionagi/tools/coding.py`: `6.3=482,486,513,524,567,595,617,848,858,866,874,894`.
- `lionagi/tools/context/context.py`: `4.1=24; 4.2=24`.
- `lionagi/tools/file/editor.py`: `6.3=214,228`.
- `lionagi/tools/file/reader.py`: `6.3=200,209`.
- `lionagi/tools/khive_injection.py`: `4.1=380; 4.2=380`.
- `lionagi/tools/sandbox.py`: `6.3=268,273,278,284,289`.
- `lionagi/tools/sandbox_backend.py`: `6.3=242`.
