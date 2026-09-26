# lion1
the LION runtime

## lionagi

`lionagi/` is the runtime package: a model speaks in LNDL, handlers act, and the record remembers.
The design records under `docs/adr/` are its specification, and the code cites them as `ADR-000N/Cn`.

Work on it with [uv](https://docs.astral.sh/uv/):

    uv sync
    uv run pytest

CI runs the suite on Linux (x86-64 and ARM) and macOS with Python 3.11, 3.12, 3.13 and 3.14, then again at
the lowest declared dependency versions and against the built wheel.
