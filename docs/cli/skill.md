# li skill

Load and print installed skill bodies.

## Synopsis

```
li skill <NAME>
li skill list
li skill show <NAME>
```

## Description

`li skill` reads skill files from `~/.lionagi/skills/` and prints them to stdout. Orchestrators use this to inject skill instructions into agent context on demand — no extra protocol required.

`li skill` is direct-dispatched before argparse runs, so it has no subparser and accepts no flags.

Skills use the same directory/file layout as Claude Code skills. A skill file at `~/.lionagi/skills/<NAME>/SKILL.md` can be symlinked from `.claude/skills/` so the same source serves both systems.

## Forms

| Form | Output |
|------|--------|
| `li skill NAME` | Prints the skill body — content after the YAML frontmatter. |
| `li skill list` | Prints all installed skill names, one per line. |
| `li skill show NAME` | Prints the full `SKILL.md` file including frontmatter. |

`NAME` must be a bare identifier: no path separators (`/`, `\`), no leading `.`, no leading `-`.

## Storage layout

```
~/.lionagi/skills/
  <NAME>/
    SKILL.md    ← the skill file
```

Each skill is a directory named `<NAME>` containing a single `SKILL.md`. Files directly in the root (not in a subdirectory) are ignored by `list`.

## Frontmatter stripping

`li skill NAME` strips the YAML frontmatter block before printing. A frontmatter block opens with `---` on its own line and closes with another `---`. If the file does not start with `---`, it is printed as-is. Unterminated frontmatter (opening `---` with no closing `---`) is also printed as-is.

`li skill show NAME` prints the full file without stripping.

## Security

`resolve_skill_path` enforces containment: after symlink resolution, the target path must remain under `~/.lionagi/skills/`. A `SKILL.md` that is a symlink pointing outside the skills root is rejected. The root directory itself may be a symlink (e.g. pointing at a shared skills repo).

## Examples

```bash
# List all installed skills
li skill list

# Print a skill body (post-frontmatter) — used by orchestrators
li skill codex-review

# Print the full file including frontmatter
li skill show codex-review

# Inject into a prompt
INSTRUCTIONS=$(li skill codex-review)
li agent claude "$INSTRUCTIONS — now review PR #42"
```
