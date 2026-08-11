# `li state null-content` — design rationale

`_null_content` (in `lionagi/cli/state.py`) replaces old message bodies with a
marker, keeping every row and reference. This note preserves the reasoning
that used to live inline as its docstring.

## Why this exists separately from `prune`

The prune cannot reach these bytes. The prune selects SESSIONS; the bytes
live on MESSAGES; and a message some surviving progression still names is
kept whatever its age. So a store can be almost entirely message content,
have every message inside a keep-window, and give a prune nothing to delete.

## What survives, what doesn't

The row, its id, its role, its timestamp and its place in every progression
all survive. What is dropped is the body, and in its place goes a value that
says a body was there and how large it was, so the removal stays legible
instead of reading as a turn that produced nothing.

## What `dry_run` actually measures

`dry_run` performs the update and rolls it back, so the reported numbers are
measurements of the operation rather than an estimate of it. That makes a
preview a WRITE that takes the same lock for the same duration — it is not a
read, and on a large store it is not a quick one.
