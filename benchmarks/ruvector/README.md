# RuVector retrieval benchmark

Run from the repository root:

```bash
uv sync --extra ruvector
uv run python benchmarks/ruvector/retrieval.py --ef 100 --output /tmp/ruvector-benchmark.json
```

See [validation and measured results](../../docs/guides/ruvector-benchmarks.md). Raw JSON is stored alongside the runner. The standard-library `resource` dependency restricts this benchmark script to Linux/macOS; it is not a runtime dependency of the integration.
