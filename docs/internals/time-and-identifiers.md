# Time and identifier primitives

`lionagi.ln.short_id(n=12)` returns the first `n` lowercase hexadecimal digits of a
random UUID4. Lengths must be integers from 1 through 32; booleans are rejected.
Truncation reduces the available random bits, so these identifiers are intended
for existing short display and record identifiers, not authentication secrets.

`lionagi.ln.parse_duration(spec, *, allow_bare_seconds=False, allow_float=False,
max_seconds=None)` returns seconds as a float. Units are lowercase `s`, `m`, `h`
and `d`. Integer numeric syntax is the default; `allow_float=True` additionally
accepts fractional and exponent notation. `allow_bare_seconds=True` permits an
omitted unit. Surrounding whitespace is ignored. Invalid, zero, negative and
non-finite values raise `ValueError`; `max_seconds`, when given, must be positive
and finite and includes its boundary.

For example, `parse_duration("30m")` returns `1800.0`, and
`parse_duration("1.5h", allow_float=True)` returns `5400.0`. A caller requiring
an integer return value or a narrower textual format must enforce that contract
at its own boundary.

Elapsed time inside an async backend can be read with
`lionagi.ln.concurrency.current_time()`. CLI stream result parsers use it for
duration accounting under either asyncio or Trio. Operator heartbeat deadlines
and scheduler recovery deadlines also use it, while their existing asyncio task
and polling operations retain their backend requirements. Synchronous elapsed
time measurements continue to use `time.monotonic()`.

`lionagi.testing.helpers.assert_eventually` measures its polling timeout with a
monotonic clock. Wall-clock adjustments do not change the timeout; its polling
continues to use asyncio.
