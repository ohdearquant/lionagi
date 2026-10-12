# lion1
the LION runtime

This project lives under `lion1/` in this repository. Run the commands below from the repository
root, entering `lion1/` first. Project paths in prose are relative to `lion1/` unless explicitly
prefixed with `lion1/` or written as a Markdown link.

## lion1

`lion1/src/lion1/` is the runtime package: a model speaks in LNDL, handlers act, and the record
remembers.
The design records under `lion1/docs/adr/` are its specification, and the code cites them as
`ADR-000N/Cn`.

Start with [docs/README.md](docs/README.md): what each module holds, the words the code uses, and the
values each state field takes. The [examples](examples/README.md) take one idea per file, and all but
the live backend one run offline.

Work on it with [uv](https://docs.astral.sh/uv/):

    cd lion1
    uv sync
    uv run pytest -n 4

`-n` spreads the suite over that many processes (pytest-xdist). A few tests hold a deadline of a
fraction of a second, so on a machine already busy keep the count below its cores; `-n 0` runs the
suite in one process.

The tests of the phone page (`lion areas`, served at `/app`) run its script under
[node](https://nodejs.org/), so `node` must be on the `PATH`; without it they fail, naming node.

A pull request's hosted check lints and runs the suite with coverage on Linux with Python 3.11. The suite
also runs with Python 3.12, 3.13 and 3.14, at the lowest declared dependency versions and against the
built wheel before a change is merged.
