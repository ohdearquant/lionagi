# Files and processes

Issue #3582 baseline: `ee5783cc689fbff53afef49f772e6584121b70e6`. Positions use this revision. Changed entries are implemented; focused validation passed. KEEP rows describe source contracts.

Accounting at commit `ee5783cc689fbff53afef49f772e6584121b70e6` preserves existing behavior, bytes, API bindings, errors and initialization. No new public helper or behavior-changing replacement is included.

Already addressed: [#3617](https://github.com/ohdearquant/lionagi/pull/3617) (dispatch timeout/group cleanup), [#3618](https://github.com/ohdearquant/lionagi/pull/3618) (hook serialization and cleanup), [#3619](https://github.com/ohdearquant/lionagi/pull/3619) (temporary monitor signal handling), and [#3661](https://github.com/ohdearquant/lionagi/pull/3661) (six shared workspace-root normalization in six tool constructors).

The existing start-time comparator is reused by `lionagi/cli/kill.py::_check_pid_identity`, through a private alias: implemented, focused validation passed. Its tolerance, guard/probe order, verdicts and exception behavior remain unchanged. This implementation is separate from the already merged work.

The native-call accounting covers 280 Path constructor/home/cwd calls, four PurePath constructors, 22 run/Popen calls, nine temporary-resource calls, twenty file-lock calls and two SIGPIPE registrations. These 337 direct native sites retain their contracts. Ten session/dispatch/operator objects named signal are excluded; they are unrelated to stdlib signal handling. `map(Path, roots)` at `lionagi/mcp/jobs.py:1226` is separately retained as a deferred constructor factory. Two `isinstance(..., Path)` expressions are type predicates. Direct constructor/module-call counts do not enumerate all bound Path methods.

## Original issue units

| Unit | Existing contract and treatment|
| --- | ---|
| N1 | Existing private root helper is already shared by all six tool constructors; retain empty/None/cwd/expanduser/resolve behavior and its private name.; `create_path` accepts leading-dot components and preserves relative return spelling; keep its own dot/backslash checks, lazy containment import, admission and mkdir/error order.; Keep public signatures, errors and current libs owner. Moving bodies into ln has no exact alias/cycle/source-initialization proof.; Agent artifact path preserves existing component checks, resolved containment and exact ValueError envelope; common validation can reject currently accepted spellings.; Save path is already resolved and separately admitted against cwd/home; retain exact `log_error` and existing order.; This tool already uses existing private `_resolve_workspace_root` with unchanged argument/default/expanduser/resolve behavior from PR3661; no new public root helper.; Plugin bundle candidate preserves resolved bundle boundary, symlink/traversal refusal and its own ValueError wrapper.; Fixture loader keeps its checks before prefix containment and exact PermissionError text; normalization or validator changes are not established equivalent.; Artifact `safe_join` keeps absolute/backslash/traversal checks, root symlink handling and ArtifactPathError envelope.|
| N2 | Private writer keeps None return, nonfinite check before mkstemp0600, stdlib indent2, replace and BaseException cleanup. Relocation lacks exact initialization/alias/error proof.; Kill retains existing private `_runs` writer binding and surrounding abort/audit ordering; no exact import-owner/alias/error equivalence for relocation is established.; Both snapshots retain current worker offload and private writer serializer/0600/None-return/error behavior and import owner.; MCP text writer keeps mkdir, mkstemp0600, replace, BaseException cleanup and chmod0600 after publication; generic mode/return/cleanup order differs.|
| N3 | MCP Windows lock seeks byte0 without restoring offset; POSIX flock and `RECORD_ABSENT`/`LOCK_UNAVAILABLE`/error precedence remain native.; Plugin Windows lock uses `LK_LOCK`/`LK_RLCK` and restores prior file offset after success; POSIX shared/exclusive and unlock behavior remain native.|
| N4 | Existing start-time comparator implemented; focused validation passed. Keep existing five lazy process exports; requested eleven names would add public API and source initialization.; Keep internal helper/constant identities and unknown/gone/found/witness contracts unchanged; only existing start-time comparator is a selected consumer reuse.; MCP `pid_alive` has low-PID/waitpid reaping and propagates generic os.kill OSError; CLI returns False. Keep PGID OSError fallback and identity states.; Spawned PGID fallback, psutil failure envelope and None for unverified create-time remain local; do not collapse unknown identity or weaken group ownership.; Only inclusive raw-create-time comparison may reuse existing private `_start_time_matches` alias. Keep expected-time guard, once-only probe, psutil verdicts, unexpected exception identity and later cmdline/session checks.; CLI PID policy rejects invalid PID, treats PermissionError as alive and other OSError as False without MCP waitpid policy.; None PID wrapper remains unknown None before delegating to existing CLI PID policy; bool-only generalization erases unknown.; Admin keeps no-recorded-time path, zombie detection, invalid-PID precheck and1.0s tolerance; unknown remains alive. Kill helper has0.1s tolerance and is not equivalent.|
| N5 | Keep EOF-draining tail2048 bytes and plain-byte result; no new flag/dataclass, head retention or normalized cap contract.; Keep EOF-draining head retention, existing truncated flag/logging, per-stream caps and hook trust/cleanup ownership.|
| N6 | Keep `env_fn`/cwd/trusted executable, state-URL removal, witnessed cleanup, warning/outcome callbacks and best-effort2s reap. Existing shared helper lacks these options; no extension/new API.|
| N7 | Both monitor takeovers already use existing `temporary_signal_handlers`; preserve main-thread/None-handler abstention and installed-handler restoration.; Existing helper and restoration behavior landed in PR3619; no new signature/name/extraction.|
| B1 | Last-branch pointer keeps in-place stdlib JSON and caught/logged convenience error; atomic replacement changes inode/hard-link/open-reader behavior.; Snapshot keeps nonfinite precheck then compact stdlib default=str JSON written in place; replace changes inode/mode/symlink/reader contract.; Cause record keeps in-place stdlib JSON and all-Exception suppression; new nonfinite refusal/publication can silently omit forensic output.; Hook library keeps mkdir and in-place indent2 JSON plus trailing newline; changing inode/mode/hard-link behavior is not neutral.; Both config/settings keep in-place indent2 JSON plus trailing newline and distinct validated/disabled result paths.; Both Markdown updates retain body/newline formatting and supported write-through symlinks; replacing link/inode changes update behavior.; Both disk writes stay inside existing worker, preserving content, directory ownership and supported symlink following; replace changes inode/readers/modes.; YAML retains produced formatting and in-place/symlink update semantics; generic publication changes inode/mode/failure behavior.; Keep sequence-derived fixed temp, default=str/nonfinite ordering and replace; interrupted temp remains today. Unique-temp cleanup/modes differ.; Keep exact serialized-byte unchanged-write shortcut then fixed .json.tmp replace, current leftover-temp/mode/symlink behavior.; Keep `acreate_path` admission, AnyIO fixed .tmp, ln checked JSON bytes and offloaded replace; async serializer/cleanup/mode defaults differ.; Keep pre-temp nonfinite check, pid+id unique sibling temp, indent2 JSON, replace and suppressed-unlink BaseException cleanup; native umask mode differs from generic preserve-mode policy.|
| B2 | Keep permissive NaN/Infinity stdlib json.dump(indent2,`sort_keys`), flush/fsync and replace; refusal changes accepted bytes/error policy.; Existing except Exception swallows write failures; adding refusal silently omits a record currently written.|
| B3 | Windows missing fcntl yields unlocked; POSIX required error raises, optional error warns/yields. New msvcrt contention changes run outcomes.; Windows missing fcntl yields unlocked; POSIX lock spans child lifetime with explicit unlock. New Windows locking changes failures/unwind.; Module-level fcntl currently prevents Windows import; keep POSIX shared read/exclusive in-place rewrite, fsync and unlock.; Keep module-level fcntl, nonblocking claim/error result and retained fd lifetime; Windows import success is a separate behavior change.|
| B4 | PR3618 already supplies pre-spawn serialization and witnessed cancellation cleanup while preserving prehook PermissionError/posthook logging envelope.; PR3617 already uses shared exchange and both timeout classes on3.10 with witnessed group cleanup and existing dispatch warning/result envelope.; Keep existing shared exchange, abandoned-spawn ownership and cleanup semantics; no generic env/cwd/cap extension.; Keep copy-then-hash trusted executable pinning, capped streams and existing timeout/cancel/error handling; generic runner changes trust/lifecycle.; gh auth token remains without a new10s bound; a bound changes long-running lookup into None.; Keep command env/stdin/decoding/redaction and current post-SIGKILL wait; imposing2s reap changes lifecycle and return timing.|
| B5 | Keep shell decision, session/witness/env/capped byte drains, timeout result labels and unbounded final wait; new ProcessRun/pattern/reap differs.; Keep existing control refusal text, shlex splitting and tool runner. Adding dollar-brace variable recognition rejects currently accepted input.; Keep current control/split validation and worker/result contract; new dollar-brace pattern changes acceptance.; Control pattern chooses shell=True here, so recognizing variable expansion enables shell processing rather than tool refusal.; Keep current shell control admission/refusal policy; shared pattern expansion changes authorization.; Keep native locale-strict text/newlines,30s timeout and exact AstSearchResponse timeout/spawn/error messages, inherited session.; Keep native locale-strict text/newlines,30s timeout and exact CodeCheckResponse violation/error/timeout/spawn behavior, inherited session.; Keep check=True15s locale text and exact class-name/cause-bearing setting errors for subprocess/JSON failures.; Keep locale text, native timeout and returncode/stdout; only OSError/TimeoutExpired produce empty error result.; Keep shared remaining budget, locale text stdout/stderr and native exception class/message diagnostics.; Keep lifecycle unknown fallback, sender-specific timeout/OSError causes/output limits and live-job append-log Popen lifecycle distinct; exact per-call contracts retained separately.|
| B6 | PR3619 already handles both monitor signal scopes; keep current abstention/restoration semantics.; Keep asyncio.run active-loop RuntimeError/default SIGTERM behavior; `run_async` changes BaseException/cancellation. No dedicated entrypoint rail was identified.; Keep asyncio.run and Exception-only main handler, current SIGTERM/exit/lock behavior; helper-only signal tests do not prove this entrypoint.|
| H1 | KEEP requested public spelling; existing `_resolve_workspace_root` already landed for six roots. No gratuitous new public name/alias/deprecation; preserve existing empty/None/current-cwd behavior.|
| H2 | KEEP current libs owner and public bindings; no relocation. Exact source initialization/cycle/alias/error equivalence for a libs-to-ln move is not established. Public helper bindings and compatibility behavior remain unchanged.|
| H3 | KEEP current writer owners; no new generic atomic APIs/private relocation. Existing None return, serializer/check/cleanup/mode/publication order and initialization are part of caller behavior. A generic Path-returning writer or relocation lacks exact API/error/initialization proof.|
| H4 | KEEP native lock wrappers/no public additions. Offset restoration and Windows lock availability differ; unchanged facade/initialization is not established.|
| H5 | KEEP native implementations and eleven private `_proc` names; no new ln exports. MCP propagates generic os.kill OSError while CLI returns False; waitpid low-PID/reap and unknown-identity distinctions must survive. kill may reuse existing `start_time_matches` at `_proc`:184-190 without an export change.|
| H6 | KEEP local head/tail readers/no public additions. EOF draining/cap allocation/truncated flags/logging differ; zero/negative cap policy is not a neutral new contract.|
| H7 | KEEP proposed generic API and all remaining specialized runners; existing communicate exchange remains unchanged. Existing shared runner deliberately lacks cwd/env/executable/caps and has exact Python3.10 timeout/cancel behavior; no behavior-changing public helper extension allowed in this cluster.|
| H8 | KEEP native synchronous sites/no new public contract. New sessions/group timeouts/result type would change current exceptions/output, decoding and terminal signal behavior.|
| H9 | KEEP native patterns/split logic. A shared ${ addition reverses consumer effect: tool refusal vs engine shell execution. Preserve caller-owned messages and shell decisions.|
| H10 | `ALREADY_MERGED` existing name and integrations; KEEP unchanged. PR3619 is proven; no duplicate extraction, signature change, or altered handler ownership.|

## Native subprocess callers

The 22 subprocess contracts, including every option and refusal boundary, are retained once under the direct-native contracts below. The address index marks this original population as `subprocess`.

## Complete direct native-call locations

**Contracts**

- **K1.1** — Native Path construction leaves normalization, existence, symlink, IO and admission policy to its surrounding consumer. Existing ln root/containment helpers resolve/admit paths and can change spelling, IO and errors; they are not constructor equivalents. This row proves only construction, not all following bound Path methods.
- **K1.2** — Native home lookup; keep native user-home lookup/errors and Path spelling. ln workspace-root resolution adds default-cwd/expand/resolve policy and filesystem observations; containment enforces another contract.
- **K1.3** — Git top-level probe keeps cwd, 5s native run timeout, locale-strict text/newline handling, no new session, returncode-zero Path construction and every Exception mapped to None. A group runner would change process lifetime/timeout exception and result contract.
- **K1.4** — Native current-directory lookup; preserve its OSError/timing and native Path value. ln workspace-root resolution additionally expands/resolves and applies default-root policy; containment admits paths. Neither is a generic cwd equivalent. Any outer bound Path methods remain owned by this consumer.
- **K1.5** — Codex allowlist discovery keeps the selected CLI, copied env-or-None, 15s timeout, check=True CalledProcessError, locale text and JSON parsing. OSError/SubprocessError/JSONDecodeError are replaced by the exact setting error with original cause and class-name text; no group runner equivalence exists.
- **K1.6** — Git identity consumes a shared remaining budget, keeps min(per-call budget,left), check=False and locale-strict text. OSError/SubprocessError become the diagnostic sentinel plus class/message; returncode and stripped stdout/stderr remain separate. New sessions or timeout/result wrappers would change this diagnostic contract.
- **K1.7** — Origin-URL detection keeps a 5s timeout, cwd, locale text and inherited session. Nonzero or any Exception returns None; zero stdout is parsed as a remote URL. No shared process result or group ownership is substituted.
- **K1.8** — Existing JSON writer checks nonfinite before temp creation, uses mkstemp0600, stdlib indent2 text, replace publication, BaseException cleanup suppressing only unlink OSError, and returns None. Retain the current None return, mode, fsync, serializer and initialization behavior; the writer stays at its current location.
- **K1.9** — POSIX-only manifest lock holds across full read/modify/write, opens0600 with CLOEXEC/NOFOLLOW, releases by close, skips Windows; required OSError propagates, optional failure warns and yields unlocked. New Windows locking or generic wrapper changes platform/failure behavior. Exact site `_run_manifest_lock`: fcntl.flock(fd, `fcntl.LOCK_EX`).
- **K1.10** — CLI interactive SIGPIPE intentionally becomes `SIG_DFL` after the machine-path early return; not temporary SIGINT/SIGTERM takeover. Preserve global disposition/worker failure behavior.
- **K1.11** — Background flow deliberately returns immediately with a live Popen PID, redirected flow.log stdout+stderr, new session and `LIONAGI_SESSION_ID` env. It does not wait, collect output, time out or own a synchronous completion result; a run helper cannot preserve this launch/ownership API.
- **K1.12** — POSIX module import remains native; finalization takes exclusive nonblocking flock on CLOEXEC0600 fd before terminal read. OSError closes and returns None, other BaseException closes/rethrows; successful claim retains fd through later ownership. A contextmanager/shared platform lock changes claim lifetime/import/error policy. Exact site `claim_finalization`: fcntl.flock(fd, `fcntl.LOCK_EX` | `fcntl.LOCK_NB`).
- **K1.13** — Round records mkdir first, mkstemp0600, stdlib indent2/`sort_keys`, flush+fsync, replace, then BaseException unlink without suppressing its failure. Nonfinite tokens remain permitted. A generic writer would change serializer/check ordering, cleanup precedence, modes or return contract.
- **K1.14** — POSIX-only module import and flock protect same-inode team read or read/truncate/rewrite. Readers use shared lock/unlock and OSError/JSON None; writers exclusive lock, nonfinite check before truncate, flush/fsync before unlock. Atomic replacement or Windows availability changes concurrency/inode/platform policy. Exact site `_locked_team`: fcntl.flock(fp.fileno(), `fcntl.LOCK_EX`).
- **K1.15** — POSIX-only module import and flock protect same-inode team read or read/truncate/rewrite. Readers use shared lock/unlock and OSError/JSON None; writers exclusive lock, nonfinite check before truncate, flush/fsync before unlock. Atomic replacement or Windows availability changes concurrency/inode/platform policy. Exact site `read_team_json`: fcntl.flock(fp.fileno(), `fcntl.LOCK_SH`).
- **K1.16** — POSIX-only module import and flock protect same-inode team read or read/truncate/rewrite. Readers use shared lock/unlock and OSError/JSON None; writers exclusive lock, nonfinite check before truncate, flush/fsync before unlock. Atomic replacement or Windows availability changes concurrency/inode/platform policy. Exact site `_locked_team`: fcntl.flock(fp.fileno(), `fcntl.LOCK_UN`).
- **K1.17** — POSIX-only module import and flock protect same-inode team read or read/truncate/rewrite. Readers use shared lock/unlock and OSError/JSON None; writers exclusive lock, nonfinite check before truncate, flush/fsync before unlock. Atomic replacement or Windows availability changes concurrency/inode/platform policy. Exact site `read_team_json`: fcntl.flock(fp.fileno(), `fcntl.LOCK_UN`).
- **K1.18** — Trusted executable copy needs private mkdtemp0700, same basename, exclusive executable0700 copy, copy-then-hash trust sequence, fsync and bound-executable lifetime cleanup. This is a staged executable capability, not atomic text/JSON publication.
- **K1.19** — Code generation owns TemporaryDirectory lifetime through generation, module loading, validation and `model_rebuild`. Context cleanup and exact nested errors/causes stay intact; root/containment or atomic write helpers do not replace this temporary directory contract.
- **K1.20** — Child actor config uses same-dir mkstemp0600, UTF-8 with newline empty, exact TOML `new_text` and replace. Only OSError cleanup/replacement becomes ChildActorError with source-context/cause; generic JSON/text options and BaseException cleanup would change bytes or failure precedence.
- **K1.21** — Terminal-hook delivery retains Popen pipes/text/env/cwd/new session and a witnessed leader. Spawn and communicate SubprocessError/OSError map to delivery failure; communicate timeout triggers witnessed group teardown. Locale text stdin/stdout, classification and returncode envelope stay caller-owned; the proposed byte/result runner is not equivalent.
- **K1.22** — Machine CLI keeps byte capture, `MACHINE_TIMEOUT_SECONDS`, inherited session and check=False. TimeoutExpired becomes unavailable OpError with original cause; OSError becomes internal OpError. Full stdout length is checked before UTF-8 replacement decode, stderr tail and JSON envelope checks; no process-group/result wrapper preserves all these contracts.
- **K1.23** — MCP lock wrapper chooses POSIX flock or Windows one-byte lock after lseek0 and does not restore offset; surrounding `locked_job` preserves `RECORD_ABSENT` versus `LOCK_UNAVAILABLE` and exception precedence. Proposed restored-offset/shared platform helper changes actual Windows behavior. Exact site `_lock_fd`: `_fcntl.flock`(fd, `_fcntl.LOCK_EX`).
- **K1.24** — MCP lock wrapper chooses POSIX flock or Windows one-byte lock after lseek0 and does not restore offset; surrounding `locked_job` preserves `RECORD_ABSENT` versus `LOCK_UNAVAILABLE` and exception precedence. Proposed restored-offset/shared platform helper changes actual Windows behavior. Exact site `_lock_fd`: `_msvcrt.locking`(fd, `_msvcrt.LK_LOCK`, 1).
- **K1.25** — MCP lock wrapper chooses POSIX flock or Windows one-byte lock after lseek0 and does not restore offset; surrounding `locked_job` preserves `RECORD_ABSENT` versus `LOCK_UNAVAILABLE` and exception precedence. Proposed restored-offset/shared platform helper changes actual Windows behavior. Exact site `_unlock_fd`: `_fcntl.flock`(fd, `_fcntl.LOCK_UN`).
- **K1.26** — MCP lock wrapper chooses POSIX flock or Windows one-byte lock after lseek0 and does not restore offset; surrounding `locked_job` preserves `RECORD_ABSENT` versus `LOCK_UNAVAILABLE` and exception precedence. Proposed restored-offset/shared platform helper changes actual Windows behavior. Exact site `_unlock_fd`: `_msvcrt.locking`(fd, `_msvcrt.LK_UNLCK`, 1).
- **K1.27** — Lifecycle query keeps native byte capture, exact timeout and check=False. Every Exception returns unknown None, never no-record; output byte ceiling, UTF-8 replacement/JSON/lifecycle validation remain local. Adding group teardown or a new result type is outside this read-fallback contract.
- **K1.28** — Sender resolution keeps cwd, byte capture, exact identity timeout and check=False. TimeoutExpired/OSError are separately replaced by exact NotifySenderResolutionError messages and causes; bytes/str handling, output limit, exit and strict actor-envelope checks remain local.
- **K1.29** — Job submit needs a live Popen handle/PID, append-mode shared log, merged stderr, DEVNULL stdin, cwd/env and independent session. Parent closes only its log descriptor; all spawn Exceptions update failure records and rethrow with cause. PID/start-time/PGID attachment follows without waiting; a completion runner changes lifecycle and audit ordering.
- **K1.30** — Plugin wrapper chooses POSIX shared/exclusive or Windows `LK_LOCK`/`LK_RLCK` one-byte lock. Windows seek0 then restores prior file position only after success; unlock mirrors this sequence. MCP wrapper has different offset behavior; common helper is not proven identical under failure. Exact site `_lock_file`: `_fcntl.flock`(fp.fileno(), mode).
- **K1.31** — Plugin wrapper chooses POSIX shared/exclusive or Windows `LK_LOCK`/`LK_RLCK` one-byte lock. Windows seek0 then restores prior file position only after success; unlock mirrors this sequence. MCP wrapper has different offset behavior; common helper is not proven identical under failure. Exact site `_lock_file`: `_msvcrt.locking`(fp.fileno(), mode, 1).
- **K1.32** — Plugin wrapper chooses POSIX shared/exclusive or Windows `LK_LOCK`/`LK_RLCK` one-byte lock. Windows seek0 then restores prior file position only after success; unlock mirrors this sequence. MCP wrapper has different offset behavior; common helper is not proven identical under failure. Exact site `_unlock_file`: `_fcntl.flock`(fp.fileno(), `_fcntl.LOCK_UN`).
- **K1.33** — Plugin wrapper chooses POSIX shared/exclusive or Windows `LK_LOCK`/`LK_RLCK` one-byte lock. Windows seek0 then restores prior file position only after success; unlock mirrors this sequence. MCP wrapper has different offset behavior; common helper is not proven identical under failure. Exact site `_unlock_file`: `_msvcrt.locking`(fp.fileno(), `_msvcrt.LK_UNLCK`, 1).
- **K1.34** — Admission uses exclusive nonblocking POSIX flock with BlockingIOError=>False, explicit reverse unlock+close, and slot release clears record before unlock while `unlock_keeping_record` preserves it. OSError suppression/close precedence and no-flock cap refusal are caller-owned lifecycle semantics. Exact site `_lock_now`: `_fcntl.flock`(fd, `_fcntl.LOCK_EX` | `_fcntl.LOCK_NB`).
- **K1.35** — Admission uses exclusive nonblocking POSIX flock with BlockingIOError=>False, explicit reverse unlock+close, and slot release clears record before unlock while `unlock_keeping_record` preserves it. OSError suppression/close precedence and no-flock cap refusal are caller-owned lifecycle semantics. Exact site `_unlock_admission`: `_fcntl.flock`(fd, `_fcntl.LOCK_UN`).
- **K1.36** — Admission uses exclusive nonblocking POSIX flock with BlockingIOError=>False, explicit reverse unlock+close, and slot release clears record before unlock while `unlock_keeping_record` preserves it. OSError suppression/close precedence and no-flock cap refusal are caller-owned lifecycle semantics. Exact site `_Slot.release`: `_fcntl.flock`(fd, `_fcntl.LOCK_UN`).
- **K1.37** — Admission uses exclusive nonblocking POSIX flock with BlockingIOError=>False, explicit reverse unlock+close, and slot release clears record before unlock while `unlock_keeping_record` preserves it. OSError suppression/close precedence and no-flock cap refusal are caller-owned lifecycle semantics. Exact site `_Slot.unlock_keeping_record`: `_fcntl.flock`(fd, `_fcntl.LOCK_UN`).
- **K1.38** — Linked-worktree Git probe keeps resolved cwd, 5s timeout, locale text and two output lines. OSError/SubprocessError or malformed/nonzero output returns empty tuple; same-dir exclusion and existing-directory checks stay after probe. A generic root resolver or group runner would change admission/output behavior.
- **K1.39** — `_safe_join` deliberately uses pathlib.PurePosixPath lexical flavor for PurePosixPath(rel); it performs no filesystem resolution. Native platform Path/root/containment helpers would change cross-platform lexical parsing and admission, so retain this flavor primitive.
- **K1.40** — `_resolve_produced` deliberately uses pathlib.PurePosixPath lexical flavor for PurePosixPath(rel); it performs no filesystem resolution. Native platform Path/root/containment helpers would change cross-platform lexical parsing and admission, so retain this flavor primitive.
- **K1.41** — Completion-evidence Git probe keeps cwd, native timeout and locale-strict text. It returns native returncode plus stripped stdout; only OSError/TimeoutExpired map to (1,empty). Other errors are not broadened; no new session, group kill, decoder or wrapper error is introduced.
- **K1.42** — Lineage reading uses a TemporaryDirectory for copied store+journal and consistency checks, returning MOVED versus UNREAD on different races/errors. Context cleanup is resource lifetime, not target-file publication; replacing it would alter SQLite copy/read isolation.
- **K1.43** — Studio intentionally changes SIGPIPE to `SIG_IGN` when supported so disconnected client writes raise BrokenPipeError instead of killing the daemon. A restoring temporary handler has a different lifetime/policy.
- **K1.44** — Docker pull keeps captured bytes, no imposed timeout, inherited terminal session and KeyboardInterrupt return0 with stopping text. Nonzero is a decoded warning followed by cached-image fallback; a new 30s/group runner would change user interrupt and long-pull behavior.
- **K1.45** — Foreground docker run keeps inherited stdin/stdout/stderr and terminal session, no timeout, check=False and Docker exit status. KeyboardInterrupt runs docker stop then returns0. Detachment/capture/group killing or bounded completion changes the interactive lifecycle.
- **K1.46** — Vite launch keeps live Popen/text stdout with merged stderr, cwd/env and new session; only FileNotFoundError maps to warning/None. Readiness reads that same stream then returns (proc,url); a completed ProcessRun cannot preserve stream ownership and later shutdown.
- **K1.47** — Docker stop is deliberately inside the foreground KeyboardInterrupt handler, captures bytes and has no bound or local replacement handler. Its own failure remains observable before the outer return0; new timeout/session or swallowing changes shutdown semantics.
- **K1.48** — npm install keeps frontend cwd, captured bytes, no timeout and check=True. CalledProcessError or FileNotFoundError print the existing warning and returnFalse; no new session or group timeout is added.
- **K1.49** — Vite build keeps frontend cwd, captured bytes, no timeout and check=True. Only CalledProcessError is caught for warning/False; FileNotFoundError still propagates here. A generalized runner must not normalize this distinct error envelope.
- **K1.50** — `public_project` deliberately uses pathlib.PureWindowsPath lexical flavor for PureWindowsPath(value); it performs no filesystem resolution. Native platform Path/root/containment helpers would change cross-platform lexical parsing and admission, so retain this flavor primitive.
- **K1.51** — Scheduled flow YAML uses mkstemp0600, caller-produced YAML text and returns argv carrying the temporary path. Only write Exception deletes immediately; later scheduled-process owner cleans the path. A target atomic helper would change path lifetime, cleanup and launch contract.
- **K1.52** — ps snapshot keeps 2s timeout, locale text, check=False and inherited session. Returns stdout even for nonzero exit; every Exception gives empty string, which is the callers unknown-liveness evidence. Group/session or decoding changes would affect health classification.
- **K1.53** — Private MCP text writer mkdirs first, mkstemp0600, native text write then replace, BaseException unlink cleanup, and post-publication chmod0600 on every save. Generic mode/encoding/Path return or relocation would change repair/failure ordering and inode publication.
- **K1.54** — Queued resume config needs a surviving unique mkstemp0600 path, stdlib `sort_keys` with compact separators, BaseException unlink/rethrow and returned str path. Worker later reads+unlinks and parent owns fallback cleanup; a generic published target changes ownership/lifetime/bytes.
- **K1.55** — Queued resume holds an exclusive POSIX lock for the entire child lifetime,0600 fd under hashed branch lock directory, NOFOLLOW when available; missing fcntl yields unlocked on Windows. Local StateDB absence raises exact RuntimeError; generalized Windows advisory locking changes failure/signal-unwind behavior. Exact site `_branch_resume_lock`: fcntl.flock(`lock_file.fileno`(), `fcntl.LOCK_EX`).
- **K1.56** — Queued resume holds an exclusive POSIX lock for the entire child lifetime,0600 fd under hashed branch lock directory, NOFOLLOW when available; missing fcntl yields unlocked on Windows. Local StateDB absence raises exact RuntimeError; generalized Windows advisory locking changes failure/signal-unwind behavior. Exact site `_branch_resume_lock`: fcntl.flock(`lock_file.fileno`(), `fcntl.LOCK_UN`).
- **K1.57** — Existing shared tool runner already owns shell choice/new session/env stripping+marker, live Popen pipes, concurrent capped byte drains and witnessed teardown. Spawn Exception becomes Execution error dict; TimeoutExpired triggers group kill then unbounded wait and specific timeout label/output dict. Keep exact cap/decode/truncation markers and joins; proposed ProcessRun, ${ pattern or bounded reap is non-neutral.
- **K1.58** — ast-grep keeps argv/cwd, 30s timeout, locale-strict text/newline translation and inherited session. TimeoutExpired and OSError produce distinct exact AstSearchResponse messages; exit0/1 versus2+, JSON and empty-output handling stay local. UTF-8 replacement or group timeout would change output/error behavior.
- **K1.59** — ruff keeps argv/cwd, 30s timeout, locale-strict text/newline translation and inherited session. TimeoutExpired and OSError produce distinct exact CodeCheckResponse messages; violations exit1 is distinct from errors, and parsing remains caller-owned. No new session/group-result/decoder equivalence is established.

**Operations**

- **1.1** — pathlib.Path; K1.1.
- **1.2** — pathlib.Path.home; K1.2.
- **1.3** — subprocess.run; K1.3.
- **1.4** — pathlib.Path.cwd; K1.4.
- **1.5** — subprocess.run; K1.5.
- **1.6** — subprocess.run; K1.6.
- **1.7** — subprocess.run; K1.7.
- **1.8** — tempfile.mkstemp; K1.8.
- **1.9** — fcntl.flock; K1.9.
- **1.10** — signal.signal; K1.10.
- **1.11** — subprocess.Popen; K1.11.
- **1.12** — fcntl.flock; K1.12.
- **1.13** — tempfile.mkstemp; K1.13.
- **1.14** — fcntl.flock; K1.14.
- **1.15** — fcntl.flock; K1.15.
- **1.16** — fcntl.flock; K1.16.
- **1.17** — fcntl.flock; K1.17.
- **1.18** — tempfile.mkdtemp; K1.18.
- **1.19** — tempfile.TemporaryDirectory; K1.19.
- **1.20** — tempfile.mkstemp; K1.20.
- **1.21** — subprocess.Popen; K1.21.
- **1.22** — subprocess.run; K1.22.
- **1.23** — fcntl.flock; K1.23.
- **1.24** — msvcrt.locking; K1.24.
- **1.25** — fcntl.flock; K1.25.
- **1.26** — msvcrt.locking; K1.26.
- **1.27** — subprocess.run; K1.27.
- **1.28** — subprocess.run; K1.28.
- **1.29** — subprocess.Popen; K1.29.
- **1.30** — fcntl.flock; K1.30.
- **1.31** — msvcrt.locking; K1.31.
- **1.32** — fcntl.flock; K1.32.
- **1.33** — msvcrt.locking; K1.33.
- **1.34** — fcntl.flock; K1.34.
- **1.35** — fcntl.flock; K1.35.
- **1.36** — fcntl.flock; K1.36.
- **1.37** — fcntl.flock; K1.37.
- **1.38** — subprocess.run; K1.38.
- **1.39** — pathlib.PurePosixPath; K1.39.
- **1.40** — pathlib.PurePosixPath; K1.40.
- **1.41** — subprocess.run; K1.41.
- **1.42** — tempfile.TemporaryDirectory; K1.42.
- **1.43** — signal.signal; K1.43.
- **1.44** — subprocess.run; K1.44.
- **1.45** — subprocess.run; K1.45.
- **1.46** — subprocess.Popen; K1.46.
- **1.47** — subprocess.run; K1.47.
- **1.48** — subprocess.run; K1.48.
- **1.49** — subprocess.run; K1.49.
- **1.50** — pathlib.PureWindowsPath; K1.50.
- **1.51** — tempfile.mkstemp; K1.51.
- **1.52** — subprocess.run; K1.52.
- **1.53** — tempfile.mkstemp; K1.53.
- **1.54** — tempfile.mkstemp; K1.54.
- **1.55** — fcntl.flock; K1.55.
- **1.56** — fcntl.flock; K1.56.
- **1.57** — subprocess.Popen; K1.57.
- **1.58** — subprocess.run; K1.58.
- **1.59** — subprocess.run; K1.59.

## Factories and type predicates

**Contracts**

- **K2.1** — map(Path,roots) performs deferred native construction; it is not a direct Path call and is separately recorded.
- **K2.2** — isinstance(...,Path) is a type predicate, not construction/IO or an extractable helper call.

**Operations**

- **2.1** — map(Path, roots); K2.1.
- **2.2** — isinstance(value, Path); K2.2.
- **2.3** — isinstance(resource, Path); K2.2.

## Untyped bound-method candidates

Method spelling alone does not establish a pathlib receiver. These separate references retain their existing consumer behavior and are not added to the direct-native-call count.

**Contracts**

- **K3.1** — Lexical attribute method candidate in issue support; receiver is not inferred to be pathlib solely from method name. Native direct-call337 total excludes these bound operations.

**Operations**

- **3.1** — .resolve; K3.1.
- **3.2** — .`relative_to`; K3.1.
- **3.3** — .replace; K3.1.
- **3.4** — .open; K3.1.
- **3.5** — .`read_text`; K3.1.
- **3.6** — .`write_text`; K3.1.
- **3.7** — .unlink; K3.1.
- **3.8** — .chmod; K3.1.
- **3.9** — .`is_relative_to`; K3.1.

## Excluded aliases

The following expressions refer to application objects named signal, rather than the standard-library module.

**Contracts**

- **K4.1** — Local DispatchSignal instance serialization, not stdlib signal.
- **K4.2** — Relative lionagi.session.signal lifecycle class/function import, not stdlib signal.
- **K4.3** — Loop dictionary named signal, not stdlib signal.

**Operations**

- **4.1** — Application signal object; excluded from stdlib calls; K4.1.
- **4.2** — Application signal object; excluded from stdlib calls; K4.2.
- **4.3** — Application signal object; excluded from stdlib calls; K4.3.

## Baseline address index

Entries use `contract=lines`, or `line:column` for JSON calls. `U:` identifies unit rows; `×N` preserves occurrences. Ranges cover original spans only. All positions use the baseline.

- `lionagi/_flow_spec.py`: `1.1=64`.
- `lionagi/_paths.py`: `1.1=20,47,88; 1.2=20,110; 1.3=80; 1.4=109; subprocess=80`.
- `lionagi/agent/factory.py`: `1.1=539,647,688,704,1146; 1.2=718; 1.4=539,647,704; 1.5=1266; 3.1=1029×2; 3.4=1387; 3.5=1146; 3.7=1334,1390; U:B5=1265–1286; subprocess=1266`.
- `lionagi/agent/hooks.py`: `1.1=97,104,116`.
- `lionagi/agent/permissions.py`: `U:B5=14; U:H9=14`.
- `lionagi/agent/settings.py`: `1.1=53; 1.2=44; 1.4=53; 3.3=240; U:B4=222`.
- `lionagi/agent/spec.py`: `1.1=53,162; 1.4=53`.
- `lionagi/casts/pack.py`: `1.1=50`.
- `lionagi/casts/pattern.py`: `1.1=76`.
- `lionagi/casts/profile.py`: `1.1=62,78`.
- `lionagi/cli/_code_identity.py`: `1.1=283,332,365,567; 1.6=136; 3.1=332; U:B5=127–145; subprocess=136`.
- `lionagi/cli/_context_from.py`: `1.1=113,161`.
- `lionagi/cli/_mcp_resolve.py`: `1.1=98,159,162,168,170,186`.
- `lionagi/cli/_project.py`: `1.2=80; 1.4=21; 1.7=110; subprocess=110`.
- `lionagi/cli/_runs.py`: `1.1=329,617,760,771,772,774; 1.4=625; 1.8=103; 1.9=138; 3.1=198,199,329,334,335,617,625; 3.2=201; 3.3=107; 3.4=137,375,1757,2294; 3.5=313,412,541,564,709,756; 3.6=732; 3.7=110; U:B1=732; U:B3=115–152; U:H3=91–111; U:N1=192–206; U:N2=91–111`.
- `lionagi/cli/_util.py`: `1.1=111; 3.3=219×3; U:H5=138–149; U:N4=138–149`.
- `lionagi/cli/agent.py`: `1.1=220,601,1094,1269,2471`.
- `lionagi/cli/doctor.py`: `1.1=65`.
- `lionagi/cli/hooks.py`: `1.1=233,234,435; 1.4=233,435`.
- `lionagi/cli/kill.py`: `1.1=637; 3.1=641,642; 3.5=645,738; U:N2=727–753; U:N4=20`.
- `lionagi/cli/machine.py`: `1.1=453`.
- `lionagi/cli/machine_schedule.py`: `1.1=183; 1.4=396,397; 3.1=340; 3.3=170; 3.5=332; U:B6=401`.
- `lionagi/cli/main.py`: `1.10=302`.
- `lionagi/cli/mirror.py`: `1.1=202,213,881,882,944,946; 1.2=25,26,33; 3.1=612,881,882,944,946; 3.2=33; 3.3=298; 3.4=365,430,634; 3.5=242,292; 3.6=297; U:B1=286–298`.
- `lionagi/cli/monitor.py`: `1.1=926; 3.5=290; U:B6=1561–1578; U:H10=1131–1148; U:N4=34–37; U:N7=1131–1148`.
- `lionagi/cli/orchestrate/__init__.py`: `1.1=1189,1233; 1.11=1238; 1.2=191,1191; 1.4=1191; 3.1=277,278,1189,1191×2; 3.2=279,1193; 3.3=92,315,339; U:N1=1186–1203; subprocess=1238`.
- `lionagi/cli/orchestrate/_checkpoint.py`: `3.3=167; 3.5=171; 3.6=166; U:B1=157–167`.
- `lionagi/cli/orchestrate/_common.py`: `1.1=75,106`.
- `lionagi/cli/orchestrate/_finalize.py`: `1.1=65,143; 1.12=152; 3.4=150; U:B3=15`.
- `lionagi/cli/orchestrate/_harvest.py`: `1.1=127,128`.
- `lionagi/cli/orchestrate/_manifest.py`: `1.1=167,315,362`.
- `lionagi/cli/orchestrate/_orchestration.py`: `1.1=651,657,1042,1353,1712,1734; 1.4=1353; 3.1=1353×2,1363; 3.2=1736; 3.3=116; 3.6=1802; U:B1=1802`.
- `lionagi/cli/orchestrate/_round_records.py`: `1.1=97,101,115; 1.13=107; 3.3=113; 3.4=127; 3.7=115; U:B2=104–116`.
- `lionagi/cli/orchestrate/flow.py`: `1.1=2402,3051,3059; 1.4=2402`.
- `lionagi/cli/skill.py`: `1.1=17`.
- `lionagi/cli/status.py`: `1.4=636`.
- `lionagi/cli/team.py`: `1.14=89; 1.15=48; 1.16=108; 1.17=52; U:B3=9`.
- `lionagi/config.py`: `1.1=260`.
- `lionagi/dispatch/outbox.py`: `4.1=190; U:B4=145–156`.
- `lionagi/engines/coding.py`: `1.1=221,384,539,541,671,925,1038,1042,1046,1066; 1.4=338,539; 3.2=697,947,1044,1046; 3.6=399,402,1086; 3.9=697,947; U:B5=1127–1133; U:H9=1127–1133`.
- `lionagi/engines/hypothesis.py`: `1.1=638`.
- `lionagi/hooks/external.py`: `1.1=152,161,253; 1.18=252; 1.4=388,774; 3.1=152,161; 3.4=219,255,272; U:B4=472–604; U:H6=461–500; U:N5=461–500`.
- `lionagi/libs/file/process.py`: `1.1=24,75,94,126`.
- `lionagi/libs/path_safety.py`: `1.1=39,65,159; 3.1=43,100,101,132,185,186; 3.2=45,103,134,188; U:H2=98–109; U:N1=98–109`.
- `lionagi/libs/schema/load_pydantic_model_from_schema.py`: `1.1=329; 1.19=328`.
- `lionagi/ln/__init__.py`: `U:N4=120–125`.
- `lionagi/ln/_paths.py`: `3.1=12; U:H1=10–12; U:N1=10–12`.
- `lionagi/ln/_proc.py`: `U:B4=736–842; U:H7=806–842; U:N4=23`.
- `lionagi/ln/_utils.py`: `3.1=151,161,189,208; 3.3=448,466; U:N1=144–189`.
- `lionagi/ln/concurrency/utils.py`: `U:N7=125`.
- `lionagi/mcp/_child_actor.py`: `1.20=239`.
- `lionagi/mcp/_notify_hook.py`: `1.21=413; subprocess=413`.
- `lionagi/mcp/_terminal_cause.py`: `1.1=161,179; 3.5=182; 3.6=161; U:B1=161; U:B2=161–163`.
- `lionagi/mcp/config.py`: `1.1=110`.
- `lionagi/mcp/dispatch.py`: `1.1=541,768; 1.22=934; subprocess=934`.
- `lionagi/mcp/jobs.py`: `1.1=756,787,1225,1502,1799; 1.23=436; 1.24=440; 1.25=445; 1.26=449; 1.27=592; 1.28=1161; 1.29=1582; 2.1=1226; 3.1=1799; 3.2=796; 3.3=401,2973; 3.4=493,1981; 3.5=557,625,756,1965; 3.6=351,400,431,1499; 3.7=341,408,1106; 3.9=1226; U:B1=389–409; U:B5=591–609; U:H4=434–449; U:H5=633–655; U:N3=48–58; U:N4=31–38; subprocess=592,1161,1582`.
- `lionagi/mcp/projection.py`: `1.1=543`.
- `lionagi/mcp/roster.py`: `1.1=38; 1.2=46`.
- `lionagi/operations/_visualize_graph.py`: `1.1=271`.
- `lionagi/operations/run/run.py`: `3.7=800; U:B1=50–67`.
- `lionagi/plugins/_user_settings.py`: `1.2=52; 1.30=87; 1.31=93; 1.32=99; 1.33=104; 3.4=163; U:H4=84–105; U:N3=24–33`.
- `lionagi/plugins/discovery.py`: `1.1=78,100; 3.1=83,85; 3.2=85; U:N1=67–87`.
- `lionagi/plugins/manifest.py`: `1.1=68`.
- `lionagi/plugins/registry.py`: `1.1=499,513`.
- `lionagi/plugins/trust.py`: `1.1=87`.
- `lionagi/providers/_cli_subprocess.py`: `1.1=1248,1343,1344; 1.2=1342; 1.4=1240; 3.3=778; U:H5=52–80; U:N4=26–33`.
- `lionagi/providers/_secret_resolution.py`: `3.3=221,309; U:B4=194–211`.
- `lionagi/providers/_subagent_admission.py`: `1.34=505; 1.35=553; 1.36=391; 1.37=401`.
- `lionagi/providers/openai/_codex_profile.py`: `1.1=265,267,281,346×2; 1.2=285`.
- `lionagi/providers/openai/_codex_project_layers.py`: `1.1=82,130,132,138,145,146,165`.
- `lionagi/providers/openai/codex.py`: `1.1=1146; 1.38=1132; subprocess=1132`.
- `lionagi/service/accounts/_ledger.py`: `1.1=182; 1.2=65`.
- `lionagi/service/connections/mcp_wrapper.py`: `1.1=1721`.
- `lionagi/session/branch.py`: `4.2=479,495,503,1201,1242,1250`.
- `lionagi/session/observer.py`: `4.2=356`.
- `lionagi/state/_mirror_common.py`: `1.1=228,231`.
- `lionagi/state/artifact_verifier.py`: `1.39=70; 1.40=128; U:N1=63–82`.
- `lionagi/state/codex_mirror.py`: `1.1=225`.
- `lionagi/state/completion_evidence.py`: `1.41=25; U:B5=23–34; subprocess=25`.
- `lionagi/state/db.py`: `1.1=162,272,944,945,8147,8221; 1.42=8209`.
- `lionagi/state/engine.py`: `1.1=325,911; 2.2=315`.
- `lionagi/state/lifecycle/notify_settings.py`: `3.3=414; U:H7=472–552; U:N6=434–442`.
- `lionagi/state/lifecycle/schedules.py`: `1.1=71`.
- `lionagi/studio/_traceback_dump.py`: `1.1=118`.
- `lionagi/studio/app.py`: `1.1=488`.
- `lionagi/studio/cli.py`: `1.1=58,195,1477,1489,1760,1966,2016,2246,2327; 1.2=55,591,627; 1.4=1503,1511,1967,2327; 1.43=446; 1.44=596; 1.45=678; 1.46=1131; 1.47=681; 1.48=879; 1.49=892; subprocess=596,678,681,879,892,1131`.
- `lionagi/studio/config.py`: `1.1=20,79,107,165,367,383,614,664,677; 1.2=370,665; 2.3=134`.
- `lionagi/studio/operator/application_mcp.py`: `1.1=432,433; 1.50=434`.
- `lionagi/studio/operator/engine.py`: `1.1=358,595; 1.4=560,708; 3.1=560,595,708; 3.5=748,812; U:N2=340–363`.
- `lionagi/studio/operator/redact.py`: `1.1=76,77; 1.50=78`.
- `lionagi/studio/operator/run_progress.py`: `4.3=465,468`.
- `lionagi/studio/operator/store.py`: `1.1=215`.
- `lionagi/studio/reconnect.py`: `1.1=87,177`.
- `lionagi/studio/scheduler/engine.py`: `1.1=351; 1.4=57,411`.
- `lionagi/studio/scheduler/github.py`: `U:B4=241–256`.
- `lionagi/studio/scheduler/subprocess.py`: `1.1=482; 1.51=728; 3.3=332,702; 3.7=733,984; U:H6=809–824; U:N5=809–824`.
- `lionagi/studio/services/_db.py`: `1.1=46`.
- `lionagi/studio/services/_path_safety.py`: `1.1=22; 1.2=23`.
- `lionagi/studio/services/admin.py`: `1.1=96,143,576,785; 1.52=227; 3.5=213,218; U:N4=401; subprocess=227`.
- `lionagi/studio/services/agents.py`: `3.1=131,175; 3.5=62,114,146,332; 3.6=298,381; 3.7=319; U:B1=298`.
- `lionagi/studio/services/definitions.py`: `3.2=70; 3.6=467,520; U:B1=467`.
- `lionagi/studio/services/hooks_library.py`: `3.5=87; 3.6=102; U:B1=102`.
- `lionagi/studio/services/lifecycle.py`: `1.1=743`.
- `lionagi/studio/services/mcp_servers.py`: `1.53=142; 3.3=146,293; 3.7=149; 3.8=151; U:H3=129–151; U:N2=129–151`.
- `lionagi/studio/services/operator_hooks.py`: `3.5=80; 3.6=94,121; U:B1=94`.
- `lionagi/studio/services/playbooks.py`: `1.1=32; 3.1=32,67,93; 3.5=57,81,118,143,255; 3.6=301; U:B1=301`.
- `lionagi/studio/services/plugins.py`: `1.1=18,185; 1.2=37`.
- `lionagi/studio/services/redaction.py`: `1.1=96`.
- `lionagi/studio/services/run_resume.py`: `1.54=282`.
- `lionagi/studio/services/run_resume_worker.py`: `1.1=27,51; 1.55=59; 1.56=63; 3.4=57; 3.5=29; 3.7=33; U:B3=40–63; U:B6=116–128`.
- `lionagi/studio/services/runs.py`: `1.1=130,299,557`.
- `lionagi/studio/services/schedule_declaration.py`: `1.1=415,1057,1073,1134,1309,1446`.
- `lionagi/studio/services/schedule_export.py`: `1.1=339,356`.
- `lionagi/studio/services/schedules.py`: `1.1=140`.
- `lionagi/studio/services/shows.py`: `1.1=132`.
- `lionagi/studio/services/stats.py`: `1.1=253`.
- `lionagi/studio/services/workflow_compile.py`: `1.1=132,147`.
- `lionagi/testing/_env.py`: `1.1=27`.
- `lionagi/testing/_script.py`: `1.1=86`.
- `lionagi/testing/loaders.py`: `1.1=14,21; 3.1=14,37,38; U:N1=23–50`.
- `lionagi/tools/_subprocess.py`: `1.57=65; U:B5=19; U:H8=49–137; U:H9=19; subprocess=65`.
- `lionagi/tools/code/ast_search.py`: `1.58=93; U:B5=91–113; U:N1=166; subprocess=93`.
- `lionagi/tools/code/bash.py`: `U:B5=69–89`.
- `lionagi/tools/code/check.py`: `1.59=142; U:B5=140–171; U:N1=243; subprocess=142`.
- `lionagi/tools/code/nav.py`: `1.1=58; 3.5=58; U:N1=181`.
- `lionagi/tools/code/search.py`: `1.1=96,99,121,204`.
- `lionagi/tools/coding.py`: `3.1=161,197,266,416,440; 3.3=246; 3.5=215; 3.6=185; U:B5=536–571; U:N1=364`.
- `lionagi/tools/file/editor.py`: `3.3=176; 3.4=34; 3.5=143; 3.6=119; U:N1=203`.
- `lionagi/tools/file/reader.py`: `U:N1=188`.
- `lionagi/tools/sandbox.py`: `1.1=42,86,131,158,160`.
- `lionagi/tools/sandbox_backend.py`: `1.1=231,268`.
