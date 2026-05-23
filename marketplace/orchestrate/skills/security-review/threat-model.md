# Security Review Threat Model

Full 10-section security checklist with CWE mapping.

## 1. Authentication & authorization

- Every new endpoint / handler / RPC: where's the auth check?
- Session token handling: storage, rotation, expiry, HttpOnly / Secure flags.
- RBAC / capability checks: are they enforced on the server side, not just UI?
- "Fail closed": if auth check errors, does access default to denied or allowed?
- Multi-tenant: is `tenant_id` derived from authenticated caller, not from
  user-supplied input? A caller-supplied tenant id is always a bug.

CWE reference: CWE-285 (improper authorization), CWE-384 (session fixation), CWE-613 (insufficient session expiration).

## 2. Input validation

- Every external input (HTTP body, query string, file upload, CLI arg):
  is its type, range, length, and encoding validated?
- Model-controlled strings that become filesystem paths, URLs, SQL, shell
  commands, or serialized data — are they escaped / parameterized / regex-constrained?
- Regex validation: is it anchored (`^...$`)? Is input length capped before
  regex runs (ReDoS)?
- Structured data (JSON, YAML, TOML): does parsing allow arbitrary
  code execution (pickle, `!!python/object`, TOML overrides)?

CWE reference: CWE-20 (improper input validation), CWE-1333 (ReDoS).

## 3. Data exposure

- Error messages: do they leak internals (stack traces, query fragments,
  internal paths)?
- Logs: any PII, credentials, secrets, tokens, session ids?
- Response bodies: any fields that should be omitted (hashed_password,
  internal_id, admin flags)?
- Indirect leaks: timing attacks, cache-control on auth responses,
  distinct error messages for "user not found" vs "wrong password".

CWE reference: CWE-200 (information exposure), CWE-209 (error message information exposure).

## 4. Crypto & secrets

- Hardcoded keys, tokens, credentials? Even in tests?
- Secret material read from env / file: does it have a safe default
  fallback? Does failure to load crash loudly (fail closed) or silently
  fall back to an insecure path?
- Cryptographic primitives: weak (MD5, SHA-1, DES, RC4), deprecated (TLS
  1.0/1.1), or home-rolled? Use standard libraries.
- Random: `random` vs `secrets` / `os.urandom` for token generation?
- Password hashing: bcrypt/argon2/scrypt, never plain SHA-256.

CWE reference: CWE-321 (hardcoded key), CWE-327 (broken crypto), CWE-338 (weak PRNG).

## 5. Injection

- SQL: parameterized queries, never string-formatted.
- Shell: never `shell=True` with user input; use argv lists.
- LDAP, XPath, NoSQL: same principle.
- Template injection: Jinja / mustache with unescaped user data.
- HTML: output-encoded at the template layer? XSS?

CWE reference: CWE-89 (SQLi), CWE-78 (OS command injection), CWE-79 (XSS), CWE-94 (code injection).

## 6. File / path handling

- Paths from model output or user input: containment checked with
  `resolve()` + `relative_to(root)`?
- Zip / tar extraction: "zip-slip" — does it block `../` or absolute paths
  in archive entries?
- Temp files: predictable names (`/tmp/foo`), or `mkstemp`?
- Symlinks: does code follow them naively in security-sensitive paths?

CWE reference: CWE-22 (path traversal), CWE-377 (insecure temp file), CWE-61 (symlink following).

## 7. Supply chain

- New dependencies: typosquatting, maintainer check, license?
- Pinned versions with hashes in lockfile? Or floating ranges?
- New external service calls: is the endpoint/hostname validated?

CWE reference: CWE-1357 (insufficient provenance), CWE-829 (inclusion of external functionality).

## 8. Deserialization & parsers

- `pickle.loads`, `yaml.load` (not `safe_load`), `eval`, `exec`: any of
  these on untrusted input?
- XML parsers: XXE protection enabled?

CWE reference: CWE-502 (deserialization of untrusted data), CWE-611 (XXE).

## 9. Race conditions / TOCTOU

- File checks followed by operation ("check if exists, then open") —
  atomic or race-prone?
- Lock scope: does a mutation happen outside the lock?

CWE reference: CWE-367 (TOCTOU), CWE-362 (race condition).

## 10. Denial of service

- Unbounded loops, allocations, recursion on user input?
- Rate limiting on expensive endpoints (crypto ops, file parsing, LLM calls)?

CWE reference: CWE-400 (uncontrolled resource consumption), CWE-770 (allocation without limits).

## Source Code Reference

| File | Purpose |
|---|---|
| `lionagi/agent/permissions.py` | PermissionPolicy — allowlist / denylist / confirm modes |
| `lionagi/agent/hooks.py` | guard_destructive, guard_paths, log_tool_use built-in hooks |
| `lionagi/agent/config.py` | AgentConfig presets (coding/research) with path policies |
| `lionagi/tools/sandbox.py` | SandboxSession — git worktree isolation for speculative edits |
| `lionagi/service/connections/providers/` | Provider wrappers (OpenAI, Anthropic, Gemini, Ollama, etc.) |
| `lionagi/protocols/action/` | Tool schema generation via function_to_schema(); MCP integration |
| `lionagi/session/branch.py` | Branch facade — where tool registration and call dispatch happen |
| `lionagi/ln/` | Utilities: retry, fuzzy_json — check for unsafe deserialization paths |
