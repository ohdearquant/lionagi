# Clocks, timestamps and identifiers

Issue #3581 baseline: `ee5783cc689fbff53afef49f772e6584121b70e6`. Positions use this revision. Changed entries are implemented; focused validation passed. KEEP rows describe source contracts.

| Unit | Current disposition|
| --- | ---|
| N1 | UTC wall-clock calls already share `now_utc`; each caller retains its ISO/strftime output.|
| N2 | Seven epoch conversions already delegate. Four broader admission/error surfaces remain individually kept.|
| N3 | Keep gmtime fractional-second truncation for session role labels.|
| N4 | Shared AnyIO clock and companion sleep already adopted; preserve the wait cadence and run liveness reads.|
| N5 | 49 short-id call sites already share lengths/defaults. The owning path-builder suffix keeps permissive bool/overlength slicing.|
| N6 | Canonical `protocols.to_uuid` or structural `ID.get_id` already used after each existing gate.|
| N7 | Four spelling-only uuid4 calls already share the import; formatting remains str(uuid4()).|
| B1 | Keep all paired native monotonic clock domains, including one synchronous abandoned-terminal path;35physical lines contain36AST calls.|
| B2 | Loop-clock calls already delegate `current_time`; adopted backend behavior is already landed.|
| B3 | `assert_eventually` already uses the shared elapsed clock.|
| B4 | Keep Element timestamp admission, precision and diagnostics.|
| B5 | The public coercer fixes are already landed; its numeric-before-ISO policy is unchanged.|
| B6 | Timestamp adapters already delegate with their own fallback/catch behavior.|
| B7 | Scheduler log conversions already delegate; preserve current recompute error boundary.|
| B8 | Three duration adapters already delegate with distinct flags/return/error adapters; stats has no independent parser.|
| B9 | Keep the durable native JSON codec and all four current SQLAlchemy serializer bindings, including the additional read-only engine binding.|
| B10 | Keep exact inline team message shape; its ids are already shared.|
| H1 | Strict `short_id` helper is already landed and public behavior is retained.|
| H2 | Positive finite `parse_duration` helper and caller flags are already landed.|

The baseline address index uses `U:` plus the unit name for these original unit populations.

## Exact call and factory contracts

**Contracts**

- **K2.1** — UTC wall-clock calls already share `now_utc`; each caller retains its ISO/strftime output. Preserve the enclosing expression at each indexed baseline position.
- **K2.2** — Seven epoch conversions already delegate. Four broader admission/error surfaces remain individually kept. Preserve the enclosing expression at each indexed baseline position.
- **K2.3** — `_iso` only guards None. Native fromtimestamp keeps epoch-only admission (including bool) and native TypeError for strings; shared coerce rejects bool and accepts strings/datetime. Do not widen or alter errors merely from float annotation.
- **K2.4** — Normal validated floats convert equivalently, but `created_datetime` currently delegates epoch-only conversion even for `model_construct`/bypassed values. coerce broadens string/datetime admission and rejects bool differently. No caller-domain proof excludes those observable property inputs; retain original conversion without a new guard.
- **K2.5** — `_fmt_rfc3339` only guards None, then epoch→UTC→local zone→seconds text. coerce changes bool/string admission and exceptions. Preserve local astimezone formatting and epoch-only boundary.
- **K2.6** — The at-trigger row uses a truthiness guard, then native epoch conversion. True is an epoch1 here; coerce rejects it, and string epochs would become newly accepted. Keep exact malformed-row errors and exported fire-time spelling.
- **K2.7** — gmtime truncates fractional seconds before HH:MM; datetime.fromtimestamp can round across the minute. Preserve deterministic role labels and None/name/id omission.
- **K2.8** — Shared AnyIO clock and companion sleep already adopted; preserve the wait cadence and run liveness reads. Preserve the enclosing expression at each indexed baseline position.
- **K2.9** — The issue's companion polling sleep already delegates to ln.concurrency.sleep; `eff_poll`/remaining clipping and cancellation stay at the original wait seam.
- **K2.10** — 49 short-id call sites already share lengths/defaults. The owning path-builder suffix keeps permissive bool/overlength slicing. Preserve the enclosing expression at each indexed baseline position.
- **K2.11** — The existing `random_hash_digits`>0 gate and Python slicing accept bool and lengths>32; `short_id` explicitly rejects them. This public path-builder input surface is broader than the bounded logger caller. Keep permissive suffix behavior; no public helper tightening.
- **K2.12** — Canonical `protocols.to_uuid`/`ID.get_id` is already used. Retain Observable normalization and lookup/type/error contracts; do not switch back to UUID(str(...)) or deprecated `ln.to_uuid`.
- **K2.13** — Four spelling-only uuid4 calls already share the import; formatting remains str(uuid4()). Preserve the enclosing expression at each indexed baseline position.
- **K2.14** — Decompose/fanout/synthesis/total timings share native starts and elapsed reads. Preserve progress/timing outputs and synchronous time access.
- **K2.15** — Execution/synthesis/planning/total phase durations share native monotonic starts and reads. Preserve CLI progress and timing data; do not swap clock domains as a spelling change.
- **K2.16** — The paired clock feeds operation elapsed output and the synchronous abandoned-terminal fallback. Preserve the same monotonic domain for start/read/default and eligibility without active AnyIO context.
- **K2.17** — Run-start and both terminal duration paths share monotonic scalars. Replacing the clock changes backend/context and test-patching/telemetry behavior; keep the paired domain.
- **K2.18** — Observed-run/ReAct start/finally duration signals use the same monotonic domain. Keep emitted duration values and the existing clock boundary.
- **K2.19** — Both reads conserve one tick deadline across stray cancellation; keep native monotonic deadline arithmetic while sleep delegation is handled separately.
- **K2.20** — Stream heartbeat initialization/activity/read/reset calls share one native monotonic domain. Preserve5second emission cadence across generator resumption and existing clock-patch behavior.
- **K2.21** — Loop-clock calls already delegate `current_time`; adopted backend behavior is already landed. Preserve the enclosing expression at each indexed baseline position.
- **K2.22** — `assert_eventually` already uses the shared elapsed clock. Preserve the enclosing expression at each indexed baseline position.
- **K2.23** — Element preserves float pass-through (including precision/nonfinite), local-naive datetime.timestamp, ISO-first strings, numeric fallback including Decimal/bool, and exact Invalid* errors. The shared UTC helper has different admission/order/error contracts; leave the validator unchanged.
- **K2.24** — Current helper already rejects bool, preserves aware datetime identity, normalizes naive UTC/trailingZ/z/Python3.10 fractions and keeps numeric-before-ISO strings. Do not revise this public behavior.
- **K2.25** — Timestamp adapters already delegate with their own fallback/catch behavior. Preserve the enclosing expression at each indexed baseline position.
- **K2.26** — Scheduler log conversions already delegate; preserve current recompute error boundary. Preserve the enclosing expression at each indexed baseline position.
- **K2.27** — Three duration adapters already delegate with distinct flags/return/error adapters; stats has no independent parser. Preserve the enclosing expression at each indexed baseline position.
- **K2.28** — Native json.dumps(default=`_json_serializer`,`allow_nan`=False) is the actual current durable codec. Preserve refusal and bytes; do not describe this path as permissive NaN.
- **K2.29** — All engine JSON binds retain `_dumps_with_uuid`: native spacing/ASCII/key coercion, UUID-only custom default and `allow_nan`=False durable refusal. `ln.json_dumps` broadens supported values/changes bytes; keep the existing codec policy.
- **K2.30** — Inline result messages intentionally retain `from_op` even when None and omit kind. `_build_message` adds kind and omits falsy `from_op`. Keep these exact persisted inbox shapes; N5id alone is already shared.
- **K2.31** — Existing strict1..32 integer helper, bool refusal and public export are landed. No new name or semantics needed.
- **K2.32** — Existing positive finite duration helper, flags, maximum and public export are landed. Preserve its current errors and caller adapters.

**Operations**

- **2.1** — N1 / Already shared / #3602; K2.1.
- **2.2** — N1 / Already shared / #3629; K2.1.
- **2.3** — N2 / Already shared / #3629; K2.2.
- **2.4** — N2 / KEEP / ; K2.3.
- **2.5** — N2 / KEEP / ; K2.4.
- **2.6** — N2 / KEEP / ; K2.5.
- **2.7** — N2 / KEEP / ; K2.6.
- **2.8** — N3 / KEEP / ; K2.7.
- **2.9** — N4 / Already shared / #3629; K2.8.
- **2.10** — N4 / Already shared / #3602; K2.8.
- **2.11** — N4 / Already shared / #3629; K2.9.
- **2.12** — N5 / Already shared / #3629; K2.10.
- **2.13** — N5 / KEEP / ; K2.11.
- **2.14** — N6 / Already shared / #3621; K2.12.
- **2.15** — N6 / Already shared / #3629; K2.12.
- **2.16** — N6 / Already shared / #3714; K2.12.
- **2.17** — N7 / Already shared / #3629; K2.13.
- **2.18** — B1 / KEEP / ; K2.14.
- **2.19** — B1 / KEEP / ; K2.15.
- **2.20** — B1 / KEEP / ; K2.16.
- **2.21** — B1 / KEEP / ; K2.17.
- **2.22** — B1 / KEEP / ; K2.18.
- **2.23** — B1 / KEEP / ; K2.19.
- **2.24** — B1 / KEEP / ; K2.20.
- **2.25** — B2 / Already shared / #3629; K2.21.
- **2.26** — B3 / Already shared / #3629; K2.22.
- **2.27** — B4 / KEEP / ; K2.23.
- **2.28** — B5 / Already shared / #3629; K2.24.
- **2.29** — B6 / Already shared / #3629; K2.25.
- **2.30** — B7 / Already shared / #3629; K2.26.
- **2.31** — B8 / Already shared / #3629; K2.27.
- **2.32** — B9 / KEEP / ; K2.28.
- **2.33** — B9 / KEEP / ; K2.29.
- **2.34** — B10 / KEEP / ; K2.30.
- **2.35** — H1 / Already shared / #3629; K2.31.
- **2.36** — H2 / Already shared / #3629; K2.32.

## Additional static native candidates

These are separate from the original issue-arm counts. Operations and baseline positions are retained; dynamic receiver types are not inferred from spelling alone. They remain unchanged.

**Contracts**

- **K3.1** — Preserve the native floating-point epoch value and its precision at this consumer; a UTC datetime round-trip changes representation/admission and can round sub-microsecond values.
- **K3.2** — Preserve the full UUID object or unsliced full-width spelling consumed here; `short_id` returns a bounded string and is not the same type/width contract.
- **K3.3** — Preserve the native monotonic clock domain, paired starts/deadlines and operation outside an active AnyIO context; replacing the clock also changes caller patching and scheduling assumptions.
- **K3.4** — Preserve the explicit format, supplied `struct_time` and local/platform formatting semantics; `now_utc` is a wall-clock datetime constructor, not this formatter.
- **K3.5** — Preserve host-local timezone/DST `struct_time` semantics; UTC datetime coercion changes the value and formatting contract.
- **K3.6** — Preserve synchronous blocking delay and native value/error behavior; an async sleep returns an awaitable and requires a different caller contract.
- **K3.7** — Preserve this epoch-only conversion, explicit timezone/local interpretation and native admission/errors rather than broadening through a multi-type coercer.
- **K3.8** — Preserve strict ISO parsing, native errors and timezone/naive interpretation at this consumer; general coercion also accepts numeric strings and other input types.
- **K3.9** — Preserve this UUID constructor form, argument/keyword admission and native errors; structural UUID helpers accept a different set of objects.
- **K3.10** — Preserve deterministic namespace/name UUID identity; a random short identifier is a different value and representation.
- **K3.11** — Preserve UTC `struct_time` conversion and fractional-second truncation used by this formatter.
- **K3.12** — Preserve the paired native high-resolution elapsed counter and synchronous availability; a backend loop clock changes the domain/context and instrumentation boundary.
- **K3.13** — Preserve the supplied format grammar, structuring/timezone interpretation and native refusal surface; general timestamp coercion is broader.

**Operations**

- **3.1** — time.time; K3.1.
- **3.2** — uuid.uuid4; K3.2.
- **3.3** — time.monotonic; K3.3.
- **3.4** — time.strftime; K3.4.
- **3.5** — time.localtime; K3.5.
- **3.6** — time.sleep; K3.6.
- **3.7** — datetime.datetime.fromtimestamp; K3.7.
- **3.8** — datetime.datetime.fromisoformat; K3.8.
- **3.9** — uuid.UUID; K3.9.
- **3.10** — uuid.uuid5; K3.10.
- **3.11** — time.gmtime; K3.11.
- **3.12** — `time.perf_counter`; K3.12.
- **3.13** — datetime.datetime.strptime; K3.13.

## Baseline address index

Entries use `contract=lines`, or `line:column` for JSON calls. `U:` identifies unit rows; `×N` preserves occurrences. Ranges cover original spans only. All positions use the baseline.

- `lionagi/agent/context_fold.py`: `3.1=542,634`.
- `lionagi/agent/factory.py`: `3.1=1330; 3.2=1380`.
- `lionagi/agent/nudge.py`: `3.1=172`.
- `lionagi/cli/_code_identity.py`: `2.1=101; 3.3=109,112; U:N1=101`.
- `lionagi/cli/_providers.py`: `2.3=521; 3.1=520; U:N2=521`.
- `lionagi/cli/_runs.py`: `2.12=66; 2.17=2029,2034,2072,2073; 3.1=470,586,638,1013,1585,2116; 3.2=2029,2034,2072,2073; U:N5=66; U:N7=2029,2034,2072,2073`.
- `lionagi/cli/agent.py`: `3.1=1680,1728,1856,2095,2157; 3.3=498,548,884,904,1871,1876,1895`.
- `lionagi/cli/dispatch.py`: `3.4=28; 3.5=28`.
- `lionagi/cli/engine.py`: `3.1=274,354,398,420,440,544,576; 3.2=273`.
- `lionagi/cli/invoke.py`: `2.12=30; 3.1=40,91; U:N5=30`.
- `lionagi/cli/kill.py`: `3.1=500,601,752,926,1042,1090,1147,1196; 3.3=325,327; 3.6=333`.
- `lionagi/cli/machine_schedule.py`: `3.1=210; 3.3=63,77; 3.7=227`.
- `lionagi/cli/mirror.py`: `2.31=330; 3.1=560,798; U:B8=330`.
- `lionagi/cli/monitor.py`: `2.31=144; 3.1=128,150,1672,2021,2028; 3.3=1337,1351,1643,1646; 3.4=570,573,725,866,1171; 3.5=570,573,725,866; 3.6=1179,1586; U:B8=144`.
- `lionagi/cli/orchestrate/__init__.py`: `3.2=1231`.
- `lionagi/cli/orchestrate/_common.py`: `2.12=354,383,396; 2.34=381,394; U:B10=381,394; U:N5=354,383,396`.
- `lionagi/cli/orchestrate/_notify.py`: `3.2=262`.
- `lionagi/cli/orchestrate/_orchestration.py`: `3.1=1996,2079,2225,2298; 3.2=1970,2130`.
- `lionagi/cli/orchestrate/_quiescence.py`: `3.3=277,278; 3.6=286`.
- `lionagi/cli/orchestrate/fanout.py`: `2.18=352,376,469,553,618,641,722; 3.1=198; 3.3=352,376,469,553,618,641,722; U:B1=352,376,469,553,618,641,722`.
- `lionagi/cli/orchestrate/flow.py`: `2.14=1379,1403,1891,1912; 2.19=2421,2501,2837,2873,3530,3615,3807; 2.3=677; 3.1=516,1834,1836,1844,1989,2216,3119,3320,3395; 3.2=3483; 3.3=2421,2501,2837,2873,3530,3615,3807; U:B1=2421,2501,2837,2873,3530,3615,3807; U:N2=677; U:N6=1379,1403,1891,1912`.
- `lionagi/cli/state.py`: `2.12=1336; 2.29=134,139,1345; 3.1=59,123,505,709,710,766,964,992,1021,1118,1218; 3.2=159,246; 3.4=419; 3.5=419; U:B6=134,139,1345; U:N5=1336`.
- `lionagi/cli/stats.py`: `2.4=48; 3.7=48; U:N2=48`.
- `lionagi/cli/status.py`: `3.4=537; 3.5=537`.
- `lionagi/cli/team.py`: `2.12=154,178,704; U:N5=154,178,704`.
- `lionagi/dispatch/outbox.py`: `3.1=178,339,590; 3.2=179,180,246`.
- `lionagi/engines/coding.py`: `3.3=938,970,975`.
- `lionagi/engines/engine.py`: `2.12=460; 3.3=489,567,576,1088; U:N5=460`.
- `lionagi/hooks/builtins.py`: `3.1=87,135,220`.
- `lionagi/integrations/ruvector/memory.py`: `2.14=64; 2.15=97; U:N6=64,97`.
- `lionagi/ln/_utils.py`: `2.13=203; 2.28=443; 2.35=67; 2.36=74; U:B5=443; U:H1=67; U:H2=74; U:N5=203`.
- `lionagi/mcp/_notify_hook.py`: `3.3=51,82`.
- `lionagi/mcp/_run_detail.py`: `3.1=167,325`.
- `lionagi/mcp/dispatch.py`: `2.1=56; 3.1=57,872; U:N1=56`.
- `lionagi/mcp/jobs.py`: `2.11=3104; 2.12=289,398; 2.2=283,288,2974; 2.3=1786; 2.9=3066,3097; 3.8=2969; U:N1=283,288,2974; U:N2=1786; U:N4=3066,3097,3104; U:N5=289,398`.
- `lionagi/operations/_turn_origin.py`: `3.2=45`.
- `lionagi/operations/act/act.py`: `3.2=55`.
- `lionagi/operations/flow.py`: `2.20=577×2,666,677,678; 2.3=85; 3.3=577×2,666,677,678; U:B1=577×2,666,677,678; U:N2=85`.
- `lionagi/operations/run/run.py`: `2.21=443,810,841; 2.9=95,207,221,288,574; 3.3=443,810,841; U:B1=443,810,841; U:N4=95,207,221,288,574`.
- `lionagi/protocols/action/manager.py`: `3.3=37`.
- `lionagi/protocols/generic/element.py`: `2.27=75; 2.5=119; 3.7=119; 3.8=87; 3.9=109,253,274,310; U:B4=75; U:N2=119`.
- `lionagi/protocols/generic/flow.py`: `2.16=120; U:N6=120`.
- `lionagi/protocols/graph/node.py`: `2.1=125,158; U:N1=125,158`.
- `lionagi/protocols/messages/prepare.py`: `3.1=63,86`.
- `lionagi/providers/_subagent_admission.py`: `3.1=672; 3.3=522,524,537,539,666,751,757; 3.6=541`.
- `lionagi/providers/google/gemini_code.py`: `2.25=587,719; U:B2=587,719`.
- `lionagi/providers/openai/codex.py`: `2.25=3003,3474; U:B2=3003,3474`.
- `lionagi/providers/pi/cli.py`: `2.25=441,595; U:B2=441,595`.
- `lionagi/service/accounts/_router.py`: `3.2=125`.
- `lionagi/service/connections/endpoint.py`: `3.2=577,639,641`.
- `lionagi/service/hooks/hook_event.py`: `2.10=36,77; U:N4=36,77`.
- `lionagi/service/imodel.py`: `3.2=189`.
- `lionagi/service/resilience.py`: `3.1=125,142,176,202`.
- `lionagi/session/branch.py`: `2.22=480,502,1206,1249; 3.3=480,502,1206,1249; U:B1=480,502,1206,1249`.
- `lionagi/session/observer.py`: `3.1=487; 3.3=232,556`.
- `lionagi/state/artifact_verifier.py`: `3.1=236,274`.
- `lionagi/state/claude_mirror.py`: `2.29=57; 3.10=45; 3.9=33; U:B6=57`.
- `lionagi/state/codex_mirror.py`: `2.29=213; 3.1=227; 3.10=201; 3.9=81; U:B6=213`.
- `lionagi/state/db.py`: `2.12=6416,6520; 3.1=1780,1971,2685,3153,3167,3342,3537,3915,4085,4295,4360,4722,4771,4841,4881,5335,5371,5461,5559,5645,5928,6117,6186,6415,6529,6573,6683,6789,6911,6931,7043,7111,7517,7617,7653,7727,7816,7880,7902,7950; 3.2=7105,7210,7780; U:N5=6416,6520`.
- `lionagi/state/engine.py`: `2.32=258; 2.33=777,834,859,913; U:B9=258,777,834,859,913`.
- `lionagi/state/lifecycle/deliveries.py`: `3.1=30`.
- `lionagi/state/lifecycle/schedules.py`: `3.1=365`.
- `lionagi/state/lifecycle/service.py`: `2.12=497,644; 3.1=209,358; 3.2=208; U:N5=497,644`.
- `lionagi/state/session_naming.py`: `2.8=144; 3.11=144; 3.4=144; U:N3=144`.
- `lionagi/state/staleness.py`: `3.1=33`.
- `lionagi/studio/cli.py`: `2.2=1386; 2.6=1578; 3.1=1391; 3.3=1055,1057,1238,1248,1640,1642,1649,1650,1709,1710; 3.6=1643,1651,1711; 3.7=1578; U:N1=1386; U:N2=1578`.
- `lionagi/studio/config.py`: `2.3=553; 3.1=528; U:N2=553`.
- `lionagi/studio/launch_access.py`: `3.3=188,257,303,340`.
- `lionagi/studio/operator/application_mcp.py`: `3.1=1240`.
- `lionagi/studio/operator/cancel_run.py`: `3.1=346,423`.
- `lionagi/studio/operator/coordinator.py`: `2.12=541; 3.1=467,549,918,919; U:N5=541`.
- `lionagi/studio/operator/permission_mcp.py`: `3.1=97`.
- `lionagi/studio/operator/rename_session.py`: `3.1=175`.
- `lionagi/studio/operator/resume_run.py`: `3.1=236`.
- `lionagi/studio/operator/run_control.py`: `3.1=381`.
- `lionagi/studio/operator/run_progress.py`: `3.1=650`.
- `lionagi/studio/operator/store.py`: `2.12=1970,2247,2306,2424,2518,2569; 3.1=563,656,768,903,922,1033,1040,1086,1135,1141,1222,1365,1459,1702,1733,1797,1998,2042,2202,2386,2464,2538,2625; 3.2=564,729,787,827,1082,1364,1730,2039,2040; U:N5=1970,2247,2306,2424,2518,2569`.
- `lionagi/studio/reconnect.py`: `3.3=257,265; 3.6=267`.
- `lionagi/studio/scheduler/admit.py`: `3.1=254`.
- `lionagi/studio/scheduler/engine.py`: `2.12=506,963,1183,1362,1475,1502,1958,2273,2331,2465,2526,2818,3207; 2.23=992,994; 2.25=1059,1060; 2.3=597,604; 2.30=824,825; 3.1=526,557,647,658,775,807,881,1083,1150,1154,1266,1375,1536,1684,1710,2207,2768,2792,2813,2894,3107,3232,3327; 3.2=2168; 3.3=992,994; 3.7=3418; U:B1=992,994; U:B2=1059,1060; U:B7=824,825; U:N2=597,604; U:N5=506,963,1183,1362,1475,1502,1958,2273,2331,2465,2526,2818,3207`.
- `lionagi/studio/scheduler/signals.py`: `3.1=288`.
- `lionagi/studio/scheduler/worker.py`: `2.12=256; 3.1=207,268,360,555,576,633; U:N5=256`.
- `lionagi/studio/services/admin.py`: `3.1=845,960,1182; 3.12=146,183,198,324,334; 3.2=1328; 3.3=358,368,568,693,697`.
- `lionagi/studio/services/approvals.py`: `3.1=194,318,354,362,400,503; 3.2=193,363`.
- `lionagi/studio/services/attention.py`: `3.1=184,271,305; 3.2=221,284`.
- `lionagi/studio/services/db_maintenance.py`: `3.1=699,762,843,844; 3.12=202,211`.
- `lionagi/studio/services/definitions.py`: `3.1=454,507`.
- `lionagi/studio/services/engine_defs.py`: `2.12=145; 3.1=146; U:N5=145`.
- `lionagi/studio/services/invocations.py`: `3.1=95,285`.
- `lionagi/studio/services/launches.py`: `2.12=209; 3.1=210,323,364; U:N5=209`.
- `lionagi/studio/services/leo.py`: `3.1=42,75,85,88,352,368; 3.2=78`.
- `lionagi/studio/services/lifecycle.py`: `3.1=117,238,342,437,559,681`.
- `lionagi/studio/services/mcp_servers.py`: `3.1=374,483,495,543`.
- `lionagi/studio/services/operator.py`: `2.25=286,300,302; U:B2=286,300,302`.
- `lionagi/studio/services/projects.py`: `3.1=171,205,243`.
- `lionagi/studio/services/retention_archive.py`: `2.12=45; U:N5=45`.
- `lionagi/studio/services/run_tags.py`: `3.1=52`.
- `lionagi/studio/services/runs.py`: `3.1=225,348`.
- `lionagi/studio/services/schedule_declaration.py`: `2.12=796,1832; 2.31=335; 3.1=797,1372,1820; 3.8=351; U:B8=335; U:N5=796,1832`.
- `lionagi/studio/services/schedule_export.py`: `2.7=204; 3.7=204; U:N2=204`.
- `lionagi/studio/services/schedules.py`: `2.12=1240; 3.1=560,1149,1241; 3.13=115; 3.7=439; U:N5=1240`.
- `lionagi/studio/services/sessions.py`: `2.24=1747,1785,1791,1793,1832,1844,1854,1856; 3.1=1798,1859; 3.3=1747,1785,1791,1793,1832,1844,1854,1856; U:B1=1747,1785,1791,1793,1832,1844,1854,1856`.
- `lionagi/studio/services/shows.py`: `2.29=614,619,626; 3.1=492,633,747,769,772; 3.2=498,591; U:B6=614,619,626`.
- `lionagi/studio/services/stats.py`: `3.1=49,122,173`.
- `lionagi/studio/services/task_applications.py`: `3.1=123; 3.2=149,202`.
- `lionagi/studio/services/workflow_defs.py`: `2.12=160; 3.1=161; U:N5=160`.
- `lionagi/studio/services/workflow_run.py`: `3.1=57; 3.2=38`.
- `lionagi/testing/_endpoint.py`: `2.12=236,258,260; U:N5=236,258,260`.
- `lionagi/testing/helpers.py`: `2.26=34,35; 3.1=197; U:B3=34,35`.
- `lionagi/tools/khive_injection.py`: `2.12=423; U:N5=423`.
- `lionagi/tools/sandbox.py`: `2.12=267; U:N5=267`.
