# Contributing

Read [`CONTRIBUTING.md`](https://github.com/ohdearquant/lionagi/blob/main/CONTRIBUTING.md) at the repo root for pull-request guidelines, and [`AGENT.md`](https://github.com/ohdearquant/lionagi/blob/main/AGENT.md) for the contributor workflow (commands, coding standards, testing).

## Preview documentation changes

The docs gate checks prose, links, executable contracts, and the complete
production build:

```bash
uv run pytest -n0 tests/docs
uv run mkdocs build --strict
```

For a live local preview:

```bash
uv run mkdocs serve
```

Keep examples aligned with live Python signatures and `li ... --help`. The
strict build is also run before GitHub Pages deploys, so missing assets and
broken internal links cannot be published silently.

Next: [Architecture decisions](adr/README.md) · [Code of conduct](code-of-conduct.md)
