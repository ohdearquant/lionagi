### Security

- A Codex child under the child sandbox is now denied reading the credential locations the
  child sandbox lists, and its own Codex home, as Claude Code children already were. The child
  runs under a permission profile, and a launch is refused before the process starts unless a
  probe record shows that the codex binary it runs enforces the profile. The record is written by
  `python -m lionhub.providers.openai.codex_sandbox_probe`, and its directory is on the child
  sandbox's edit-deny list so a child cannot forge one.

### Changed

- With the child sandbox on, a Codex child's command line carries no `--sandbox` and no `-s`: it
  carries `default_permissions` and a `permissions.lionagi_child` table as the last `-c`
  overrides, with the same write roots and network setting the sandbox mode gave. The
  `sandbox_workspace_write` pins are no longer emitted in that state. With the child sandbox off
  the command line is unchanged.
- A Codex launch under the profile refuses `sandbox_mode`, `permissions` and `default_permissions`
  overrides, a user config that sets `default_permissions` or defines `[permissions.lionagi_child]`,
  and a project `.codex/config.toml`. A user config that sets a root `sandbox_mode` is accepted,
  and the leg log says in one line that the command-line profile governs over it.
- Under the profile, an override key is refused when either reading of it names a guarded key
  (`sandbox_mode`, `default_permissions`, `permissions.*`, `profile`, `sandbox_workspace_write.*`):
  split at dots, the way Codex reads command-line keys, or decoded as a TOML dotted key, so a
  quoted part, spaces around a dot or a `\u` escape does not hide it. A profile the run could apply
  (the request's `profile`, a `profile` override, or the root `profile` key of `config.toml`) is
  refused when it sets `sandbox_mode`, `default_permissions` or `permissions`, as a
  `[profiles.<name>]` table or a `CODEX_HOME/<name>.config.toml` file. A request's own `full_auto`
  or `sandbox` setting no longer exempts a user config that turns the sandbox off, because no
  `--sandbox` flag is emitted, and `as_cmd_args()` reaches the same refusals as a launch.
- The profile's write entries are the set the sandbox mode it replaces granted. A read-only child
  has none, its working directory included; a light child writes its working directory only; a
  heavy child writes the child sandbox's roots, the `writable_roots` the run's own overrides set
  (a string, list or tuple, at the root or in the selected config profile, with a leading `~`
  expanded), the `--add-dir` directories and a linked worktree's git roots. A heavy profile
  honours the user config's `exclude_slash_tmp` and `exclude_tmpdir_env_var`. Codex ignores
  `sandbox_workspace_write.*` under a permission profile, so those pins and the caller's
  `writable_roots` overrides are no longer emitted beside it; the table carries them.
- A heavy request that names no sandbox takes the mode Codex would have used without
  `--sandbox`: the user config's `sandbox_mode`, then the selected profile's, and read-only when
  neither sets one. A read-only profile has no write entry at all.
- A denied location is emitted as written, as the sandbox places it (the real path of its
  directory plus its own name) when that differs, and at each link hop. A leading `~` expands
  against the child's `HOME`, then the account's home directory, never the launching process's
  `HOME`. A per-profile `allow_read` list is not applied to a Codex child, which does not know the
  agent profile the way a Claude Code child does; the sandbox's default read policy applies.
