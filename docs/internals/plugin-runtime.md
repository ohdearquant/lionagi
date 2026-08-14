# Plugin trust and the external hook lifecycle

A lionagi plugin is a directory bundle under `.lionagi/plugins/<name>/` carrying
a `plugin.yaml` manifest. Nothing it declares — a tool, a hook, an agent
profile, a playbook — runs until a human has explicitly trusted that exact
content. This document covers how that trust is computed and stored, and how
a trusted (or built-in) hook actually gets executed once approved.

## Trust model

Trust is content-pinned, not declaration-pinned. `compute_trust_hashes()`
(`lionagi/plugins/trust.py`) hashes the canonical-JSON manifest plus every
file the manifest declares — tool/provider targets, hook binaries, agent
profile files, playbook files, pack data files. `trust_plugin()` persists
those hashes; `trust_state()` recomputes them on every load and returns
`CHANGED` the instant any one of them differs, `UNTRUSTED` if there's no
record at all, `TRUSTED` only when every hash still matches. There is no
partial trust — one changed file reverts the whole plugin.

Trust records live in `~/.lionagi/settings.yaml`, always user-level, never
project-level. A repository cannot self-trust a plugin it carries by
committing a settings line; only the human on the machine approves. The
record also pins the bundle's resolved directory path, which is what garbage
collection checks for presence.

`gc_trust_records()` prunes a trust record only when its bundle directory is
confirmed gone — not when the manifest merely fails to parse. A `plugin.yaml`
mid-edit or hit by a transient read error is not the same as uninstalled, and
losing its trust record over a blip would force re-approval for no reason.
The presence check also closes a resurrection path: without it, a stale
record for a genuinely removed bundle would silently re-trust a different
bundle that later reappears under the same name and happens to hash the same
— exactly what content-pinning exists to prevent.

`trust_plugin()` raises `FileNotFoundError` rather than trusting a partial
bundle: if a declared capability file can't be read, there's nothing to pin
a hash to, and trusting is pinning content — a bundle missing a file it
declares can't be trusted.

## Settings lock

Every mutator that touches `~/.lionagi/settings.yaml` — GC, trust, plugin
enable/disable — goes through `locked_user_settings()`
(`lionagi/plugins/_user_settings.py`), a single exclusive POSIX lock (`flock`
on POSIX, `msvcrt.locking` on Windows) held for the entire read-modify-write.
That is the one choke point that makes concurrent writers safe: without it, a
writer's stale in-memory snapshot could silently clobber another writer's
change between its read and its write.

The context manager yields the parsed settings dict for the caller to mutate
in place, and writes back only if the dict actually changed — a no-op pass
touches neither the file's mtime nor a concurrent reader. It opens the file
with `O_CREAT` but deliberately never `O_TRUNC`: truncating before the lock is
held could blow away content a racing creator already committed, so
truncation only happens after the lock is acquired, immediately before the
rewrite.

`read_user_settings()` takes a shared lock for a point-in-time snapshot, safe
against a concurrent writer's truncate-then-rewrite. `write_user_settings()`
is an unconditional whole-file rewrite under an exclusive lock — safe as a
standalone call, but its lock only spans the write itself, not any read that
preceded it, so two independent read/write pairs built on top of it can still
race each other. Anything that needs read-modify-write safety must use
`locked_user_settings()`, not compose `read_user_settings()` +
`write_user_settings()` itself.

## Hook lifecycle

An external hook (a plugin's declared `hooks_external:` entry, or a
project/user-authored one) is a wire contract, not an in-process callback.
`build_envelope()` (`lionagi/hooks/external.py`) serializes the event as JSON
to the hook's stdin; `_execute_hook()` spawns the command, exchanges the
envelope, and normalizes exit code + stdout into a `HookVerdict`: exit 0 with
non-empty, non-truncated stdout is parsed as a decision; exit 2 denies with
stderr as the reason; anything else (non-zero exit, spawn/IO error, timeout)
is a hook failure — denied on a blocking event, logged and passed through on
an advisory one. A truncated response is never parsed even if it looks
complete, since the retained prefix could coincidentally read as a valid but
wrong decision.

Trust for an *imported* hook command (one carrying a non-empty `source`, e.g.
`imported:claude`) is re-checked on every invocation, not cached from when
the adapter was built: `_trust_status()` (ADR-0048 D7) requires a record in
`trusted_hook_commands` whose argv hash, resolved executable path, and
content digest all match the command as it resolves *right now*. A stale
approval of `["./guard"]` does not carry over if the executable it resolves
to — or that executable's bytes — changed since approval. This closes a gap
where argv-only hashing would let a different repository's `./guard`, or a
later PATH-resolved one, run under someone else's approval. The resolution,
the trust-record match, and the private hash-verified copy that actually
gets executed all come from one `_prepare_trusted_execution` call, so a swap
between "approved" and "exec'd" can't win a race by re-resolving the command
a second time. A project/user-authored command (no `source`) has no separate
approval to bind against and always spawns directly in the resolving
directory.

Hook teardown is where lost events get reported. `MessagePersistRetryQueue`
(`lionagi/hooks/_message_retry.py`) preserves message order while retrying
failed atomic persistence; `flush_final()` makes one last attempt and, if
messages still didn't make it, logs an error — `flush()` alone only returns
`False`, which nothing here reads. Teardown reaches `flush_final()` more than
once (the hook bus flushes on `SESSION_END`, and run teardown flushes again
before reading completion evidence, unaware of each other), so the loss
report is deduped by pending count: a repeated identical count is the same
incident restated and stays quiet, a changed count is new information and
gets logged again.
