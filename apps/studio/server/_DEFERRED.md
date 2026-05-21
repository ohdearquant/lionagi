# Studio Backend Deferred Items

## #985 - SSRF guard on ReaderTool + AG2 NLIP

Deferred: the vulnerable code paths are in `lionagi/tools/file/reader.py` and
`lionagi/providers/ag2/nlip/models.py`, with a proposed shared helper under
`lionagi/ln/_ssrf.py`. These files are outside the allowed Studio backend scope
for this play. No safe fix can be made solely in `apps/studio/server/`.

Required future work:
- Add `lionagi/ln/_ssrf.py` with a guard that resolves hostnames and rejects
  RFC1918 (10/8, 172.16/12, 192.168/16), loopback (127/8), link-local
  (169.254/16), and metadata endpoints (169.254.169.254).
- Call the guard in `lionagi/tools/file/reader.py` before issuing any HTTP fetch.
- Call the guard in `lionagi/providers/ag2/nlip/models.py` before posting to
  the NLIP endpoint.
- Cover DNS rebinding and IPv6 unique-local ranges.

## #990 - status_source schema and migration

Deferred: the required schema and migration sources are `lionagi/state/schema.sql`
and `lionagi/state/db.py`. Those are outside the allowed Studio backend scope for
this play. `docs/adrs/ADR-0011-shows-data-model.md` acknowledges the migration
is pending.

Required future work:
- Add `status_source TEXT NOT NULL DEFAULT 'unknown'` column to the `shows` table
  in `lionagi/state/schema.sql`.
- Add a migration entry in `lionagi/state/db.py:_MIGRATION_COLUMNS` for the
  `shows` table so existing rows are backfilled to `"unknown"`.
- Add a round-trip migration test.
