# ADR-0041: Agent specification and Branch construction boundary

- **Status**: Accepted, amended 2026-10-17, 2026-10-25, 2026-10-27, 2026-11-20 and 2026-11-21 (see
  Amendments 1 to 5); Amendment 6 (2026-11-22) accepted 2026-11-23; Amendment 7 (2026-11-23)
  Proposed; Amendment 8 (2026-11-23) Proposed
- **Kind**: Retrospective
- **Area**: agent-roles
- **Date**: 2026-07-09
- **Relations**: supersedes v0-0078, v0-0100

## Amendment 8 (2026-11-23) — a resume runs the MCP servers its first run was handed

**Status: Proposed.**

A new `li agent` branch records its MCP choice once, in `branch.metadata["mcp_launch"]`. A
resumed or continued leg that names neither `--mcp-config` nor `--no-mcp-config` replays that
choice (`docs/internals/cli.md`, the `mcp_launch` paragraph). No decision record described this
record until now. Its fields are:

- `mcp_config`: the absolute path of the file whose servers the run was handed, or null.
- `no_mcp_config`: whether the run was handed no servers.
- `launch_dir`: the directory the run resolved from. A resume searches from here, not from where
  it runs.
- `no_mcp_config_source`: `profile` when an agent profile's `mcp: none` made the choice. A resume
  cannot widen that choice with `--mcp-config`.
- `mcp_config_origin`: `submit_snapshot` when the file is the copy `agent.submit` took of a config
  it discovered, or `explicit` when the file was named on the command line.

The feature request reports that a resume "resolves the recorded path again and reads the file as
it is at that moment. Nothing compares it with what the first run read." Three kinds of record
reach a resume. A config `agent.submit` discovered is replayed from the copy it took at submit. A
person's `li agent` that names no flag records `mcp_config: null` and the launch directory, and its
resume searches that directory again. A named config is read again from the named file, which can
have changed since the first run and can sit where the leg itself can write. This amendment
changes the third kind only.

Decision:

- **A named set is copied at launch.** This covers a new branch whose set was named on the command
  line, whether by a person or by `agent.submit` passing its caller's `mcp_config` through. Once the
  leg has allocated its run directory, it writes `mcp-config.json` there, created new and readable
  by its owner alone (mode 0600). The copy holds the servers as read from the named file, before
  the run's own khive actor is attributed to them, so a resume attributes its own actor as the first
  run did. It is not a second read of the named file.
- **The record points at the copy.** `mcp_config` names the copy, and `mcp_config_origin` stays
  `explicit`. A new field, `mcp_config_named`, holds the absolute path the caller named. A resume
  reads `mcp_config` as before and never reads `mcp_config_named`, which is provenance only. A
  record whose `mcp_config_named` is not an absolute path, or whose origin is not `explicit`,
  refuses the resume before any config is read.
- **The copy is still a named set.** A resume hands it over as the whole set, as the first run had
  it: Claude gets `--strict-mcp-config`, and codex is given only those servers.
- **A resume copies it again.** A resume that replays a copy writes the set again into its own
  run directory and points `mcp_config` there, keeping `mcp_config_named`. Nothing in lionagi
  removes a run directory; a sweep outside it may, so the set lasts as long as the branch's newest
  run directory rather than its first. An automatic resume after a timeout replays the copy the
  same way rather than reading the named file again.
- **A copy that is gone refuses the resume.** The refusal names the file and the flags to go on
  with (`--mcp-config`, `--no-mcp-config`), as a recorded file that can no longer be read does
  today.
- **The record is written after the run directory exists.** The branch is persisted only after
  that point, so no branch can carry a record that points at a copy that was never written. A launch
  refused before its run directory exists (a profile's `scratch` or `deny_read` that the route
  cannot hold) leaves neither a copy nor a branch, as before. A branch with no record at all reads
  as before, and this amendment adds no way to reach that state.
- **Where the copy sits.** At `<lionagi home>/runs/<run id>/mcp-config.json`. No default write
  root holds the lionagi home, and the copy is among the run files a Claude Code child's file tools
  are denied by name (beside `run.json`, `checkpoint.json`, `branches` and `stream`). A Claude Code
  child's Bash is held to its working directory and `allowWrite`, and its file tools to the same
  roots by a hook; a codex child writes under its working directory, its granted roots and its
  scratch directory; an API model's write set under its working directory, the temporary directory,
  the uv cache and its granted roots. None of these reaches the copy unless the working directory or a granted
  root contains the lionagi home.
- **Records written before this amendment.** A record with origin `explicit` and no
  `mcp_config_named` keeps today's behaviour and reads the named file again.
- **`li o flow`.** Its checkpoint records a named config the same way (`mcp_config` and
  `mcp_config_origin: explicit`), and its resume reads the file again. The flow copies the set into
  its own run directory under the same rules, and its checkpoint gains `mcp_config_named`. That half
  is not implemented yet: until it is, a flow's resume reads the named file again.
- **`agent.submit` takes no copy of a named config itself.** ADR-0048 Amendment 1's sentence on
  this stays true of the submit layer, and the job record's `mcp_config` still names the caller's
  file.

Limits:

- A set found by searching the launch directory is searched again on every resume, by design. It
  is not pinned, and a config placed in that directory or above it after the first run reaches the
  resume.
- A branch recorded before this amendment has no copy. Its resumes read the named file as it is
  then, until the branch is started fresh. A resume that names an MCP flag uses that flag for that
  leg only and does not rewrite the record.
- A leg with no filesystem boundary can rewrite the copy, as it can the run's other files: a child
  whose sandbox is switched off, a codex child with `bypass_approvals`, and a Gemini CLI child,
  whose sandbox grants no write set this launcher can verify.
- The copy holds each server's `env` and headers as the named file did. They now also sit in the
  run directory, as a discovered config's copy already does in the job directory.
- A resume that names `--mcp-config` reads that file as it is then. The copy governs only a resume
  that names no MCP flag.
- A copy that cannot be written fails the launch after its run directory exists. No branch is
  persisted, and the run's `run.json` keeps the `running` entry it was allocated with, as for any
  other failure between allocating the run and starting the leg.

## Amendment 7 (2026-11-23) — an agent profile adds paths its child may not read

**Status: Proposed.**

The ask, in the words of the feature request ("Agent profiles: confine a leg's reads to its
working directory"): "`workload: heavy` keeps a Codex leg read-only and `light` gives
workspace-write with no network, but in both, reads reach anything the user can read." What it
wants: "A profile field (for example `read_roots: [cwd]` or `deny_read: [...]`) passed to each
provider's sandbox where it can be enforced, and a refusal at submit where it cannot, as `light`
already refuses for providers without a sandbox." This amendment answers the `deny_read` half.
An allow-list such as `read_roots` needs reads denied by default, which no route here has been
measured to do; it comes as its own amendment once that is measured, and the key is not added
before then.

Decision:

- **The profile key.** `deny_read: [path, ...]` in an agent profile's frontmatter. Each entry is
  an absolute path or starts with `~/`. A relative path, an entry with a `.` or `..` part, an
  entry holding a pattern character (`* ? [ ] { }`), and a value that is not a list of non-empty
  strings refuse the profile when it is loaded, and the message names the entry. Patterns are
  refused on every route, including those that could match one, so an entry always means the one
  path it names.
- **Links.** An entry whose existing part passes through a symbolic link to another path is
  refused, and the message prints the path it resolves to so the author can write that instead.
  On macOS `/tmp`, `/var` and `/etc` are links into `/private`, so an entry under them is refused
  this way. An entry that does not exist is accepted and denies the path if it appears. Doubled
  and trailing slashes are dropped, and an entry given twice is kept once.
- **Never read as absent.** A frontmatter the parser cannot read that names `deny_read`, and a
  `deny_read` key in a `---` block after the leading frontmatter, refuse the profile.
- **Tightening only, from any profile file.** The entries are added to the run's deny list after
  the `profiles.<name>.allow_read` exemptions are applied, so no exemption lifts one, and they are
  denied for editing too. They can stop a run and cannot widen one, so a profile file in a
  project's agents folder may carry them; exemptions stay limited to the profile files in the
  user's own agents folder, as before. What an exemption lifts is unchanged on every route.
- **The sandbox switch.** With the child sandbox switched off in `~/.lionagi/settings.yaml`, a
  run whose profile declares entries is refused, since nothing would hold them.
- **The probe record.** A codex launch with the `lionagi_child` permission profile needs a probe
  record for its binary. The record is keyed to the binary (resolved path, version and digest),
  not to the profile's content, and a profile's entries render through the same deny entries the
  probe measures, so they need no probe of their own.
- **Where it applies.** One row per launch surface and provider. A refusal names the reason shown
  here; a refusal "at launch" happens before the run directory exists.

| Launch surface | Provider | What holds the entries | Reason given when refused |
| --- | --- | --- | --- |
| `li agent`, and `agent.submit`, an agent schedule and a leg host's launch, which start `li agent` | Claude Code CLI, `heavy` or `light` | `sandbox.filesystem.denyRead` (Bash reads) and `Read` and `Edit` deny rules (the file tools) in the child's `--settings`; `light` removes the write tools and keeps the same sandbox | |
| the same | codex, `heavy` or `light` | deny entries of the `lionagi_child` permission profile. A working directory or write root that is, holds or lies inside an entry refuses the launch, as it does for every denied path | with `bypass_approvals`: codex's sandbox is off, so the entries would not hold |
| the same | Gemini CLI, on a host with `/usr/bin/sandbox-exec` | `DENY_READ_<n>` parameters of the macOS sandbox policy the CLI runs under | |
| the same | Gemini CLI, on a host without it | refused at launch | the CLI is held to the unreadable list only by the macOS sandbox, and this host has none |
| the same | an API model, `api_tools: write` | the macOS sandbox policy its commands run under | |
| the same | an API model, `api_tools: read` | refused at launch | its tools run in lionagi's process with no sandbox, so nothing denies a read |
| the same | any other CLI | refused at launch | the named provider has no child sandbox to hold them |
| `li o flow`, `li o fanout` and `li play`, the orchestrator's role | as the rows above for a CLI; an API model, whatever its tool set | as above; an API model is refused at launch | as above, at launch; an API model's tools are not built under a policy holding the entries |
| a worker's role in a flow, fan-out or play | as the rows above for a CLI; an API model, whatever its tool set | as above; an API model is refused | as above, when the worker is built, after the orchestrator's run directory exists; an API model's tools are not built under a policy holding the entries |
| an orchestration engine stage | any | refused when the stage's agent is built, before it counts against the engine's agent budget, when the role's profile declares `deny_read`, or fails to load and its file names `deny_read` or cannot be read | a stage takes only the model and effort from its role's profile, so nothing carries the entries to its child; a profile that does not load leaves the stage on its other settings |

Required checks:

- The key's accepted forms, each refused form naming its entry, a link refused with the resolved
  path printed, and an entry under `/tmp` refused naming where `/tmp` resolves where it is a link.
- A malformed, unreadable or misplaced `deny_read` refuses the profile; a project profile's
  entries are taken.
- Claude Code, the one route that reads exemptions: the rendered settings deny the entry while
  `allow_read` for the same profile names the same path, `heavy` and `light`, with a control
  showing that `allow_read` still lifts the path for a profile without the entry. Codex, the
  Gemini CLI and API models read no exemption: on each the rendered permission profile, macOS
  sandbox parameters or API-model policy contains the entry, with a control rendered without it.
- Each refusing route refuses with its reason. `li agent` refuses before the run directory exists
  for the Gemini CLI without the macOS sandbox and with the sandbox switched off, and so does an
  orchestrator whose model is the Gemini CLI without it. A worker whose model cannot hold the
  entries refuses when it is built, with a control showing the same worker builds when its role
  declares none. An engine stage refuses when its agent is built if its role declares entries,
  has a malformed `deny_read`, fails to load for another reason while its file names
  `deny_read`, or is a link whose target cannot be read; a stage whose role declares none
  builds, and so does one whose file fails to load the same way without naming `deny_read`. The sandbox switched off
  refuses the profile resolver, a Claude Code request, a codex request and a model build; codex
  refuses under `bypass_approvals`.
- On a macOS host where the sandbox can be applied, a command reads a control file and then a
  denied file in one run: the control read succeeds and the denied read fails.

Limits:

- Measured on codex 0.162.1 with the network on, a unix socket under a denied path accepted a
  connection. A `heavy` codex run therefore holds file reads under an entry but not a connection
  to a socket there. Which profiled codex launches may run on such a binary is decided in an
  ADR-0133 amendment, which measures the socket check once per network state.
- An engine stage is refused rather than given a role's entries, as the table says. Carrying the
  entries to a stage's model is a later change with its own row here.
- A macOS sandbox deny rule was measured, on a case-insensitive volume, to refuse a read by a
  spelling in another letter case than the name on disk, beside a control read in the same run.
  How Claude Code matches its `Read` and `Edit` deny rules against a path spelled in another
  letter case is not measured.
- The entries are checked when the profile is loaded for a launch. A link made at an entry's path
  after that is not seen by the check; the sandbox then denies the spelling the entry names.

## Amendment 6 (2026-11-22) — a per-run writable scratch directory for an agent

**Status: Accepted (2026-11-23),** on a format-6 codex sandbox probe record from codex 0.162.1 in
which `scratch_env` passed (the child's `TMPDIR`, `TMP` and `TEMP` each named the directory and the
file written under `$TMPDIR` landed in it), with `read_only_write_root` and `write_entry` passed
beside it.

The ask, in the words of the feature request: "A reviewer profile's sandbox has no writable
temporary folder, so a reviewer agent cannot run the project's tests to check a claim." What it
wants: "A per-run scratch folder that is writable inside an otherwise read-only sandbox, declared
in the profile and removed or kept per run."

The directory is made whatever the role's workload or sandbox mode, a read-only one being only
the case the request names: a light codex child has `/tmp` and `$TMPDIR` read-only as well, so a
light reviewer has no temporary directory either.

Decision:

- **The profile key.** `scratch: true` or `scratch: keep` asks for one; `false` or no key asks
  for none. Any other value refuses the profile, so a request is never read as absent.
- **One directory per run.** An `li agent` run of such a role makes `<run state
  directory>/scratch`, mode 0700, once its run directory exists and before its first turn, and
  records `scratch: {path, keep}` in run.json. A name already there refuses the run. The path
  reaches the Claude Code or codex request as the endpoint's runtime state, never its config, and
  `scratch_dir` is one of `RUNTIME_STATE_NAMES`: no saved branch or stored config carries it into
  a later run, and a run without scratch clears one left on its endpoint.
- **The Claude Code child.** The path is the one write root the run adds to the child sandbox's
  policy, in `sandbox.filesystem.allowWrite` and in the roots of the file-tool hook, added after a
  stored policy is resolved again so the settings' own write list cannot drop it. It is also the
  child's `TMPDIR`, `TMP` and `TEMP`. A scratch path that is a symbolic link, or not a directory,
  refuses the launch; one that is gone adds nothing and warns.
- **The codex child.** Under the child sandbox a codex child runs under the `lionagi_child`
  permission profile (ADR-0133), and the resolved path is one write entry in that table in every
  workload and mode: beside the other write entries of a light or heavy table, and as the only
  write entry of a read-only one. The commands the child runs get `TMPDIR`, `TMP` and `TEMP` from
  three `-c shell_environment_policy.set.<NAME>=<path>` overrides, dotted keys so that a `set`
  table of the user's is merged and not replaced, placed after every caller override and the
  profile because codex keeps the last `-c` of a key. Codex's own process keeps its environment,
  so a light table's `:tmpdir = read` still names the directory codex started with, never the
  scratch directory. A link or a non-directory refuses the launch and a gone path adds nothing
  and warns, as for Claude Code. `scratch_dir` is runtime state of the codex request as well, in
  no dump, schema or stored config of it.
- **The codex gate.** A codex launch with a scratch directory needs the probe record's
  `scratch_env` arm, beside the arms its table already needs: a read-only table with the entry
  needs `read_only_write_root`, and a light or heavy one rests on `write_entry`. The probe's
  `scratch_env` run starts from a light table with one write entry and carries the three
  overrides. Its child prints `TMPDIR`, `TMP` and `TEMP` and writes one file under `"$TMPDIR"`.
  The arm passes when all three equal the directory and the file is in it, and otherwise fails
  with a note naming the values the child printed and whether the file landed in the directory.
  The record moves to format 6, and a record of format 5 or earlier admits no launch. A record
  whose `scratch_env` arm did not pass refuses a scratch launch with that arm named; the same
  launch without a scratch directory still starts on that record.
- **Codex refusals.** Each names its reason. Before the run directory exists: `bypass_approvals`,
  which turns codex's sandbox off, and the child sandbox switched off, which leaves no permission
  table to hold the entry. When the command is built, after the run directory and its scratch
  exist: a scratch path that is, holds or lies inside a denied location or the probe record
  directory; for a light table, or a heavy one under `exclude_slash_tmp` or
  `exclude_tmpdir_env_var`, a scratch path inside `/tmp` or `$TMPDIR`, as written or resolved,
  which that table makes read-only; and a record without a passed `scratch_env` arm. The run
  state directory is under the lionagi home by default, so the `/tmp` and `$TMPDIR` case does not
  arise in a default setup.
- **Removal.** In the run's shielded teardown, after the child's process group has ended, `true`
  removes the directory by its recorded path. It is refused, and left exactly as it is, when it is
  a symbolic link, is not a directory, is not named `scratch`, or is not directly in the run
  directory. A link inside is removed as a link and what it points to is left alone. When the
  first removal leaves something, a directory inside being read-only for one, every directory
  under scratch is opened with `O_DIRECTORY` and `O_NOFOLLOW` and given owner access through that
  descriptor, so no link is followed on Linux or macOS, and removal runs once more. What is
  inside a directory its owner cannot open for reading is left and named. Anything the
  removal raises is recorded as its error, and the teardown after it still runs. run.json's
  `scratch` then reads `removed` and `removal_error`; the error names what was refused or left.
  `keep` leaves the directory and reads `removed: false` with no error.
- **Sockets.** macOS limits a unix socket path to 104 bytes, and a tool that makes a socket under
  `TMPDIR` in a deep run directory can pass it. The docs say so; no shorter directory is made in
  its place.
- **Where it applies.** One row per launch surface and provider. A refusal names the reason
  shown here; a refusal "at launch" happens before the run directory exists.

| Launch surface | Provider | Scratch | Reason given when refused |
| --- | --- | --- | --- |
| `li agent`, and `agent.submit`, an agent schedule and a leg host's launch, which start `li agent` | Claude Code CLI | made, as above | |
| the same | codex | made: one write entry in the permission profile table, with `TMPDIR`, `TMP` and `TEMP` set for the child's commands through `shell_environment_policy.set`, on a record whose `scratch_env` arm passed; removed or kept by the removal above, unchanged, which already covers a directory the sandboxed child could write while the teardown runs outside the sandbox | under `bypass_approvals` or with the child sandbox off, refused at launch: no permission profile would hold the entry; a path over a denied location, the probe record directory, or a `/tmp` or `$TMPDIR` the table makes read-only, or a record without a passed `scratch_env` arm, refused when the command is built |
| the same | Gemini CLI | refused at launch | the Gemini CLI's sandbox does not bound reads and grants no single writable directory this launcher can verify |
| the same | an API model | refused at launch | an API-model run's tools run under lionagi's own seatbelt policy, which has no scratch root yet |
| the same | any other CLI | refused at launch | the named provider has no scratch directory support |
| `li o flow`, `li o fanout` and `li play`, the orchestrator's role | any | refused at launch | a flow, fan-out or play does not give its orchestrator or workers a scratch directory yet |
| a worker's role in a flow, fan-out or play | any | refused when the worker's role is resolved, after the orchestrator's run directory exists | the same |
| an orchestration engine stage | any | does not apply: a stage takes only the model and effort of a role's profile | |

Required checks:

- The key reads `true`, `keep`, `false` and no key, and refuses every other value it is tested
  with; the roster reports it only when set.
- A Claude Code run has the directory during its turn and records it; at run end `true` removed
  it and `keep` kept it, and run.json says which.
- A symbolic link inside scratch, to a file and to a directory outside, is removed and both
  targets survive. A symbolic link put in place of scratch is left, its target survives, and
  run.json carries the named error, both from the removal itself and from a run.
- A path that is a file, is named otherwise, or is outside the run directory is refused and left.
  A read-only directory inside is removed, a directory its owner cannot open is left and named,
  and opening up a directory changes nothing a link inside it points to. A removal that raises
  something other than an `OSError` is recorded in run.json, and the teardown after it still
  runs.
- The child's settings gain exactly the scratch path as a write root and in the hook's roots; a
  scratch path that is a link refuses the launch; a gone one adds nothing. The child's three
  temporary-directory variables name it, with an inherited and a given environment, and a run
  without scratch leaves the environment as it was. The path is in no dump of the request or the
  endpoint and reaches the rebuilt request.
- A Gemini CLI run is refused with its reason and no run directory; the reason for an API model
  is the one in the table. A codex run under `bypass_approvals`, and one with the child sandbox
  off, is refused with its reason and no run directory. An orchestrator role is refused with no
  run directory, and a worker role when it is resolved.
- A codex launch's table holds the resolved scratch path as a write entry, and nothing else
  changes, for a light, a heavy workspace-write, a heavy read-only and a heavy launch that names
  no mode; a launch without scratch carries no entry. Only the read-only tables need
  `read_only_write_root`, and only a scratch launch needs `scratch_env`.
- The three `shell_environment_policy.set` overrides name the resolved path, after a caller's
  override of the same key and after the profile; a launch without scratch carries none of them.
- Each codex refusal above, with a control: a scratch path beside a denied location is granted,
  and a heavy workspace-write table, which leaves `/tmp` writable, grants a path that a light one
  refuses. A link or a file as the scratch path is refused; a gone one adds nothing.
- The `scratch_env` verdict passes when the three variables equal the directory and the file is
  in it, fails when any one variable differs, and fails when the file lands elsewhere. A record
  of format 5 admits no launch.
- A scratch launch on a record whose `scratch_env` arm did not pass is refused naming the arm,
  and the same launch without scratch starts on that record; a passing record admits it.

Limits:

- The seatbelt policy that API models and the Gemini CLI run under gets the scratch root in a
  later change to this same amendment, with Amendment 5's file-link rules covering the new root
  and a probe arm proving that a hard link from scratch to a file outside the roots is refused.
  Until then it is refused as the table says.
- The handling of a hard link from the scratch directory to a file outside the writable roots has
  not been measured, by Claude Code's own sandbox or by codex's, as it has not been for the
  working directory.
- `scratch_env` is measured on a light table. A heavy or read-only launch rests on it for the
  variables and on `write_entry` or `read_only_write_root` for the write.
- An automatic resume runs in a run directory of its own and makes its own scratch; the first
  run's directory goes at that run's end unless kept.
- A process the child left running after the run can still write into the directory while it is
  removed. Removal reports what it could not remove, and nothing retries it.
- Before Python 3.13, removal descends recursively and holds a descriptor per level, so a tree
  deeper than the open-file limit (256 by default on macOS) is left and named.
- A directory its owner cannot open for reading, mode 0300 for one, is not opened up, so what is
  inside it is left and named.
- A kept directory stays until its run directory is removed; nothing sweeps kept directories.

## Amendment 5 (2026-11-21) — a hard link out of the roots is refused by name, measured per volume

**Status: Accepted.**

This amendment replaces the sentence of Amendment 2's Limits that reads "Making a hard link to a
file outside the roots, or to a file in `.git`, is refused." It was measured on the internal disk
only. On a volume mounted with ownership ignored (`noowners`, the default for an external APFS
disk), the policy as Amendments 2 and 4 build it lets a confined command make both links: `ln` of
a file outside the roots, and of the working directory's `.git/config`, into the working directory
succeeds there, while a plain write, a write into `.git`, and a move into or out of the roots are
still refused, and so is a link of a workspace file to a name outside the roots. Seatbelt does
not judge the source of `link(2)` by the write rules on that volume.
It does judge the file-link operation itself: `(deny file-link)` alone refuses every link there.

Decision:

- **File-link rules.** As its last rules, after the network and terminal rules, so no later rule
  can allow what they deny, the policy renders
  `(deny file-link (require-not (require-any (subpath WRITABLE_ROOT_i) ...)))` and one
  `(deny file-link (subpath ...))` for each read-only path and each unreadable entry. A policy
  with no writable root renders `(deny file-link)`. Every policy built by `SeatbeltPolicy` carries
  them: the write set and the gemini-cli policy alike. A link inside a root to a file inside a root
  still works.
- **A probe per volume.** `python -m lionhub.tools.seatbelt_link_probe --scratch <dir>` makes, in
  a fresh directory under `<dir>`, a workspace with a `.git/config`, a file outside the workspace
  and a symlink to that file inside it, builds the write set's policy for that workspace, and runs
  five arms: a link to the outside file must be refused, a link to `.git/config` must be refused,
  a link through the symlink (which `ln` follows, and which a confined command can make) must be
  refused, a link of a workspace file to a name beside the outside file must be refused, and a
  link to a file in the workspace must work. A refusal arm passes only on a non-zero exit with
  the `Operation not permitted` text, no name made where the link was asked for, and the file it
  would name at the link count it had before. The scratch directory is refused inside `/tmp`,
  `/var/tmp`, the platform temp directory or `$TMPDIR`, which the policy makes writable. Without
  `--scratch` it uses `<lionagi home>/state/seatbelt-link-probe`.
- **The record.** The probe writes `LIONAGI_HOME/seatbelt-link-probes/volume-<UUID>.json`,
  keyed by the volume UUID `diskutil info` reports for the mount point `df -P` names. It carries
  the kernel (`uname` system, release and version), a digest of the measured policy and each
  arm's outcome and evidence. The digest is the SHA-256 of the policy's text with every option
  that renders rules turned on: its own number of writable roots and read-only paths, the
  network and terminal rules, and one unreadable entry. The paths are parameters and stay out of
  it, so it covers every rule a launch can render and the policy's shape, not where it points.
- **The launch check.** A write-capable API launch (`api_tools: write`) is refused before the
  run exists unless, for the volume of every writable root of its policy, a record exists that
  is of the current format, names that volume, was made under the running kernel, carries the
  digest of the launch's own policy, and holds every arm as passed. A change to the rules, or to
  the roots and read-only paths the write set is built with, therefore asks for a new probe. A
  volume with no UUID, or whose UUID `diskutil` cannot report, cannot be keyed and is refused.
  A writable root that holds the record directory or lies inside it is refused, since a
  confined command could write a record there. The refusal names the volume and the probe
  command.

Required checks:

- The policy text carries the file-link rules as its last rules, one per read-only path and
  unreadable entry, and `(deny file-link)` with no root.
- On the host volume the probe passes all five arms and its record lets a launch through; with a
  policy that grants every write, the four refusal arms fail and the control passes.
- With every option that renders rules turned on, nothing after the first file-link deny allows
  anything, and a change to the terminal rules changes the digest.
- A launch is refused with no record, with a record whose arm failed or is missing (each arm), a
  record made for another kernel, policy text, volume or format, a record for a policy without
  the read-only path, an unreadable record, and a writable root above, equal to or inside the
  record directory. With records for two volumes, a launch is refused until both pass.
- The write-set launch asks the check with its own policy, and the read set does not.

Limits:

- The probe measures one volume at a time, by its UUID. A volume that is reformatted or replaced
  gets a new UUID and needs a new probe; a volume whose mount options change without a new UUID
  keeps its record.
- The probe and the check cover the write set. The gemini-cli policy gets the file-link rules but
  no launch check: no code refuses a gemini-cli process on a volume without a passing record.
  Gemini legs with tools stay held on this host until the gemini-cli launch calls the same check.
- A hard link that already exists inside a root before the leg starts is still written through to
  the file it shares, as Amendment 2 says.
- The record directory is outside every writable root of a checked launch. Another process of the
  same account can still write it.

## Amendment 4 (2026-11-20) — credential locations unreadable, a uv cache per leg, no keychain service

**Status: Accepted.**

For the write set, this amendment replaces these sentences of Amendment 2:

- in its Decision, "The policy denies by default, leaves reads unrestricted, and allows writes only
  under three roots: the leg's working directory, the uv cache (the directory `uv cache dir`
  reports, or uv's default location when `uv` is not on the path) and the temporary directory";
- in its Limits, the bullet that begins "Reads are not confined": reads now stop at the unreadable
  list, and the rest of that bullet holds for every other read;
- in its Limits, "A command that writes outside the three roots fails";
- in its Limits, "The uv cache is a writable root, and `uv` runs outside the sandbox install from
  it, so a command can change what a later unconfined `uv` run installs."

The write set's policy now denies by default and leaves reads open except for the unreadable list
below. It allows writes under two roots, the leg's working directory and the temporary directory,
which holds the leg's own uv cache. The working directory's `.git` stays read-only. A command that
writes outside the two roots fails. The rest of Amendment 2 stands.

This amendment also records the policy lionagi builds around the gemini-cli (Antigravity)
process, which no earlier decision describes. It uses the same policy type.

Decision:

- **Unreadable paths.** `SeatbeltPolicy` takes an opt-in `deny_read` list. Each entry is rendered
  as `(deny file-read* file-write* (subpath ...))` after the base read allow and the writable
  roots, so a denied path can be neither read nor overwritten, moved or added to. Each entry is
  denied in four forms: as written, where it sits once its parent directories resolve, at every
  link it passes through, and at the path it resolves to. A form that lies under `/dev` is left
  out, since denying `/dev/null` breaks every command that reads it. The other forms stay denied,
  so an entry that is a link to `/dev/null` is itself still denied. As with the roots, the paths
  reach the policy as parameters, never inside its text. Under a network-on policy each entry also
  gets `(deny network-outbound (subpath ...))` after the network rules, so a unix socket inside an
  entry cannot be connected to. The file rules do not cover it: a `connect()` is judged by the
  network rules, and without this rule a daemon listening inside a denied directory (a store's
  server socket, an agent socket) answers a confined command and reads or writes for it.
- **Where the list comes from.** One function in the child sandbox module,
  `credential_deny_read`, builds the list. The Claude Code policy starts from it, and the write
  set and the gemini-cli policy each pass it whole through `configured_deny_read`, which adds the
  `deny_read` entries of the global `child_sandbox` block and its `extends` files with no
  profile's `allow_read` exemption, so no policy can drop an entry another one denies and a path
  the user adds there reaches all three. It holds `DEFAULT_DENY_READ` (generic credential locations, local stores and shell
  histories), lionagi's own `.env` under `LIONAGI_HOME`, and every sign-in the subscription
  accounts directory keeps or names: the directory itself, the token files in it, the `.env` files
  its rows read and the homes of its Codex rows, each named as written and at the path it resolves
  to. It also holds the private directory of the Studio daemon's same-user reconnect socket,
  beside the daemon's store, because a connection there is answered with a pairing code and the
  daemon's key for that start. When the directory setting, the accounts file or one of its rows cannot be read, those
  sign-ins cannot be named, so the launch is refused with a configuration error rather than run
  with them readable. On the write set and the gemini-cli policy the refusal names no opt-out:
  turning the Claude Code child sandbox off does not lift it there. The
  gemini-cli policy keeps no entry of the list readable for its own sign-in, which it reads from
  its own state directory.
- **Overlap is refused.** A policy is refused when an unreadable entry is a writable root or holds
  one, since the leg could not use its own tree. It is also refused when an entry lies inside a
  writable root. The one exception is the temporary directory when it is not the workspace.
  Containment is judged by file identity where the paths exist, so an entry spelled in another
  case on a case-insensitive volume is judged the same way.
- **A uv cache per leg.** With `per_leg_uv_cache`, the shared uv cache is not a writable root.
  The policy makes a new, empty cache under the temporary directory, hands it to the confined
  commands as `UV_CACHE_DIR`, and removes it when the process exits. The write set and the
  gemini-cli policy both turn it on. The write set no longer runs the `uv cache dir` probe.
- **No keychain service.** The network rules no longer allow the lookup of
  `com.apple.SecurityServer`, so no confined command reaches it, network or not. With the lookup
  allowed, a confined command can ask `/usr/bin/security` for any item whose access list admits
  that tool, and the item is returned even when the keychain file's own directory is unreadable,
  because the service reads the file and the command does not. The gemini-cli completes a turn
  without the lookup: measured with Antigravity CLI 1.5.13 under the policy this amendment builds,
  with `~/.gemini/oauth_creds.json` denied and the keychain service out of reach, one turn
  returned `SUCCESS` (conversation `c017023a-3268-4c91-be14-3098a01e9160`), so the CLI signs in
  from its own state directory. ADR-0142, which is Proposed, excludes four credential services in its D2.
  This is one of them; this policy, which denies by default, has never allowed the other three
  (`com.apple.securityd`, `com.apple.securityd.xpc` and `com.apple.security.agent`).
- **The gemini-cli policy.** On a host with the sandbox, lionagi runs the whole CLI process under
  a policy built by `SeatbeltPolicy.for_workspace`, and leaves the CLI's own `--sandbox` flag out,
  since macOS refuses a second sandbox inside the first. Writes go under the working directory,
  the temporary directory and the CLI's state directory, and the working directory's `.git` stays
  read-only. The network is always on, since the CLI talks to its service. The process may
  allocate pseudo-terminals of its own for its shell tool. The CLI starts through `/usr/bin/env`
  with the per-leg `UV_CACHE_DIR`, so every command it runs inherits that cache. A CLI path that
  contains `=` is refused, since `env` would read it as a variable. On a host without the sandbox,
  a `yolo` request is refused and any other request runs with the CLI's own flags.

Required checks:

- The policy text and argv carry the read denials only when asked for, and never the paths.
- A denied file cannot be read under `sandbox-exec` directly, through a symlink, by listing its
  directory, by a hard link, by a copy, or under a path spelled in another case. A file in the
  workspace still reads.
- Each overlap refusal above is exercised. A denied path linked to `/dev/null` leaves `/dev/null`
  readable.
- A confined command sees the per-leg `UV_CACHE_DIR`, can write it, and cannot write the shared
  cache.
- Under a network-on policy, an item in a keychain the test creates under the temporary directory
  is not returned to a confined command and is returned outside it. The user's login keychain is
  never used.
- Under a network-on policy, a confined command cannot connect to a unix socket listening inside
  a denied directory, directly or through a symlink it plants in the workspace. Three controls run beside it: a file in that directory cannot be read, the
  same connect is refused with the network off, and a listener outside the list answers with the
  network on.
- On the write set and on the gemini-cli policy, a confined command cannot connect to a listener
  at the Studio daemon's reconnect socket path under a temporary LionAGI home, while a listener
  outside the list answers.
- The gemini-cli argv denies every entry of the list and sets a new, empty `UV_CACHE_DIR` on each
  launch. The shared uv cache is not one of its roots. A `yolo` request on a host without the
  sandbox, and a CLI path containing `=`, are refused.

Limits:

- Every read outside the list stays open, and with `api_network: true` it can still leave the
  machine. The list names known locations. A credential kept elsewhere is readable.
- On the gemini-cli policy the network is never off, so every read outside the list can leave the
  machine on every leg. That includes the CLI's own state directory, `~/.gemini/antigravity-cli`,
  where it keeps its refreshed login: the CLI must read and write it, and the commands its shell
  tool runs share its sandbox, so they can read that login too.
- The editor, reader and search tools run in this process, outside the policy. They keep their own
  path checks, not the unreadable list: a final symlink is refused, the resolved path must lie in
  the workspace, and protected names (`.env`, `.netrc` and private-key file names) are refused.
  That is why an unreadable path inside the workspace is refused rather than denied.
- Under the temporary directory, an unreadable path stays unreadable to commands. A command could
  still move a directory above it and read it under the new name. The exception exists because the
  temporary directory is shared scratch, and test harnesses keep a home there.
- A confined `uv run` works only where the environment already exists, since it installs nothing
  it would have to download or take from the shared cache. lionagi does not sync a leg's
  environment; whoever launches the leg syncs it before the leg starts.
- Other services in the network rules (trust evaluation, name resolution, configuration) stay
  allowed.
- With the network on, a unix socket outside the list stays reachable: an ssh agent, a container
  daemon, or any other local service listening outside the denied entries answers a confined
  command. The Studio daemon's reconnect socket is not one of them, since its directory is on the
  list. That holds for the write set and the gemini-cli policy. A Claude Code child gets the same
  directory on its deny list, but its sandbox has its own unix-socket rule, which this amendment
  does not change or measure. A Codex child is not denied it. The list names the socket's
  directory as the launching process resolves it: beside that process's own store file, or in its
  LionAGI home when its store is not a file. A daemon started with another store path or home
  keeps its socket outside the list of a leg launched by a process configured differently.

## Amendment 3 (2026-10-27) — child environments for coding tools

**Status: Accepted.**

Coding-tool subprocesses do not automatically inherit declared credential environment variables.
Names come from secret settings fields, provider and account references, configured secret lookup
names, explicit authentication-variable declarations, and any name matching the sensitive-name
patterns already applied to MCP server environments. Matching for removal is case-insensitive.
An operator may grant exact named variables through the agent specification or coding toolkit;
tool-call arguments cannot grant them. Shell startup variables `BASH_ENV` and `ENV` are excluded
by default. The same environment policy applies with and without the Seatbelt command wrapper.
Configured worktree test gates and auto-repair commands run with the scrubbed environment because
they execute agent-edited code. Configured hooks, notifications, scheduled commands, secret
resolvers and provider processes retain their existing environment policies, including existing
MCP filtering.

The environment policy has two deny checks:

- `credential_env_names(...)` computes the declared name set from the `SecretStr` fields of
  `AppSettings`, provider `api_key_env` names, account `key_env` names, configured secret lookup
  names, and explicit authentication-variable declarations, including MCP authentication references.
  It reads names only, not credentials, and does not execute a secret lookup to discover them.
  Unset declarations remain in the set. When the global settings file cannot be read or its
  secret lookup section is invalid, the set is unknown: building a coding agent or toolkit raises
  `ConfigurationError`, and so does a command that computes the set itself (a standalone coding
  tool). The policy never falls back to a partial set.
- A variable is also removed when its upper-cased name contains any member of the existing
  `_SENSITIVE_ENV_PATTERNS` frozenset in `service/connections/mcp_wrapper.py`. The policy reuses that
  one pattern set; it does not copy it into a second catalog. This covers names such as
  `AWS_SECRET_ACCESS_KEY`, `DB_PASSWORD` and `SOMEVENDOR_API_KEY` without requiring a provider or
  account declaration.

The scrubbed environment is the default for bash and search, the Seatbelt writer and its cache
location discovery, automatic formatting, static tools, coding-tool Git helpers, and configured worktree test
gates and auto-repair commands. An explicitly supplied exec-cell environment remains an explicit
grant; prompt cells continue to refuse a nonempty supplied environment. An exec-cell environment
is supplied only by the Python caller that constructs the `Cell`; no tool call or agent-visible
argument can set it.

The operator-owned opt-in is `AgentSpec.command_env_allowlist: tuple[str, ...] = ()`, with the
matching `CodingToolkit(command_env_allowlist=...)` option and the same named opt-in on
`CodingEngine`. The specification's composition and YAML read/write paths preserve the names.
Names are literal variable names, not patterns or values. An opt-in restores the exact spelling
requested from the parent environment, including a name removed by either deny check; it does
not restore a different case alias or another credential variable. A missing variable adds
nothing. `LIONAGI_STATE_DB_URL` stays excluded from coding-tool environments even when named in
the opt-in. No bash or search tool-call argument can change the grant. Process-group ownership,
timeouts and output bounds keep their existing behavior.

Required checks use isolated settings and synthetic values:

- `bash env` exposes no denied name, including case aliases, with and without confinement.
- An unrelated non-credential variable survives. A pattern-only name is removed; a near miss such
  as `MY_TOKENIZER_PATH` survives when no pattern matches it.
- The opt-in restores exactly the named variable, including a pattern-only name, while other
  denied names and the state-store URL remain absent.
- Shell startup files named by inherited `BASH_ENV` or `ENV` are not loaded automatically by the
  default environment policy.
- Configured hooks still receive their intended credential variables under their existing policy.
- Configured worktree gates and auto-repair commands cannot read an ambient credential through
  agent-edited code by default; the named operator opt-in permits it when explicitly granted.

Limits:

This closes automatic credential-variable inheritance by coding tools. It does not confine reads:
a command can still read files the user can read, including `.env`, credential stores, and startup
scripts. A child can load those files or obtain credentials through other configured services.
Names in no declaration that match no pattern, and variables an operator explicitly grants, are
outside this protection. It is not a credential confidentiality boundary for arbitrary code.

## Amendment 2 (2026-10-25) — the write set runs its editor and bash under the macOS sandbox

Amendment 1 offered an API model the read tools only, and left the tools that write files or run
commands to a later decision that runs them inside an operating-system sandbox. This is that
decision, for macOS. It replaces Amendment 1's statement that `read` is the only tool set.

Decision:

- `api_tools: write` is a second tool set: the read set plus `editor` and `bash`. It runs only
  under macOS Seatbelt (`/usr/bin/sandbox-exec`). A profile, or a stored launch record, that names
  it on a host without an executable `/usr/bin/sandbox-exec` is refused before a run exists.
- Every bash command, and the write that ends every editor `write` or `edit`, runs in a child
  process under one policy. The policy denies by default, leaves reads unrestricted, and allows
  writes only under three roots: the leg's working directory, the uv cache (the directory
  `uv cache dir` reports, or uv's default location when `uv` is not on the path) and the
  temporary directory. The working directory's `.git` stays
  read-only, since a hook written there would later run outside the sandbox. Network is off
  unless the profile sets `api_network: true`, which is valid only with the write set. The policy
  takes the shape of the OpenAI Codex CLI's macOS sandbox.
- Seatbelt judges the resolved path, so each root is resolved before it is passed, and a write
  through a symlink that points outside the roots is refused wherever the link sits. The roots
  reach the policy as parameters, never inside its text.
- The editor still reads and checks paths in this process, and the child that writes opens the
  file without following a final symlink. `guard_paths` and `guard_destructive` stay registered as
  pre-hooks. They are the inner layer; the sandbox is the boundary.
- `AgentSpec` gains `seatbelt`, the policy the coding editor and bash run under, and
  `CodingToolkit` takes the same policy. `None`, the default for both, runs them in this process
  as before.
- The launch record gains `network`. A record written before it reads as off. A record that
  names the write set with a network value that is not a boolean, or the read set with network
  on, is refused like any other record this version does not run. A resume binds the recorded set
  again, under a policy built for the resume's working directory.

Limits:

- macOS only. On another host the write set is refused; confinement on Linux is a later decision.
- Reads are not confined. A command can read any file the user can and print it into the
  conversation, so the write set suits a workspace where that is acceptable. With
  `api_network: true` the same reads can leave the machine: a command can read any file the user
  can and send it to any host, so the network is for a workspace and a task where that is
  acceptable too.
- A command that writes outside the three roots fails: a commit in a worktree whose repository
  lives elsewhere, a `uv tool` install, a write under the home directory.
- The uv cache is a writable root, and `uv` runs outside the sandbox install from it, so a command
  can change what a later unconfined `uv` run installs. Files in the working directory that run
  later outside the sandbox (tests, build files, scripts) are writable because editing them is what
  the write set is for. The sandbox bounds where writes land, not what they contain, so read a
  leg's changes before running them. `.git` alone is read-only because git runs its hooks without
  anyone reading them.
- Seatbelt judges a write by the path the command names. Making a hard link to a file outside the
  roots, or to a file in `.git`, is refused. A hard link that already exists inside a root is
  written through to the file it shares, wherever else that file is named, so a working directory
  handed to a write-set leg is assumed to hold no hard links to files outside the roots.
- `sandbox-exec` is marked deprecated in its manual page. It is present and enforced on current
  macOS; a release that removes it turns the write set into a refusal, never into unconfined tools.

## Amendment 1 (2026-10-17) — an API-model `li agent` leg binds the read tools only

`li agent` refused every model that is not a CLI provider. A CLI provider runs its own tool loop,
so the whole leg is one `operate` call; an API model has no loop of its own, and one call would
be one model turn. The coding tools `create_agent` registers run inside this process with its
permissions. `guard_paths` and `guard_destructive` are pre-hooks on a tool call; they are not an
operating-system boundary, and nothing on this path stands where a CLI provider's own sandbox
stands.

Decision:

- `AgentSpec` gains `coding_tools: tuple[str, ...] | None = None`, the coding tools step 6 of D2
  binds when `tools` names `"coding"`. `None` binds the default set, as before.
  `context_management=False` still removes `context` from either set.
- `li agent` builds an API-model leg through `create_agent` only when its agent profile declares
  `api_tools: read`. The spec binds `coding_tools=("reader", "search", "context")`, the secure
  guards scoped to the leg's working directory, the profile's `role:` (default `reviewer`), and
  no MCP server. A profile's `khive_injection` keeps its recall on the leg with writeback off,
  whatever the profile sets, so no read output is stored in khive. The leg runs `Branch.ReAct(tools=True, max_extensions=N)`, with `N` from the
  profile's `max_extensions` (a whole number from 1 to 100, default 10), under the leg's
  `--timeout`.
- `read` is the only tool set. Any other `api_tools` value, `--preset coding` and `--image` are
  refused before a run exists, and a profile without the key is refused as before. Tools that
  write files or run commands are offered to an API model only by a later decision that runs
  them inside an operating-system sandbox.
- The branch records the tool set and bound under `metadata["api_launch"]` and is written to the
  run's branches directory after every turn. A resume runs the loop again from that record, with
  the read set bound again to the resume's working directory and the bound the resuming profile
  names; a resume under a profile without `api_tools`, or with `--image`, or of a record naming a
  tool set or a bound this version does not run, is refused before a run exists. A non-CLI
  branch without the record keeps the earlier path. Queued operator
  messages drain at turn end as further turns of the loop.
- An API model is built from its model spec alone; the CLI display options (`-v`, `--theme`)
  are not put on its request, on a new leg or a resume.

`search` is held to what `reader` may read: it resolves its path inside the working directory,
passes the pattern to grep after `-e` so a pattern is never read as an option, and leaves out
the matches of every file whose path holds a name `reader` refuses (the basenames `.env`,
`.netrc`, `id_rsa`, `id_ed25519`, `id_ecdsa` and `.htpasswd`, in any case), which a recursive
search would otherwise reach. Other secret-bearing names, such as `.env.local` or a `.pem` file,
are not refused by either tool. Each match is read from the NUL grep writes after its file name,
so a name holding a newline is read whole, and binary files are skipped (`-I`), so no notice
names a matching file. A grep that runs past its 30-second bound fails the call; the rows it
read before then are not returned.

Limits: the loop's action protocol is lionagi's JSON action request, not a provider's native
tool calling.

## Context

LionAGI needs a reusable description of an agent without forcing description-time code to open
files, resolve credentials, construct an API provider, discover remote tools, or allocate a live
conversation. It also needs one place where that description becomes a fully wired `Branch`.
The shipped implementation divides those responsibilities between `AgentSpec` and
`create_agent()`.

This ADR answers six concrete problems.

**P1 — Identity and runtime state have different lifetimes.** A casts `Role` and ordered
`Mode` set can be inspected, validated, serialized in part, and reused without a provider.
A `Branch` owns live message, action, model, logging, capability, observer, memory, and context
state. Treating the declarative input as the live agent would make composition perform I/O and make
reuse dependent on construction order.

**P2 — Role-backed construction has cross-cutting wiring.** Provider selection, effort translation,
trusted settings, the Lion system preamble, built-in tools, MCP discovery, and emission grants must
agree. If every caller performs those steps independently, prompt order and granted capability
sets drift.

**P3 — prompt and emission overrides need three-state semantics.** The prompt has four ordered
sources. Emission selection distinguishes “use the Role declaration,” “replace it,” and “grant
nothing.” Collapsing `None` and an empty tuple would remove a useful denial state.

**P4 — raw prompt workers are legitimate but are not casts agents.** CLI orchestration supports
bare workers and external prompt profiles. They have no `Role` to compose and therefore cannot
implicitly acquire a Role emission contract. Their direct `Branch` construction is an explicit
escape hatch, not a second Role-backed factory.

**P5 — two unrelated facilities currently use “context” language.** The
`AgentSpec.context_management` flag controls the coding toolkit's `context` tool and a one-line
prompt hint. Pre-turn `ContextProvider` registration and execution live on `Branch` and chat/run
operation preparation. Setting the agent flag does not configure, disable, or populate the provider
registry.

**P6 — the current boundary leaks mutation and has incomplete export and interception coverage.**
Settings hooks and permission hooks are appended to the passed `AgentSpec` during construction.
Reusing one instance can therefore duplicate handlers. The YAML writer omits several serializable
fields. MCP tools are discovered after built-in tool interception and do not receive the resolved
permission preprocessor.

The as-built dependency direction is:

```text
casts Pattern/Role/Mode
          │
          ▼
   casts Profile
          │
          ▼
      AgentSpec                 Branch.providers
   (declared intent)        (pre-turn live registry)
          │                         ▲
          ▼                         │
     create_agent ───────────────► Branch
  settings/provider/tools/         live conversation
  prompt/MCP/capability grants
```

| Concern | Decision |
|---|---|
| Declarative agent contract | D1: `AgentSpec` owns Role-backed identity and requested runtime intent, not live state. |
| Materialization boundary | D2: `create_agent()` is the canonical Role-backed path for I/O, Branch allocation, tools, and grants. |
| Prompt and emission composition | D3: Prompt sources are ordered and emission selection keeps its three distinct states. |
| Raw-prompt construction | D4: Direct `Branch` construction is a visible non-casts escape hatch with no implicit Role grants. |
| Pre-turn context | D5: `ContextProviderRegistry` remains Branch/operations state; `context_management` is only a coding-tool switch. |
| Persistence and reuse | D6: The current YAML and construction mutation are recorded as shipped limitations, not treated as intended completeness. |

This ADR does not decide:

- How per-role model, effort, modes, and external-profile precedence should be resolved. That target
  belongs to ADR-0043.
- How prompt directives are renamed or executable permission interception becomes complete. That
  target belongs to ADR-0044.
- The internal ranking, budgeting, or writeback behavior of context providers. This ADR records
  only their ownership boundary because the full mechanism belongs with messages and operations.
- Provider endpoint selection and rate limiting beyond the inputs that the factory consumes.
- Session-level hook ownership. ADR-0047 records those scopes.
- Branch lifecycle after construction, including chat, operate, persistence, and orchestration
  scheduling.

## Decision

### D1 — `AgentSpec` is a declarative Role-backed runtime request

`AgentSpec` contains a casts `Profile` plus requested runtime choices. It is not a provider, tool
registry, capability registry, or conversation.

**The contract** (`lionagi/agent/spec.py`):

```python
@dataclass
class AgentSpec(HooksMixin):
    profile: Profile
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] = ()
    permissions: PermissionPolicy | None = None
    grant_emissions: bool = True
    emits: tuple | None = None
    pack: str | Pack | None = "default"
    lion_system: bool = True
    extra_prompt: str | None = None
    hook_handlers: dict[str, list[Callable]] = field(default_factory=dict)
    cwd: str | None = None
    yolo: bool = False
    mcp_servers: list[str] | None = None
    mcp_config_path: str | None = None
    context_management: bool = True
    coding_tools: tuple[str, ...] | None = None  # Amendment 1

    @classmethod
    def compose(
        cls,
        role: Any,
        *,
        modes: list[Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
        tools: tuple[str, ...] | list[str] = (),
        permissions: Any = None,
        pack: str | Pack | None = "default",
        grant_emissions: bool = True,
        emits: tuple | None = None,
        system_prompt: str | None = None,
        cwd: str | None = None,
        yolo: bool = False,
    ) -> AgentSpec: ...
```

`compose()` creates `Profile.compose(role, modes=...)`, resolves the permission representation,
copies `tools` to a tuple, stores `system_prompt` as `extra_prompt`, and returns. It does not
load settings, inspect MCP files, instantiate `iModel`, or allocate `Branch`.

Permission inputs accepted at this boundary are:

| Input | Stored result |
|---|---|
| `None` | `None` |
| existing `PermissionPolicy` | same object |
| `dict` | `PermissionPolicy.from_dict(dict)` |
| `"safe"`, `"read_only"`, `"allow_all"`, `"deny_all"` | corresponding preset |
| unknown string | `ValueError` listing valid presets |
| any other type | `TypeError` |

Hook declaration is mutable builder syntax on the spec:

```python
spec.pre(tool_name, handler)       # key "pre:<tool_name>"
spec.post(tool_name, handler)      # key "post:<tool_name>"
spec.on_error(tool_name, handler)  # key "error:<tool_name>"
```

Each method appends in declaration order and returns the same spec for chaining.

**Exact semantics.**

- Empty `tools` means no agent-factory tools are registered. It does not mean “all tools.”
- `pack=None` disables the directives block. `pack="default"` loads the packaged pack and raises
  if that resource cannot be loaded. A non-default string is not a file path here; it resolves to
  no pack and therefore renders no directives. A caller must pass a `Pack` object for a custom
  pack.
- `system_prompt=""` is normalized to `None` by `compose()`.
- `mcp_servers` and `mcp_config_path` are model fields but are not accepted by `compose()`; callers
  set them on the returned spec or construct the dataclass directly.
- `AgentSpec.coding()` fixes Role `implementer`, effort default `"high"`, and tool request
  `("coding",)`. With `secure=True` it appends destructive-command and workspace path guards.
- The spec is mutable. “Declarative” describes its responsibility, not frozen value semantics.

**Why this way.** A value-like request separates inspectable identity from external effects while
remaining Python-native: Role and Mode objects, model classes, permission objects, and callables do
not need a second configuration language. A fully frozen spec was not shipped because hook builder
methods and coding preset adjustment currently mutate fields.

### D2 — `create_agent()` is the Role-backed materialization boundary

Role-backed callers use the asynchronous factory. It is the only shipped construction path that
both composes the casts prompt and grants the resulting Role emission contract.

**The contract** (`lionagi/agent/factory.py`):

```python
async def create_agent(
    config: AgentSpec,
    *,
    load_settings: bool = True,
    project_dir: str | None = None,
    trust_project_settings: bool = False,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
    chat_model: Any = None,
    log_config: Any = None,
) -> Branch: ...
```

The materialization sequence is normative as-built behavior:

```text
1. optionally load settings and append trusted hooks to the passed spec
2. accept chat_model, or parse spec.model and construct iModel
3. allocate Branch
4. compose and install the system message
5. append PermissionPolicy as "security_pre:*" on the passed spec
6. register requested built-in/coding tools (`coding_tools` selects the coding set, Amendment 1)
   and attach their hooks
7. discover and register MCP tools
8. grant the resolved emission Operable, when non-empty
9. return Branch
```

Provider construction translates `provider/model` strings through `parse_model_spec()`. Explicit
`chat_model` wins. Otherwise, if `spec.model` is absent, the factory supplies no model keyword and
`Branch` applies its own default. Provider-specific effort and yolo keyword maps are used only when
the provider supports them. CLI providers receive the placeholder API key their subprocess
transport expects; API providers must resolve real credentials.

Settings trust is two-dimensional:

- Global `~/.lionagi/settings.yaml` may load when `load_settings=True`.
- Project settings are included only when `trust_project_settings=True`.
- Python hook imports are restricted to `trusted_hook_modules`; the default set contains only
  `lionagi.agent.hooks`.
- An untrusted configured module raises `PermissionError`. An absent or unresolvable trusted
  function is skipped by the settings resolver.

Settings hook input has this shipped shape (`lionagi/agent/settings.py`):

```yaml
hooks:
  pre|post|on_error:
    <tool-name>:
      - "trusted.module:function"
      - python: "trusted.module:function"
      - command: ["executable", "arg", "{argument_key}"]
```

A single mapping/string may be supplied instead of the per-tool list and is normalized to a
one-item list. Python import paths without `:` and trusted modules/functions that cannot be
imported resolve to no handler. A command must be an argv list of strings; shell strings raise
`ValueError`.

Pre-command hooks send the argument mapping as JSON on stdin, serialized before the subprocess
starts. A mapping that cannot be serialized, non-zero exit, subprocess creation failure, and the
10-second deadline raise `PermissionError`, preventing the Tool. Post-command hooks send the result
mapping and log/swallow serialization, subprocess or timeout failure; a result that cannot be
serialized starts no subprocess. On a timeout, a failed exchange with the subprocess, or
cancellation, both hooks terminate the process group, then wait up to 2 seconds for exit;
cancellation is re-raised after the wait, and a cancellation that arrives during the wait is
raised in place of the hook's own error. The group is terminated even when the hook process has
already exited while a process it started holds its pipes: members carrying the marker set at
spawn identify the group. A cancellation that arrives while the subprocess is being created
terminates its group once creation completes. Ten and two seconds are inherited
implementation values; no recorded rationale establishes those exact thresholds. An `on_error`
command currently builds the post-shaped callable and is appended to `error:<tool>`, but the
shipped Tool execution path never invokes error handlers (ADR-0044 records the target repair).

MCP discovery follows this search contract:

| Input | Behavior |
|---|---|
| existing `spec.mcp_config_path` | load only that path |
| non-existing explicit path | load nothing; do not fall back |
| no explicit path, project trust true | search ancestor `.lionagi/.mcp.json` and `.mcp.json` |
| no project match | try `~/.lionagi/.mcp.json` |
| no file | return without discovery |
| `mcp_servers is None` | discover all configured servers |
| explicit list | load only the named servers |

The factory calls `ActionManager.load_mcp_config()` with `mcp_security=MCPSecurityConfig.trusted()`
explicitly for every explicitly selected MCP path -- it does not rely on `load_mcp_config()`'s own
omitted-policy handling, which fails closed now that an omission is no longer upgraded to a
permissive policy (ADR-0011 delta row 3). `AgentSpec` does not expose an `mcp_security` field, so a
caller building through this path cannot request a narrower transport policy than `trusted()`. This
is current behavior, not the aspirational trust contract in ADR-0044.

**Error and repetition semantics.**

- A settings parse, explicit untrusted hook, provider-construction, Branch-construction, prompt,
  built-in tool registration, or emission-grant error propagates; the factory does not return a
  partial Branch.
- Per-server MCP discovery failures other than transport denial are logged by `ActionManager` and
  represented as an empty tool list for that server. Transport-policy denial propagates.
- There is no rollback of external work already performed before a later construction failure.
- Constructing twice from the same spec re-applies settings hooks and inserts another permission
  hook. The factory aliases `spec = config`; it does not copy.
- Built-in tool duplicate handling is delegated to `ActionManager.register_tool(update=False)` and
  raises for duplicate function names.
- MCP registration occurs after built-in interception. Newly discovered `Tool` objects keep their
  own default pre/postprocessor fields and do not receive the spec hook chain.

**Why this way.** The factory is the imperative shell around the declarative spec. It centralizes
cross-cutting order without moving provider or filesystem effects into casts. Keeping Branch
allocation here also gives emission grants one auditable construction point.

### D3 — prompt order and emission selection are deterministic

**The prompt contract** (`lionagi/agent/spec.py` and `lionagi/agent/factory.py`):

```text
optional LION_SYSTEM_MESSAGE
Role.body
Mode[0].behaviors
Mode[1].behaviors
...
selected Pack RolePolicy block
extra_prompt
optional coding-context one-liner
```

Within `AgentSpec.build_system_message()`, non-empty parts are joined by exactly two newline
characters. The Role policy block renders sections in this order when their tuples are non-empty:
`## Authority`, `## Operational Boundaries`, `## Escalation Conditions`. The factory then
prepends `LION_SYSTEM_MESSAGE.strip() + "\n\n"` when `lion_system=True`. The coding-context
one-liner is appended before that preamble step when `"coding" in spec.tools` and
`context_management=True`.

An empty composed message installs no system message. An empty casts body can still produce a
message through directives, extra prompt, or the coding one-liner.

**The emission contract**:

```python
def emission_operable(self) -> Operable | None:
    if not self.grant_emissions:
        return None
    if self.emits is not None:
        return build_emission_operable(
            tuple(self.emits),
            name=f"{self.profile.role.name}_emissions",
        )
    return self.profile.emission_operable()
```

| State | Meaning |
|---|---|
| `grant_emissions=False` | grant nothing, regardless of `emits` |
| `grant_emissions=True, emits=None` | use the Role's declaration |
| `grant_emissions=True, emits=(...)` | replace the Role declaration |
| `grant_emissions=True, emits=()` | explicit empty override; grant nothing |

A non-empty emission tuple is converted to an `Operable` and gains
`EscalationRequest` unless already present. The factory calls
`branch.grant_capabilities(op)` only for a truthy non-`None` result.

**Why this way.** Ordered prompt composition makes mode ordering meaningful and reproducible.
Three-state emission selection lets orchestration suppress, inherit, or narrow a Role contract
without editing the Role itself.

### D4 — direct `Branch` construction is a raw-prompt escape hatch

CLI orchestration has two construction families (`lionhub/src/lionhub/serve/cli/orchestrate/_orchestration.py`):

```text
built-in casts Role
    AgentSpec.compose(role, modes, extra prompt)
    → create_agent(...)
    → Branch with casts prompt and factory wiring

bare worker or external AgentProfile replacement
    Branch(chat_model=..., system=verbatim_system, ...)
    → no Profile composition
    → no Role emission grant
```

A task-level `system_prompt_override` also selects the verbatim path. External profile prompt text
wins over casts composition in the current CLI. Bare workers receive the fixed bare-worker prompt.
Both branches can later be included in a `Session` and receive session-owned routing, but that does
not retroactively make them Role-backed agents.

**Exact semantics.**

- Direct construction is not prohibited; it is the supported path for identities with no casts
  Role.
- A raw path receives no pack directives, Role body, Mode behaviors, factory settings hooks, agent
  permission hooks, built-in agent tools, MCP discovery, or Role emission grants unless its caller
  wires those separately.
- `grant_spawn` is granted after either construction path by orchestration and is independent of
  Role emissions.
- The distinction is behavioral, not merely a different constructor spelling.

**Why this way.** Forcing verbatim prompts through a fabricated Role would claim typed emissions
and directives the prompt did not declare. Conversely, allowing Role-backed callers to bypass the
factory would duplicate and eventually drift the materialization stack.

### D5 — pre-turn context providers belong to `Branch` and operations

The coding context-tool switch and pre-turn provider registry are separate contracts.

**The provider ownership contract** (`lionagi/session/branch.py` and
`lionagi/protocols/context_providers.py`):

```python
class ContextProvider(Protocol):
    async def provide(
        self,
        branch: Branch,
        instruction: Instruction,
    ) -> str | None: ...

class ContextProviderRegistry:
    def __init__(self, budget: int = 2000): ...
    def register(
        self,
        provider: ContextProvider,
        *,
        priority: int = 0,
        max_tokens: int | None = None,
        name: str | None = None,
    ) -> None: ...

class Branch:
    @property
    def providers(self) -> ContextProviderRegistry: ...
```

`Branch.providers` lazily creates the registry. Chat and run preparation invoke it only when
entries exist and a system message provides a render target. Provider blocks are inserted into the
first-message system-guidance fold for that turn, never into the durable message record, and the
per-turn slot is cleared after request preparation. Provider failure is recorded and skipped.

The default 2,000-token provider budget is inherited from the implementation; no recorded rationale
explains that exact number. It bounds ephemeral injection, not the coding context tool.

By contrast, `AgentSpec.context_management=False` has exactly two factory effects:

1. remove `"context"` from `DEFAULT_CODING_TOOLS` before binding `CodingToolkit`;
2. omit the context-curation prompt one-liner.

It does not touch `Branch._context_providers`, `Branch.providers`, or operation preparation.

**Why this way.** Providers depend on the current Branch, instruction, progression, and per-turn
render slot. They are live operation state. The coding context tool is simply one requested tool
plus user guidance and belongs to agent construction.

### D6 — current YAML is a partial export and construction is not idempotent

The shipped YAML surface is:

```yaml
role: <canonical role name>
modes: [<canonical mode names>]
model: <string-or-null>
effort: <string-or-null>
tools: [<tool requests>]
pack: <string-or-null>
system_prompt: <literal-extra-prompt-or-null>
yolo: <bool>
lion_system: <bool>
context_management: <bool>
cwd: <optional string; omitted when falsy>
```

`from_yaml()` additionally accepts `permissions`, but `to_yaml()` never writes it. The writer also
omits `grant_emissions`, `emits`, `hook_handlers`, `mcp_servers`, and
`mcp_config_path`. A `Pack` object serializes as `pack: null`. Hook callables are intentionally
code-only; the remaining omissions make the method a partial export rather than a full spec
round trip.

The current code does preserve `lion_system=False` and `context_management=False` on read because
it restores those keys explicitly after `compose()` applies its defaults.

Construction mutates two parts of the input:

- `apply_hooks_from_settings()` appends resolved handlers through `HooksMixin`;
- `_apply_permissions()` inserts `policy.to_pre_hook()` into `security_pre:*`.

No marker deduplicates either operation.

**Why this way.** The first version optimized for a convenient, human-readable subset and builder
ergonomics. This ADR does not reclassify those choices as a stable persistence or idempotency
contract; the delta below makes the intended repair explicit.

## Consequences

- Callers can inspect and compose Role identity without allocating a provider or Branch.
- Role-backed construction has one place for prompt, tool, MCP, and emission ordering.
- Raw prompts remain possible without silently inheriting a typed Role contract.
- Maintainers debugging an unexpected prompt must check Profile, pack, extra prompt,
  `lion_system`, and the coding-context condition in that order.
- Maintainers debugging tool authorization must distinguish tools registered before and after MCP
  discovery; current coverage is intentionally recorded as incomplete.
- Reversing D1/D2 would be expensive because casts, CLI orchestration, Branch, provider creation,
  and capability grants would all need a new shared lifecycle owner.
- Reversing D4 is moderate cost but requires a typed external Role loader so raw identities can
  truthfully declare emissions.
- Reversing D5 would couple construction to per-turn Branch state and would blur two existing
  context facilities.
- Construction can leave partial external setup before failure and can accumulate handlers on a
  reused spec. Callers needing repeatable construction should create a fresh spec until the delta
  is closed.
- The factory's current transport trust default for an explicitly selected MCP file is broader than
  its project-file discovery default; ADR-0044 owns the target correction.

## Current-vs-ideal delta

| # | Delta | Size | Issue |
|---|---|---|---|
| 1 | Make settings and permission application non-mutating and idempotent; accept when constructing twice from one `AgentSpec` installs each handler exactly once and leaves the spec unchanged. | M | (filled at issue-open time) |
| 2 | Apply the resolved permission/interceptor chain after all tools, including MCP-discovered tools, are registered; accept when `deny_all` blocks an MCP tool call in a regression test. | M | (filled at issue-open time) |
| 3 | Define the `AgentSpec` YAML contract as either a complete serializable round trip or an explicitly named partial export; accept when permissions, emission settings, and MCP fields are preserved or documented as excluded. | M | (filled at issue-open time) |
| 4 | Separate the coding context-tool flag from Branch context-provider terminology; accept when public names and documentation cannot imply that `context_management` enables or disables `ContextProviderRegistry`. | S | (filled at issue-open time) |

## Alternatives considered

### Put provider construction and filesystem discovery inside `AgentSpec.compose()`

This would make one call appear self-contained and could return a ready-to-use object. It lost
because composition would become asynchronous and environment-dependent, Role/Profile tests would
need provider credentials and filesystem fixtures, and merely inspecting intent could allocate live
resources. The shipped pure-description/effectful-factory split keeps those failure domains apart.

### Require every caller to construct `Branch` directly

This would remove one factory abstraction and let specialized callers tune every detail. It lost
because each Role-backed caller would have to reproduce prompt ordering, provider effort mapping,
settings trust, tool attachment, MCP discovery, and emission grants. The existing CLI already shows
why direct construction is appropriate only when no Role contract exists.

### Make `AgentSpec` the live agent facade

The spec could hold a Branch internally and forward chat and tool methods. That would provide one
user-facing object, but it would combine reusable configuration with one mutable conversation
identity. Copy, serialization, restart, and multi-Branch use would become ambiguous, so the shipped
types keep the lifecycle boundary explicit.

### Treat external prompts as synthetic Roles

This would route every worker through the factory and reduce constructor variety. It lost because a
prompt file does not provide typed emissions, artifact defaults, canonical Role identity, or a
validated Mode contract. Fabricating those fields would grant ambient capabilities. ADR-0043
instead requires explicit overlay or replacement disposition.

### Make `context_management` control both coding tools and providers

One switch would be superficially convenient. It lost because providers are registered on a live
Branch and may be useful to chat-only agents with no coding toolkit. Conversely, the coding context
tool may be disabled without disabling externally registered pre-turn knowledge.

### Collapse `emits=None` and `emits=()`

A two-state API would be simpler. It lost because orchestration needs both inheritance from the Role
and an explicit “grant nothing” override. `grant_emissions=False` remains the stronger global
short-circuit.

### Make YAML a pickle-equivalent full snapshot

A complete snapshot could preserve callables and runtime objects. It lost because hook callables,
provider clients, and `Pack` objects are code/runtime state and unsafe or non-portable to deserialize
as data. A complete data contract can include serializable intent while keeping code-only handlers
explicitly excluded.

## Notes

In pari materia resolves the boundary: `AgentSpec` fields are interpreted alongside their actual
factory and operation consumers, not as a claim that every similarly named runtime facility is
agent configuration.

Source anchors: `lionhub/src/lionhub/agent/spec.py`, `lionhub/src/lionhub/agent/factory.py`,
`lionhub/src/lionhub/agent/settings.py`, `lionagi/protocols/action/manager.py`,
`lionagi/session/branch.py`, `lionagi/protocols/context_providers.py`,
`lionagi/operations/chat/_prepare.py`, and
`lionhub/src/lionhub/serve/cli/orchestrate/_orchestration.py`.
