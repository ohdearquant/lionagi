# Clocks, timestamps and identifiers

Use the shared clock, identifier and coercion helpers where their admission and representation match the caller. Native clocks and formatters remain at boundaries that need a different time domain, precision or error surface.

## Operation boundaries

| Operation | Behavior |
| --- | --- |
| UTC wall-clock formatting | `now_utc` supplies UTC wall time; each caller retains its ISO or strftime representation. |
| Epoch conversion adapters | Compatible epoch adapters use the shared coercer; epoch-only properties and formatters retain bool/string admission, local-zone interpretation and native errors. |
| Session role labels | Role labels use gmtime truncation before HH:MM formatting, preserving fractional-second boundaries and None/name/id omission. |
| Polling clock and cadence | The shared AnyIO clock and sleep retain polling cadence, remaining-budget clipping, cancellation and run-liveness reads. |
| Short identifiers and path suffixes | `short_id` serves bounded identifiers. Path-builder suffixes retain Python slicing, including bool and lengths above 32. |
| UUID and structural ID admission | `protocols.to_uuid` and structural `ID.get_id` preserve existing admission, normalization, lookup and type errors. |
| Full-width generated identifiers | Full generated identities use `uuid4` and their existing full-width string representation. |
| Paired monotonic durations | Native paired monotonic reads retain one clock domain for starts, deadlines and elapsed output, including synchronous abandoned-terminal handling. |
| Async loop clock | `current_time` supplies the active backend loop clock at async elapsed-time consumers. |
| Eventually polling | `assert_eventually` uses the shared elapsed clock and retains its observation cadence. |
| Element timestamp validation | Element keeps float precision/non-finite handling, local-naive datetime interpretation, numeric/string admission and its exact diagnostics. |
| Public timestamp coercion | `coerce_created_at` rejects bool, preserves aware datetime identity, normalizes naive UTC and ISO suffix/fractions, and tries numeric strings before ISO. |
| Timestamp display adapters | Timestamp display adapters use the shared coercer while preserving their own catches and fallback values. |
| Scheduler log timestamps | Scheduler log adapters preserve the existing conversion and recompute error boundary. |
| Duration adapters | Duration adapters preserve distinct flags, errors and return shapes; stats has no separate parser. |
| Durable JSON codecs | Durable native JSON and SQLAlchemy bindings retain UUID-only defaults, spacing/ASCII behavior and allow_nan=False refusal, including read-only engines. |
| Team inbox message shape | Team messages retain `from_op` even when None and omit kind; their identifiers use the shared helper. |
| Short-id validation | `short_id` accepts integer lengths 1..32 and rejects bool with its existing public validation. |
| Duration validation | `parse_duration` retains positive finite admission, caller flags, maximum and errors. |

## Shared helpers

`now_utc`, `coerce_created_at`, `short_id`, `parse_duration` and the canonical protocol ID helpers serve matching callers. Polling uses `ln.concurrency.current_time` and `sleep`; paired native monotonic measurements keep their own clock domain.

## Behavioral reference

| Operation | Example consumer | Behavior |
| --- | --- | --- |
| UTC wall-clock formatting | `lionagi/cli/_code_identity.py` — `_now`; `lionhub/src/lionhub/serve/mcp/dispatch.py` — `module initialization` | UTC wall-clock calls share `now_utc`; each caller retains its ISO/strftime output. |
| Epoch conversion adapters | `lionagi/cli/_providers.py` — `build_deadline_preamble`; `lionagi/cli/orchestrate/flow.py` — `_format_budget_preamble` | Epoch adapters use the shared coercer where its input and error contracts match; broader epoch-only interfaces retain native conversion. |
| Epoch conversion adapters | `lionagi/cli/stats.py` — `_iso` | `_iso` only guards None. Native fromtimestamp keeps epoch-only admission (including bool) and native TypeError for strings; the shared coercer rejects bool and accepts strings/datetime. The float annotation does not add a runtime admission check. |
| Epoch conversion adapters | `lionagi/protocols/generic/element.py` — `Element.created_datetime` | `created_datetime` uses epoch-only conversion for validated floats and for values supplied through `model_construct` or other validation bypasses. The shared coercer accepts strings/datetime and rejects bool differently, so this property retains native admission and errors. |
| Epoch conversion adapters | `lionagi/studio/cli.py` — `_fmt_rfc3339` | `_fmt_rfc3339` only guards None, then epoch→UTC→local zone→seconds text. The shared coercer has different bool/string admission and exceptions. This formatter retains local astimezone formatting and epoch-only admission. |
| Epoch conversion adapters | `lionagi/studio/services/schedule_export.py` — `_convert_legacy_trigger` | The at-trigger row uses a truthiness guard, then native epoch conversion. True is an epoch1 here; the shared coercer rejects it and accepts string epochs. Native conversion retains the malformed-row errors and exported fire-time spelling. |
| Session role labels | `lionagi/state/session_naming.py` — `agent_role_label` | gmtime truncates fractional seconds before HH:MM; datetime.fromtimestamp can round across the minute. Preserve deterministic role labels and None/name/id omission. |
| Polling clock and cadence | `lionhub/src/lionhub/serve/mcp/jobs.py` — `wait`; `lionagi/operations/run/run.py` — `_stream_with_deadline` | The shared AnyIO clock and companion sleep retain the wait cadence and run-liveness reads. |
| Polling clock and cadence | `lionhub/src/lionhub/serve/mcp/jobs.py` — `wait` | The companion polling sleep delegates to ln.concurrency.sleep; `eff_poll`/remaining clipping and cancellation stay at the original wait seam. |
| Short identifiers and path suffixes | `lionagi/cli/_runs.py` — `_new_run_id`; `lionagi/cli/invoke.py` — `_start_invocation` | Bounded identifiers share their existing lengths and defaults. The owning path-builder suffix keeps permissive bool/overlength slicing. |
| Short identifiers and path suffixes | `lionagi/ln/_utils.py` — `_build_safe_path` | The existing `random_hash_digits`>0 gate and Python slicing accept bool and lengths>32; `short_id` explicitly rejects them. This public path-builder retains its permissive suffix behavior, distinct from the bounded logger input. |
| UUID and structural ID admission | `lionagi/cli/orchestrate/flow.py` — `_reconstruct_spawned_nodes`; `lionagi/cli/orchestrate/flow.py` — `_execute_dag._checkpoint_record` | Canonical `protocols.to_uuid`/`ID.get_id` retain Observable normalization and lookup/type/error contracts. UUID(str(...)) and deprecated `ln.to_uuid` have different admission contracts. |
| Full-width generated identifiers | `lionagi/cli/_runs.py` — `setup_agent_persist` | Generated run identities use uuid4 with full-width str(uuid4()) formatting. |
| Paired monotonic durations | `lionagi/cli/orchestrate/fanout.py` — `_run_fanout_inner` | Decompose/fanout/synthesis/total timings share native starts and elapsed reads. Preserve progress/timing outputs and synchronous time access. |
| Paired monotonic durations | `lionagi/cli/orchestrate/flow.py` — `_execute_dag`; `lionagi/cli/orchestrate/flow.py` — `_synthesize` | Execution/synthesis/planning/total phase durations share native monotonic starts and reads. This monotonic domain retains CLI progress and timing data. |
| Paired monotonic durations | `lionagi/operations/flow.py` — `DependencyAwareExecutor._emit_abandoned_terminal`; `lionagi/operations/flow.py` — `DependencyAwareExecutor._execute_operation` | The paired clock feeds operation elapsed output and the synchronous abandoned-terminal fallback. Preserve the same monotonic domain for start/read/default and eligibility without active AnyIO context. |
| Paired monotonic durations | `lionagi/operations/run/run.py` — `run` | Run-start and both terminal duration paths share monotonic scalars. The paired native clock domain retains backend/context availability and clock-patching/telemetry behavior. |
| Paired monotonic durations | `lionagi/session/branch.py` — `Branch._observed_run`; `lionagi/session/branch.py` — `Branch.ReAct` | Observed-run/ReAct start/finally duration signals use the same monotonic domain, preserving emitted duration values and the existing clock boundary. |
| Paired monotonic durations | `lionagi/studio/scheduler/engine.py` — `SchedulerEngine._sleep_between_ticks` | Both reads conserve one tick deadline across stray cancellation. Native monotonic deadline arithmetic and shared sleep remain separate operations. |
| Paired monotonic durations | `lionagi/studio/services/sessions.py` — `stream_session_route.generate`; `lionagi/studio/services/sessions.py` — `stream_signals.generate` | Stream heartbeat initialization/activity/read/reset calls share one native monotonic domain. Preserve the five-second emission cadence across generator resumption and existing clock-patch behavior. |
| Async loop clock | `lionagi/providers/google/gemini_code.py` — `stream_gemini_cli`; `lionagi/providers/openai/codex.py` — `stream_codex_cli` | Loop-clock calls delegate `current_time`; the backend clock behavior is unchanged. |
| Eventually polling | `lionagi/testing/helpers.py` — `AsyncTestHelpers.assert_eventually` | `assert_eventually` uses the shared elapsed clock. |
| Element timestamp validation | `lionagi/protocols/generic/element.py` — `Element._coerce_created_at` | Element preserves float pass-through (including precision/nonfinite), local-naive datetime.timestamp, ISO-first strings, numeric fallback including Decimal/bool, and exact Invalid* errors. The shared UTC helper has different admission/order/error contracts, so Element retains its own validator. |
| Public timestamp coercion | `lionagi/ln/_utils.py` — `coerce_created_at` | Current helper rejects bool, preserves aware datetime identity, normalizes naive UTC/trailingZ/z/Python3.10 fractions and keeps numeric-before-ISO strings. These are the public admission and normalization contracts. |
| Timestamp display adapters | `lionagi/cli/state.py` — `_derive_timestamps`; `lionagi/cli/state.py` — `_import_teams` | Timestamp adapters delegate with their own fallback/catch behavior. |
| Scheduler log timestamps | `lionagi/studio/scheduler/engine.py` — `SchedulerEngine.recompute_next_fire` | Scheduler log conversions delegate; preserve current recompute error boundary. |
| Duration adapters | `lionagi/cli/mirror.py` — `_parse_window`; `lionagi/cli/monitor.py` — `_since_timestamp` | Duration adapters use the shared parser with their own flags, returns and errors; stats has no independent parser. |
| Durable JSON codecs | `lionagi/state/engine.py` — `_dumps_with_uuid` | Native json.dumps(default=`_json_serializer`,`allow_nan`=False) is the actual current durable codec. The codec rejects non-finite values and retains native output bytes. |
| Durable JSON codecs | `lionagi/state/engine.py` — `make_engine`; `lionagi/state/engine.py` — `make_compatibility_engine` | All engine JSON binds retain `_dumps_with_uuid`: native spacing/ASCII/key coercion, UUID-only custom default and `allow_nan`=False durable refusal. `ln.json_dumps` supports a broader value domain and emits different bytes, so the engine retains this codec policy. |
| Team inbox message shape | `lionagi/cli/orchestrate/_common.py` — `_post_results_to_team` | Inline result messages intentionally retain `from_op` even when None and omit kind. `_build_message` adds kind and omits falsy `from_op`. These inbox messages retain that persisted shape while their identifiers use the shared helper. |
| Short-id validation | `lionagi/ln/_utils.py` — `short_id` | `short_id` accepts integer lengths 1..32 and rejects bool; its public export and errors are unchanged. |
| Duration validation | `lionagi/ln/_utils.py` — `parse_duration` | `parse_duration` retains positive finite admission, caller flags, maximum, errors and its public export. |
| time.time | `lionagi/agent/context_fold.py` — `ContextFold._clip_once`; `lionagi/agent/context_fold.py` — `ContextFold._fold_once` | Preserve the native floating-point epoch value and its precision at this consumer; a UTC datetime round-trip changes representation/admission and can round sub-microsecond values. |
| uuid.uuid4 | `lionagi/agent/factory.py` — `_write_codex_mcp_secret_profile`; `lionagi/cli/_runs.py` — `setup_agent_persist` | Preserve the full UUID object or unsliced full-width spelling consumed here; `short_id` returns a bounded string and is not the same type/width contract. |
| time.monotonic | `lionagi/cli/_code_identity.py` — `_Budget.__init__`; `lionagi/cli/_code_identity.py` — `_Budget.remaining` | Preserve the native monotonic clock domain, paired starts/deadlines and operation outside an active AnyIO context; replacing the clock also changes caller patching and scheduling assumptions. |
| time.strftime | `lionagi/cli/dispatch.py` — `_format_time`; `lionagi/cli/monitor.py` — `_detail_session` | Preserve the explicit format, supplied `struct_time` and local/platform formatting semantics; `now_utc` is a wall-clock datetime constructor, not this formatter. |
| time.localtime | `lionagi/cli/dispatch.py` — `_format_time`; `lionagi/cli/monitor.py` — `_detail_session` | Preserve host-local timezone/DST `struct_time` semantics; UTC datetime coercion changes the value and formatting contract. |
| time.sleep | `lionagi/cli/kill.py` — `_terminate_pid`; `lionagi/cli/monitor.py` — `_watch_loop` | Preserve synchronous blocking delay and native value/error behavior; an async sleep returns an awaitable and requires a different caller contract. |
| datetime.datetime.fromtimestamp | `lionagi/cli/machine_schedule.py` — `_resolved_next_fire`; `lionagi/cli/stats.py` — `_iso` | Preserve this epoch-only conversion, explicit timezone/local interpretation and native admission/errors rather than broadening through a multi-type coercer. |
| datetime.datetime.fromisoformat | `lionhub/src/lionhub/serve/mcp/jobs.py` — `_unresolved_spawn_age`; `lionagi/protocols/generic/element.py` — `Element._coerce_created_at` | Preserve strict ISO parsing, native errors and timezone/naive interpretation at this consumer; general coercion also accepts numeric strings and other input types. |
| uuid.UUID | `lionagi/protocols/generic/element.py` — `Element._ensure_uuid`; `lionagi/protocols/generic/element.py` — `_observable_uuid` | Preserve this UUID constructor form, argument/keyword admission and native errors; structural UUID helpers accept a different set of objects. |
| uuid.uuid5 | `lionagi/state/claude_mirror.py` — `_det`; `lionagi/state/codex_mirror.py` — `_det` | Preserve deterministic namespace/name UUID identity; a random short identifier is a different value and representation. |
| time.gmtime | `lionagi/state/session_naming.py` — `agent_role_label` | Preserve UTC `struct_time` conversion and fractional-second truncation used by this formatter. |
| `time.perf_counter` | `lionagi/studio/services/admin.py` — `store_probe`; `lionagi/studio/services/admin.py` — `_capture_ps_snapshot` | Preserve the paired native high-resolution elapsed counter and synchronous availability; a backend loop clock changes the domain/context and instrumentation boundary. |
| datetime.datetime.strptime | `lionagi/studio/services/schedules.py` — `_svc_validate_github_cursor` | Preserve the supplied format grammar, structuring/timezone interpretation and native refusal surface; general timestamp coercion is broader. |
