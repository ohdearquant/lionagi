# Changelog fragments

A pull request does not edit `CHANGELOG.md`. It adds one file here instead, named
`<pull request number>.md`, for example `1234.md`.

Inside the file, use only these Keep a Changelog headings: `### Added`, `### Changed`,
`### Deprecated`, `### Removed`, `### Fixed` and `### Security`. Put one or more `- ` bullets
under each heading. A bullet may continue on indented lines, and a blank line ends it.

```markdown
### Fixed

- Describe what now works, in one or two sentences.
  A continuation line is indented.
```

When the development branch is promoted to main, the fragments are folded into the
`## [Unreleased]` block of `CHANGELOG.md` in pull request number order, and the fold deletes
them. Run it from the repository root:

```bash
python scripts/fold_changelog_fragments.py
```

The fold refuses, and changes nothing, if a fragment has an unknown heading, no bullets or text
outside a heading, if another file is in this directory, or if `CHANGELOG.md` has no
`## [Unreleased]` block.
