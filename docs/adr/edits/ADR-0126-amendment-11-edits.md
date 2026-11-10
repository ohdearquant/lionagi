# ADR-0126 Amendment 11: earlier wording of the edited sentence

Amendment 11 changes one clause of Amendment 9's Consequences in place. This file keeps the wording it replaced. It is a
companion record and decides nothing: [ADR-0126](../ADR-0126-provider-credential-routing-and-monthly-account-budgets.md)
is the decision record.

````text
### A11.1 — Amendment 9, Consequences: age-based expiry

Old:
> There is no age-based expiry, retry-success release, cross-account failover or provider-usage-report integration.
New:
> Amendment 11 adds an age-based expiry; there is still no retry-success release, cross-account failover or provider-usage-report integration.
Reason: an open reservation past its lease is now closed at zero by the next admission, so the sentence no longer holds.
````
