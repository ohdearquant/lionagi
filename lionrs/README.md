# lionrs

The Rust tree of lionagi.

`crates/lion-core` is the Lion microkernel: production types, state machine, and
kernel API. Its Lean 4 correctness proofs live in the LNkernel repository and are
not vendored here; the code in this tree is the extraction target those proofs
describe, at the version recorded in the crate manifest.

Decisions about this tree are recorded in `docs/adr/`.
