# ADR-0126 Amendment 11: earlier wording of the edited sentence

Amendment 11 changes some sentences of the existing record in place. This file keeps, for each, the wording it replaced, the new wording and the reason. It is a
companion record and decides nothing: [ADR-0126](../ADR-0126-provider-credential-routing-and-monthly-account-budgets.md)
is the decision record.

````text
### A11.1 — Amendment 9, Consequences: age-based expiry

Old:
> There is no age-based expiry, retry-success release, cross-account failover or provider-usage-report integration.
New:
> Amendment 11 adds an age-based expiry; there is still no retry-success release, cross-account failover or provider-usage-report integration.
Reason: an open reservation past its lease is now closed at zero by the next admission, so the sentence no longer holds.

### A11.2 — Amendment 9, Decision 3: late settlement on a reconciled row

Old:
> A late runtime settlement on a reconciled row returns `already_settled` without comparison, mutation or block; any later correction requires a sourced adjustment.
New:
> A late runtime settlement on a reconciled row returns `already_settled` without comparison, mutation or block, except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction; any other later correction requires a sourced adjustment.
Reason: a lease-closed attempt now records late complete or estimated evidence as a correction.

### A11.3 — Amendment 9, Consequences: typed reconciliation

Old:
> a later runtime settlement on the reconciled row returns `already_settled` without comparison.
New:
> a later runtime settlement on the reconciled row returns `already_settled` without comparison, except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction.
Reason: same exception for a lease-closed attempt.

### A11.4 — Amendment 9, settlement summary

Old:
> Reason-only writes on settled rows and runtime settlement after reconciliation return `already_settled` without blocking.
New:
> Reason-only writes on settled rows and runtime settlement after reconciliation return `already_settled` without blocking except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction.
Reason: same exception for a lease-closed attempt.

### A11.5 — Amendment 9, evidence rules

Old:
> Runtime evidence arriving after reconciliation returns `already_settled` without comparison, mutation or blocking; other final-evidence conflicts
New:
> Runtime evidence arriving after reconciliation returns `already_settled` without comparison, mutation or blocking, except for an attempt closed by lease expiry, where Amendment 11 records the evidence as a correction; other final-evidence conflicts
Reason: same exception for a lease-closed attempt.

### A11.6 — Amendment 11 heading

Old:
> ## Amendment 11 (proposed, 2026-11-09)
New:
> ## Amendment 11 (2026-11-09)
Reason: the amendment is accepted.

### A11.7 — Amendment 11, Decision 2

Old:
> Age is `now - created_at` read with the ledger's clock; a row exactly at the lease is still live.
New:
> Age is `now - created_at` read with the ledger's clock; a row exactly at the lease is still live. An admission that is refused rolls its transaction back, closure included: it closes nothing, and `status()` keeps counting an expired hold as open until the next committed admission closes it. Each admission decides on the totals after its own closure, so the refusal itself is correct.
Reason: a refused admission rolls the closure back; the record says so.

### A11.8 — Amendment 9, implementation limits

Old:
> MAY NOT broaden the zero table, estimate missing dimensions, reuse another attempt's evidence, release a ceiling on time alone, or silently sweep old rows.
New:
> MAY NOT broaden the zero table, estimate missing dimensions, reuse another attempt's evidence, release a ceiling on time alone before the lease of Amendment 11 has passed, or silently sweep old rows.
Reason: Amendment 11 releases a ceiling on age once the lease has passed.

### A11.9 — Amendment 9, attempt identity rules

Old:
> Age or crash alone authorizes no release.
New:
> Age or crash alone authorizes no release until the lease of Amendment 11 has passed.
Reason: same as A11.8.

````
