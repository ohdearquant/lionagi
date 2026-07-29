# Changelog

All notable changes to Den are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

First public release.

### Added

- **Runs panel.** A live tree of every agent run on your machine, grouped by
  project. `li agent` and `li o` runs appear as they start and stream as they
  progress.
- **Claude Code sessions.** Local Claude Code transcripts are mirrored into the
  same tree, so terminal agents and editor agents are visible in one place.
- **Active band.** A pinned group at the top of the tree holding everything
  running right now, across every project.
- **Run detail.** Attach to any run and follow its output live over SSE. The
  panel can be retargeted at a different run without losing the stream.
- **Run tree view.** `Den: View Run Tree` draws a run's branch and agent DAG
  with typed nodes and per-run cost, refreshed as the run progresses.
- **Backend lifecycle.** Den starts and supervises a local
  `python -m lionagi.studio` backend on `localhost`, with a status bar item for
  its state. Auto-start, host, port, interpreter, and bearer token are
  configurable; you can also attach to a backend you started yourself.
- **First-run self-heal.** A pre-flight check verifies the Studio import before
  the first spawn and repairs a missing install rather than failing opaquely.
- **Getting-started walkthrough.** A four-step VS Code walkthrough covering
  install, opening the panel, starting a run, and reading the trace.

### Notes

- Den is read-only observability. It watches runs you start; it does not start,
  stop, or modify them.
- All data stays on your machine. See the Privacy section of the README.
