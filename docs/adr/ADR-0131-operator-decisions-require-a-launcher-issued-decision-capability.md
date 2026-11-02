# ADR-0131: Operator decisions require a launcher-issued decision capability

- **Status**: Accepted
- **Kind**: Aspirational
- **Implementation-status**: partial
- **Area**: studio
- **Date**: 2026-10-17
- **Relations**: changes the Studio daemon's route contract for the two Operator decision routes and
  the pairing routes (a versioned, breaking security-contract change, D7); keeps the no-token
  admission rule of the daemon's middleware unchanged for every other route; read with ADR-0076 (the
  daemon's route registry and local control plane)

Purpose: in a daemon without `LIONAGI_STUDIO_AUTH_TOKEN`, any process that can reach the listener can
allow or deny an Operator proposal, so a run whose automatically allowed tool can make HTTP requests
to the daemon can approve its own proposals. This record makes a decision require a capability that
only the daemon mints, handed to a page by its launcher or, for a reconnect, over a channel only the
daemon's own OS user can open, and that no HTTP request can obtain.

Landing steps 1, 2 and 2b are in: the decision-capability store and the decision rows that record
its principal, minting at launch, the retired HTTP mint route, redemption returning the capability,
the page's use of it, and the same-user reconnect channel the CLI pairs over. Of step 3, `--dev`
delivers a code bound to the page origin Vite reports, and the pairing route admits an origin a
launcher in the same process named; `--no-frontend` and the desktop shell do not deliver one yet.
Step 4's principal check and refusals are not in; decisions are still admitted under
contract version 1, every decision request accepted today is still accepted, and a request without
the capability is recorded unattributed.

## Context

**P1 — the decision routes check no principal.** `confirm_operator_proposal` and
`decide_operator_proposal` (`lionagi/studio/services/operator.py:335-397`) call the coordinator's
`decide()` (`lionagi/studio/operator/coordinator.py:1016`), which claims the proposal in the store
(`lionagi/studio/operator/store.py:2172`). The proposal's hash and version bind what is decided, not
who decides. With no token configured, the middleware admits a request that carries no `Origin` and
no `Sec-Fetch-Site` (`lionagi/studio/app.py:77`, `:123`), so a command-line client, a script and a
tool's HTTP request are all admitted.

**P2 — a code that paired a page could be minted by any such request.** `POST
/api/studio/pairing-codes` (`lionagi/studio/services/pairing.py:28-46`) minted a 120-second code
(`lionagi/studio/launch_access.py:33`, `:49`) for a request without browser provenance, and `POST
/api/studio/pair` (`pairing.py:48`) redeemed it for the daemon's one process-wide bearer
(`launch_access.py:43`, `:63`). A test pinned that the command line could mint and that the minted
code redeemed (`tests/studio/test_hosted_hardening.py:967`). The CLI used that route to pair a page
with a daemon that was already running (`lionagi/studio/cli.py:274`). So pairing was not a boundary:
a tool that could send HTTP could mint, redeem with a forged `Origin`, and decide. This paragraph
describes the daemon before the HTTP mint route was retired (its line numbers are at `124b14193`).

**P3 — the page keeps its bearer in memory only.** The frontend holds the paired bearer in a module
variable (`apps/studio/frontend/src/lib/pairing.ts:27`); a reload loses it. Nothing reachable over
HTTP can read it. This paragraph describes the page before it kept a launch bearer in
`localStorage` (its line number is at `47fe93701`; `docs/internals/studio.md` has the current rule).

**P4 — the permission bridge reads storage, not HTTP.** The provider-permission bridge polls the
proposal's status from the store whose path it is given (`lionagi/studio/operator/permission_mcp.py`),
so whatever the decision routes record is what a run waiting on a permission sees.

## Decision

**D1 — decision authority is separate from API admission.** Deciding a proposal requires a
**decision capability**: a random 256-bit secret, stored by the daemon only as its hash, bound to a
random principal id, the daemon's start epoch, the frontend origin it was redeemed from, and the
scope `operator:decide`. API admission (the token, the no-token middleware rule) is unchanged and no
longer sufficient to decide. `LIONAGI_STUDIO_AUTH_TOKEN` admits requests as today and cannot decide
or mint.

**D2 — codes are minted in process, at launch or over a same-user reconnect channel, never over
HTTP.** The daemon mints a code in process before it serves, and the launcher delivers it in the URL
fragment of the page it opens, as today. The HTTP mint route is retired: it answers 410 after
admission, whatever the headers or token. Redemption (`POST /api/studio/pair`) stays an HTTP route: a
code is single-use, expires after 120 seconds, is bound to the origins the launcher named, and is
consumed atomically; a daemon restart invalidates every code and capability.

A later `li studio` against a daemon that is already running obtains its code over the **reconnect
channel**, so a closed tab never leaves a pending proposal undecidable: the daemon listens on a unix
socket in its state directory, created with mode 0600 inside a 0700 directory before it serves and
removed at exit (a socket file left by a daemon that no longer runs is replaced at start). On each
connection the daemon reads the peer's user id from the socket (`SO_PEERCRED` on Linux,
`LOCAL_PEERCRED` on macOS, each read with `getsockopt` from the standard `socket` module) and
refuses any id other than its own; for its own user it mints one code in process, under every code
rule above, and returns it on the socket with the channel's port-proof key, a random 256-bit value
drawn when the channel starts; the socket carries nothing else. The socket answers for whichever
daemon uses the state directory, which need not be the daemon on the port the CLI was given, so the
CLI then sends `/health` on that port a fresh challenge, and the daemon there answers with
HMAC-SHA256 under the key over the challenge and the TCP port the request arrived on. A second
daemon on that port, or a listener relaying to the right daemon from another port, cannot produce
it; the CLI then exits non-zero without showing the code, which expires unused. Otherwise it opens
a page with that code as a launch does. It never stops a running daemon itself. The channel is not an HTTP route and
no TCP listener serves it. Any process running as the daemon's own OS user can obtain a code through
it, which D6 already places outside the guarantee. On a platform without unix sockets there is no
reconnect, and the CLI says to restart the daemon.

**D3 — a redeemed code yields one capability per redemption.** Redemption returns the admission bearer
as today and, separately, the decision capability. The page keeps the capability in `sessionStorage`
(it survives a reload of that tab, is gone with the tab, and is not readable over HTTP). It expires
eight hours after redemption and on explicit disconnect; no ordinary request renews it. Two tabs
paired by two codes hold two capabilities; a duplicated tab shares one. The principal names a
redemption, not a person.

**D4 — `decide()` requires a verified principal.** The page sends the capability in an
`X-Studio-Decision-Token` header on the two decision requests only, to the API origin it was paired
with. The route resolves it to a principal and passes that principal to `decide()`, which revalidates
it at the claim and records the principal id, epoch and issuance method with the decision in the
same transaction. No caller can pass an identity string. A missing, unknown, expired or revoked
capability is refused with 401 and `{"detail": {"code": "operator_auth_required", "message": "Pair
this page to decide", "retryable": false}}`; a valid capability from the wrong origin or without the
scope with 403 and `operator_forbidden` in the same envelope. Reads the page needs before pairing,
`/health` and `OPTIONS` stay as they are. Historical decisions stay unattributed.

**D5 — launch paths deliver codes or decide nothing.** The default, `--no-frontend`, `--dev` and
hosted launches deliver the code for their exact origins. The desktop shell delivers it over its own
launcher channel, never in a page's static script. A Docker launch has no channel the record
accepts yet (container output and the container's environment are readable by what runs inside
it), so a Docker daemon serves with decisions disabled until an adapter is specified in its own
record; its refusals say so.

**D6 — what this design stops and what it does not.** Stopped: a tool, script or other local account
that can only make HTTP requests, and a page that was not paired, can neither allow nor deny a
proposal, with known ids and hashes, a forged `Origin` or `Sec-Fetch-Site`, or the admission token.
Not stopped: a stolen capability; script running in a paired page (an extension, a compromised
dependency); code running as the same OS user that opens the reconnect socket, reads a browser
profile or process memory, or writes the store directly; and any other daemon route, whose admission
is unchanged. A decision does not prove that a human clicked; it proves that a paired page, or a
client running as the daemon's own OS user, sent it. Egress containment of tools is not required for the
HTTP-only guarantee and is required for anything broader.

**D7 — the contract is versioned and fails closed.** `/health` and an OpenAPI extension publish
`operatorDecisionContract: 2`. A page that does not find version 2 does not offer decisions. There is
no opt-out: a deployment that wants the old behaviour has none. The release notes, the route contract
and the Studio pages state the new header, the two refusals, the retired mint route, the reconnect
rule and the token's narrower meaning.

## Landing order

1. D1's capability store, with the decision routes still accepting today's requests. The contract
   stays version 1.
2. D2: issuance at launch and the 410 mint route; D3: redemption returns the capability; the frontend
   stores and sends it, and still decides under version 1.

   2b. D2's reconnect channel, with the CLI's reconnect moved from the HTTP mint to it. It ships in
   the same release as step 2's 410, so no released daemon is without a reconnect, and before step 4.
3. D5's adapters, one launch path per change, so that every launch path except Docker delivers a code
   before any decision requires one. Today the hosted launch and `--dev` deliver one.
4. D4: the principal check in `decide()` and the refusals, Docker's decisions-disabled refusal, and
   D7's version-2 marker with the page's version gate, in one release. From this step the contract
   is version 2.

## Tests that prove it

- The attack, end to end: with a real pending permission proposal, a client that sends only HTTP reads
  its ids, tries to mint (absent and forged headers, with and without the admission token), tries to
  redeem with a forged `Origin`, and tries to allow and to deny. Every step is refused, the proposal
  stays pending, the bridge never sees `allow`, and nothing executes. A paired page's decision is the
  positive control. Repeated for an application proposal and for an already-decided one.
- Mint: 410 for every header combination and for the admission token; the test that pins the
  command-line mint today (`tests/studio/test_hosted_hardening.py:967`, line numbers at `124b14193`)
  is replaced by this one.
- Codes: unknown, expired, wrong origin, replayed and from a previous daemon start are refused; two
  concurrent redemptions of one code yield one capability; a refused wrong-origin attempt does not
  consume a valid code.
- Reconnect: with a pending proposal and its paired tab closed, a second `li studio` pairs a new page
  through the socket and that page decides the proposal, with the waiting run still alive; a peer with
  another user id is refused; the socket is mode 0600 in a 0700 directory and gone after exit; its
  code obeys every code rule above; no HTTP request, with any headers or the admission token, obtains
  a code.
- Capability: expiry, disconnect and restart each refuse a later decision; a capability sent to
  another origin, or with a redirect, is refused; reads and `/health` work without it.
- `decide()` called directly without a verified principal fails before touching the store; a decision
  and its audit row commit together or not at all; a revoked capability cannot claim; a concurrent
  replay executes once.
- A page that reads contract version 1, or none, offers no decision.
- Docker launch: decisions refused with the stated reason.

## Run-contract impact

The decision rows gain the principal id, epoch and issuance method (a store change, versioned with
the store's own migration). The route contract changes as D4 and D7 state. Readers of both are told
before the release that ships step 4.

## Out of scope

- A Docker adapter.
- Authority for any route other than the two decision routes, and isolation of tools from the store
  or the browser profile.
