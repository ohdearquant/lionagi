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
