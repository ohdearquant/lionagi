# Time and identifier primitives

`lionagi.ln.short_id(n=12)` returns the first `n` lowercase hexadecimal digits of a
random UUID4. Lengths must be integers from 1 through 32; booleans are rejected.
Truncation reduces the available random bits, so these identifiers are intended
for existing short display and record identifiers, not authentication secrets.

`lionagi.ln.parse_duration(spec, *, allow_bare_seconds=False, allow_float=False,
max_seconds=None)` returns seconds as a float. Units are lowercase `s`, `m`, `h`
and `d`. Integer numeric syntax is the default; `allow_float=True` additionally
accepts fractional and exponent notation. `allow_bare_seconds=True` permits an
omitted unit. Surrounding whitespace is ignored; whitespace before a unit is
rejected. Invalid, zero, negative and non-finite values raise `ValueError`; `max_seconds`, when given, must be positive
and finite, must not be a boolean, and includes its boundary. Integer durations
are compared to this bound before conversion to float.

For example, `parse_duration("30m")` returns `1800.0`, and
`parse_duration("1.5h", allow_float=True)` returns `5400.0`. A caller requiring
an integer return value or a narrower textual format must enforce that contract
at its own boundary.

Elapsed time inside an async backend can be read with
`lionagi.ln.concurrency.current_time()`. CLI stream result parsers use it for
duration accounting under either asyncio or Trio. Operator heartbeat deadlines
and scheduler recovery deadlines also use it, while their existing asyncio task
and polling operations retain their backend requirements. Existing
`time.monotonic()` elapsed timers are preserved in both synchronous and
asynchronous code.

`lionagi.testing.helpers.assert_eventually` measures its polling timeout with a
monotonic clock. Wall-clock adjustments do not change the timeout; its polling
continues to use asyncio.

`lionagi.ln.coerce_created_at` accepts datetime objects, numeric epoch seconds
and timestamp strings. Naive datetime objects and naive ISO strings are read
as UTC; an existing timezone offset is retained. A trailing `Z` or `z` and
fractional seconds are normalized before ISO parsing, including on Python 3.10.
Fractions are padded or truncated to microseconds. Numeric strings take precedence
over ISO parsing: `"20250101"` means epoch seconds, not January 1, 2025.
Booleans are not timestamps and raise `ValueError`. Out-of-range numeric strings
also raise `ValueError`, including platforms that report `OverflowError` or `OSError`.

Run, team and show imports and the Claude and Codex transcript mirrors use this
conversion for timestamp fields. Naive ISO strings mean UTC regardless of the
host timezone. A conversion outside the platform timestamp range falls back to
the importer's existing filesystem timestamp or missing value.

The mirror CLI permits fractional durations and bare seconds, with an empty
optional window meaning no limit. Monitor cutoff parsing requires integer
values and a unit. Schedule declarations retain their strict digits-and-unit
grammar, inclusive 30-day maximum, and integer `interval_sec`. These callers
share positive, finite duration validation; their distinct input grammars remain.
