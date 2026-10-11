# RuVector retrieval benchmark

Run from the repository root:

```bash
uv sync --extra ruvector
uv run python benchmarks/ruvector/retrieval.py --ef 100 --output /tmp/ruvector-benchmark.json
```

See [validation and measured results](../../docs/guides/ruvector-benchmarks.md). Raw JSON is stored alongside the runner. The standard-library `resource` dependency restricts this benchmark script to Linux/macOS; it is not a runtime dependency of the integration.

For the paired filtered-path optimization comparison:

```bash
uv run python benchmarks/ruvector/filtered_ab.py --output /tmp/filtered-ab.json --min-reduction 0.10
```
