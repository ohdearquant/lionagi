# Schedule serialisation benchmark

Create two synthetic stores and run the route-service benchmark:

```sh
uv run bench/schedules_serialisation/grow_store.py \
  --output bench/schedules_serialisation/artifacts/small.db \
  --message-mib 128 \
  --metadata bench/schedules_serialisation/artifacts/small-store.json
uv run bench/schedules_serialisation/grow_store.py \
  --output bench/schedules_serialisation/artifacts/large.db \
  --message-mib 1200 \
  --metadata bench/schedules_serialisation/artifacts/large-store.json
uv run bench/schedules_serialisation/run_benchmark.py \
  --stores bench/schedules_serialisation/artifacts/small.db \
           bench/schedules_serialisation/artifacts/large.db \
  --output bench/schedules_serialisation/artifacts/results.json
uv run bench/schedules_serialisation/compare_engine_sizes.py \
  --small bench/schedules_serialisation/artifacts/small.db \
  --large bench/schedules_serialisation/artifacts/large.db \
  --output bench/schedules_serialisation/artifacts/engine-size-pairs.json
```

The runner sets the checked-out service's state-store setting to each explicit
fixture and calls `lionagi.studio.services.schedules.list_schedules` in one
process.  It never discovers or opens the user's normal StateDB.
