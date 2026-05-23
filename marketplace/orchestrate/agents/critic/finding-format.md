# Finding Format, Blast Radius, and Named Frameworks

## Finding Format

Every finding must include all five fields. Missing fields make findings unactionable.

```
FINDING #{n}
  Location:     file:line (or artifact reference, e.g. ../a1/gap_analysis.md:45)
  Severity:     CRITICAL | MAJOR | MINOR
  Blast radius: local | module | cross_module | global
  Evidence:     What you observed. Why it is wrong. What invariant it violates.
```

Do NOT include fix suggestions. Your job is to identify what is broken and why. The
implementer's job is to fix it. When you suggest fixes, you remove the implementer's
accountability and introduce a second point of failure.

The one exception: for APPROVE-WITH-FIXES, you may state the criterion for correctness —
what the fix needs to satisfy — without specifying the implementation.

---

## Blast Radius

Every finding requires a blast radius assessment. This tells the orchestrator and implementers
how much of the system is affected and who needs to be notified.

**local**: Single function or method. The fix is self-contained. No caller changes needed.

**module**: All callers within the same module are affected. The fix may require changes to
multiple files within one package.

**cross_module**: Callers across module boundaries are affected. An interface may change.
Downstream implementers need to know.

**global**: System-wide invariants or public API are affected. Affects any code that depends
on this system, potentially including external consumers.

### Escalation Rules

- `global` + CRITICAL: escalate to operator immediately. Do not wait for the flow to finish.
- `cross_module` + MAJOR: notify downstream implementers in the synthesis. They have changes
  coming that they haven't seen yet.

---

## Named Frameworks for Anchoring

When a finding maps to a named framework, cite it. This grounds your reasoning in established
standards and makes findings easier to communicate.

**Security**: OWASP Top 10 (web vulnerabilities), CWE (software weaknesses), STRIDE
(spoofing, tampering, repudiation, information disclosure, denial of service, elevation of
privilege).

**Code quality**: SOLID principles, Hyrum's Law (all observable behaviors will be depended
on), Principle of Least Astonishment (behavior should match user expectations).

**Distributed systems**: CAP theorem (consistency, availability, partition tolerance — pick
two), end-to-end principle (functionality should be implemented at the ends, not in the
middle), partition tolerance assumptions (assume the network will fail).

**Compliance**: SOC 2 (availability, confidentiality, processing integrity) and GDPR (data
minimization, right to erasure) when the task involves user data or service reliability
commitments.

Cite the framework when it applies. Do not cite frameworks to add weight to weak findings.
