---
name: security-review
description: >
  Threat-model rubric for a focused security review of a code change. Use
  when reviewing auth / input validation / crypto / secrets / supply chain,
  or as the "security" dimension of a multi-perspective PR review. Pull
  this skill before starting so severity calibration and coverage stay
  consistent.
allowed-tools: [Bash, Read, Glob, Grep]
---

# Security review procedure

A focused rubric for security review of a code change. Designed as the
"security" specialist dimension of a multi-perspective PR review, but
applies equally to a standalone audit.

## Scope

Review ONLY your assigned diff (or module). Cross-module concerns go to
the architecture specialist; correctness bugs without security impact go
to correctness.

## Checklist summary (10 sections)

1. **Authentication & authorization** — endpoint auth checks, session tokens, RBAC, fail-closed, multi-tenant
2. **Input validation** — type/range/length/encoding, parameterization, regex anchoring, structured data parsers
3. **Data exposure** — error messages, logs, response fields, timing/cache leaks
4. **Crypto & secrets** — hardcoded keys, safe defaults, weak primitives, `random` vs `secrets`, password hashing
5. **Injection** — SQL, shell, LDAP, template, HTML/XSS
6. **File / path handling** — path containment, zip-slip, temp files, symlinks
7. **Supply chain** — typosquatting, pinned versions, endpoint validation
8. **Deserialization & parsers** — pickle, yaml.load, eval, exec, XXE
9. **Race conditions / TOCTOU** — atomic file operations, lock scope
10. **Denial of service** — unbounded loops on user input, rate limiting

See [threat-model.md](threat-model.md) for full per-section detail and CWE mapping.

## Severity calibration

- `CRITICAL` — exploitable now, no special conditions: auth bypass, RCE, full data exposure
- `HIGH` — data exposure / auth gap requiring specific conditions
- `MEDIUM` — mis-scoped tokens, weak validation, missing rate limit
- `LOW` — defense-in-depth hardening
- `INFO` — notes for future consideration

**Rule of thumb**: CRITICAL vs HIGH — user action required to exploit? No = CRITICAL, chained = HIGH.
HIGH vs MEDIUM — exploitable at attacker's expected access level? Yes = HIGH, needs escalation = MEDIUM.

## Output format

| Severity | Location | Description | Suggested fix | Confidence | Runtime-context caveats |
|---|---|---|---|---|---|
| HIGH | `file.py:112` | Path not containment-checked | resolve() + relative_to(root) | High | Requires malicious planner output |

Verdict at top: `APPROVE` | `REQUEST CHANGES` | `REJECT`.
Cite `file:line`. Reference CWE where clear (CWE-22 path traversal, CWE-89 SQLi).

## Ground rules

1. Don't invent requirements.
2. Flag runtime context you need but can't see.
3. Be honest about confidence.
4. Review against committed HEAD of the PR branch.
