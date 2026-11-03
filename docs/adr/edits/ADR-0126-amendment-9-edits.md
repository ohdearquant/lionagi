# ADR-0126 Amendment 9: earlier wording of the edited sentences

Amendment 9 to ADR-0126 changes some sentences of the existing record in place, in the base decision and in
Amendments 5, 7 and 8, instead of restating them in a new section. This file keeps, for each edited sentence, the
wording it replaced, the new wording and the reason, so the record as it stood before Amendment 9 can be read from
the repository. It is a companion record and decides nothing: [ADR-0126](../ADR-0126-provider-credential-routing-and-monthly-account-budgets.md) is the decision record.

## How to read this file

- The edits made when the amendment was accepted come first, as written in the accepted amendment text. Their line
  ranges refer to ADR-0126 at commit c6a3b4af, the record they were applied to. The clause index after them lists
  the other paragraphs of the record that mention an affected rule and why each was left unchanged.
- The edits made afterwards, while the amendment was reviewed, follow in the order they were applied.
- Quoted text is reproduced exactly, so the edits sit in plain-text blocks rather than rendered Markdown.

## Edits made on acceptance

On acceptance the Amendment 9 section was inserted after Amendment 8 with the heading edit below, and the 97 edits
that follow it were applied to the existing record.

````text
### A0.1 — Amendment 9 heading

Old:
> ## Amendment 9 (proposed, 2026-11-02) — ending a hold with rejection evidence or observed stream usage
New:
> ## Amendment 9 (2026-11-02) — ending a hold with rejection evidence or observed stream usage
Reason: The amendment's heading drops "proposed" on acceptance.

## Explicit replacement blocks

Old quotations reproduce only changed record sentences (or indivisible headings, table rows and interface declarations), from ADR-0126 at `c6a3b4af`. Line ranges refer to that record. These edits apply only on acceptance; unquoted text remains byte-identical. To add a sentence, its preceding record sentence is quoted and retained in New.

Abbreviations in Old retain the record’s spelling: `_ledger.py`, `_router.py` and `_types.py` denote their files under `lionagi/service/accounts/`, and `messages.py` denotes `lionhub/src/lionhub/providers/anthropic/messages.py`. Colon-only continuations inherit the preceding file citation in the record. New code citations use full repository paths at `c6a3b4af`; explicitly historical attributions remain historical. Reasons name removed citations in their original spelling, with resolved paths for colon-only continuations where needed.

### R01 — Status line

#### R01.1 (lines 3–3)
Old:
>
> - **Status**: Accepted, amended 2026-10-14, 2026-10-26, 2026-10-28, 2026-10-30, 2026-11-01 and 2026-11-02 (see Amendments 1 to 8)
New:
> - **Status**: Accepted, amended 2026-10-14, 2026-10-26, 2026-10-28, 2026-10-30, 2026-11-01 and 2026-11-02 (see Amendments 1 to 9).
Reason: Extends the accepted list only on acceptance; this amendment remains proposed until then. Dropped or re-cited attribution: Amendments 1 to 8, Amendment 1 to 8.

### R02 — Implementation status

#### R02.1 (lines 6–6)
Old:
> Two clauses remain.
New:
> Within that accepted scope, two clauses remain.
Reason: Limits the two remaining implementation gaps to the accepted baseline scope.

#### R02.2 (lines 7–11)
Old:
> The ledger refuses a duplicate attempt id and settles an attempt once (an identical repeat changes nothing, a different one blocks the account), but nothing is consumed when the request is dispatched. D6's revoke-and-join before "not sent": the not-sent settle is chosen by whether the request was entered, with no ticket to revoke and no join of the worker.
New:
> The ledger refuses a duplicate attempt id and settles an attempt once (an identical repeat changes nothing, a different one blocks the account in this implemented baseline), but nothing is consumed when the request is dispatched. D6's revoke-and-join before "not sent": the not-sent settle is chosen by whether the request was entered, with no ticket to revoke and no join of the worker. Amendment 9 is unimplemented; the preceding implementation claims apply through Amendment 8 only. The required Amendment 9 runtime-after-reconciliation no-op is a new exception, not implemented by that baseline.
Reason: Limits the duplicate-conflict claim to the implemented baseline and identifies the unimplemented Amendment 9 requirements, including runtime-after-reconciliation precedence.

### R03 — D6 table rows

#### R03.1 (lines 1548–1550)
Old:
> | 5xx, timeout, transport loss | Unknown cost keeps ceiling; no quota/auth mark inferred. | Existing bounded retry policy, same account and a new row. |
> | Other 4xx | Classify request rejection; keep ceiling without complete billing evidence. | None. |
> | Cancellation, process death or stream truncation | Settle any complete evidence already captured; otherwise retain ceiling. | No cancellation replay. |
New:
> | 400 `invalid_request_error` | Classify request rejection and settle zero under Amendment 9's bounded typed-evidence policy, irrespective of class. | None. |
> | 529 `overloaded_error` | Settle zero under Amendment 9's bounded typed-evidence policy; no quota/auth mark inferred. | Existing bounded retry policy, same account and a new row. |
> | Other 5xx (including nonqualifying 529), timeout, transport loss | Unknown cost keeps ceiling; an eligible early-ended stream instead records E under Amendment 9. No quota/auth mark is inferred, but E > C records `ceiling_exceeded` and clears warmth. | Existing bounded retry policy, same account and a new row; no automatic retry after stream output starts. |
> | Other 4xx, including 413; 400 failing Amendment 9's typed-evidence rule | Classify request rejection; keep ceiling without complete billing evidence. | None. |
> | Cancellation, process death or stream truncation | Settle any complete evidence already captured, or an eligible early-ended stream estimate E under Amendment 9; otherwise retain ceiling. E > C blocks with `ceiling_exceeded` and clears warmth, including on cancellation. Process death without a committed settlement retains the durable ceiling. | No cancellation replay. |
Reason: Adds the closed 400/529 zero policy and eligible stream estimate, retaining any complete evidence, retry limits, failed-commit retention and block-precedence clearing. The unquoted 401/403 and 429 rows remain unchanged; 413 remains in Other 4xx.

### R04 — D6 terminal-evidence sentences

#### R04.1 (lines 1552–1556)
Old:
> Streams merge the initial usage and later cumulative usage fields by replacement, not addition, and settle only with complete terminal evidence. HTTP usage is intercepted before generic stream projection and before yielding to consumers; the current generic projection keeps raw events (`lionagi/service/connections/endpoint.py:1206-1286`).
New:
> Amendment 9 permits zero settlement only for its closed HTTP status/type pairs as a policy interpretation of the quoted failed-request statement. Streams merge initial usage and later cumulative fields by replacement, not addition; complete terminal evidence uses exact pricing, while only eligible early-ended streams use Amendment 9's estimate. Intercept HTTP accounting before redaction buffering, generic stream projection and consumer yields. The accepted record describes generic projection as retaining raw events (`lionagi/service/connections/endpoint.py:1206-1286`); the additional pre-redaction accounting parser is proposed.
Reason: Adds typed zeros and estimates and moves accounting before output redaction; preserves cumulative replacement and historical projection attribution.

### R05 — D5 budget qualification

#### R05.1 (lines 1514–1515)
Old:
> Settlement replaces C by A in one transaction. If A exceeds C, record the true A and block the account with `ceiling_exceeded`; never clamp the bill.
New:
> Complete-usage settlement replaces C by A in one transaction. If A exceeds C, record the true A and block the account with `ceiling_exceeded`; never clamp the bill. Amendment 9 instead replaces C by zero for qualifying typed rejections or by E = Aobs + M for eligible early-ended streams. E includes a policy margin, not just provider usage: record E without clamping and block with `ceiling_exceeded` if E > C, even if the eventual true bill is within C. Every block clears warmth.
Reason: Adds zero and estimated replacement of C, retaining the true-A block and adding an unclamped E block with warmth clearing.

#### R05.2 (lines 1516–1518)
Old:
> Keeping real charges within B additionally assumes the provider respects the documented token/rate bounds and all spending on the allocation goes through this ledger.
New:
> Its recorded costs now include estimates. For complete-usage accounting, keeping real charges within B additionally assumes the provider respects the documented token/rate bounds and all spending on the allocation goes through this ledger. For stream estimates those assumptions are insufficient: unobserved billed usage may exceed M, and E is not a provider-charge bound.
Reason: Qualifies real-charge assurance because the policy margin does not bound unobserved charges, while retaining the complete-usage assumptions.

### R06 — Consequences: unknown attempts

#### R06.1 (lines 1596–1598)
Old:
>
> - Unknown attempts and conservative ceilings reduce availability deliberately. A cancellation can remain unresolved indefinitely; explicit sourced reconciliation is required, never a timeout that invents a free request.
New:
> - Unresolved unknown attempts and conservative ceilings deliberately reduce availability; qualifying rejection and stream evidence can now end those holds. Amendment 9 permits policy-qualified rejection zeros and explicitly estimated early-ended stream settlements, including explicit operator reconciliation of qualifying current-month open rows with typed evidence and a retained source. Other unknown attempts retain their ceilings in their original admission month until ordinary sourced reconciliation; a cancellation can remain unresolved indefinitely. Month rollover does not erase or settle old rows, and elapsed time never invents a free request.
Reason: Adds evidenced zero and estimate release paths, including sourced current-month typed reconciliation; ambiguous holds and the no-timeout-release bar remain.

### R07 — Rejected alternative

#### R07.1 (lines 1661–1663)
Old:
> Failover can duplicate work on another credential; expiry or assumed zero restores money with no evidence.
New:
> Failover can duplicate work on another credential; expiry or zero assumed without the closed status/type evidence restores money without an authorized basis. Expiry and assumed zero are not permitted. Amendment 9's zero settlement instead requires its closed HTTP status/type pair, as a policy interpretation of the quoted failed-request statement; status alone, elapsed time and a successful retry are not sufficient evidence. Its separate early-ended stream rule explicitly permits an estimate from observed usage plus a policy margin; that estimate is not complete billing evidence or a bound on the final charge. Every other unknown attempt retains its ceiling until explicit sourced reconciliation.
Reason: Distinguishes typed zero and observed-cost estimation from prohibited evidence-free release; retains duplicate-work risk and requires sourced reconciliation for other unknown attempts.

### R08 — Amendment 7 Decision 1

#### R08.1 (lines 883–884)
Old:
> 1. **Two closed-set fields.** A routed rejection carries `provider_error_type` and `rejection_class`.
New:
> 1. **Two closed-set fields.** A routed request rejection carries `provider_error_type` and `rejection_class`.
Reason: Narrows the diagnostic-field subject from every routed rejection to the request-rejected branch.

#### R08.2 (lines 885–889)
Old:
> The fields are set on the request-rejected branch only. That branch settles the attempt with an `UnknownOutcome` whose reason code is `request_rejected`, so that outcome carries the two fields and raises the error that carries them. On every other outcome, and on an `UnknownOutcome` with any other reason code, both are absent.
New:
> These diagnostic fields apply only to the request-rejected branch: qualifying HTTP 400 uses `RejectionZero`, other request rejections use `UnknownOutcome(request_rejected)`, and both propagate the fields to the routed error. HTTP 529 instead leaves both rejection columns and outward fields null; qualifying `overloaded_error` appears only in structured settlement evidence. Reconciliation retains already-recorded fields. Other settlements introduce neither field, and UnknownOutcome with another reason carries neither.
Reason: Distinguishes qualifying 400 final evidence, open request rejection and null 529 diagnostics; reconciliation retains already-recorded fields.

### R09 — Amendment 7 Decision 3 settlement order

#### R09.1 (lines 904–913)
Old:
> That read comes before the settlement, so it runs under `CancelScope(shield=True)` bounded at five seconds, like the settlement writes Amendment 5 shields: a pending cancellation is still delivered after the failure is recorded (Amendment 5), and a read that has not finished within the bound records `other` for both fields. A body over the bound, a body that does not decode, a body with no `error.type` string, or one whose `error.type` is not in the provider's set records `other` for the type. A body with no `error.message` string records `other` for the class. The settlement order of D6 is unchanged: the attempt is settled `request_rejected`, now with both fields, and then the error is raised.
New:
> That read comes before the settlement, so it runs under `CancelScope(shield=True)` bounded at five seconds, like the settlement writes Amendment 5 shields: a pending cancellation is still delivered after the failure is recorded (Amendment 5), and a read that has not finished within the bound returns `other` for both parser fields. On the request-rejected branch those fields are recorded; on HTTP 529 the adapter keeps both rejection columns and outward diagnostic fields null and retains the ceiling. A body over the bound, a body that does not decode, a body with no `error.type` string, or one whose `error.type` is not in the provider's set returns `other` for the parser type; the request-rejected branch records it, while HTTP 529 leaves the rejection columns and outward fields null. A body with no `error.message` string returns `other` for the parser class; HTTP 529 does not persist that diagnostic class. The settlement order remains accounting before outward failure: qualifying 400 uses `RejectionZero`, otherwise `UnknownOutcome(request_rejected)` records both fields. A possible 529 uses the same bounded read before the generic 5xx arm; only its exact pair qualifies, with type in structured evidence only.
Reason: Extends bounded parsing to 529, separating parser fallback from outward null fields and replacing only qualified unknown holds with zeros. Dropped or re-cited attribution: D6, `D6`.

### R10 — Amendment 7 Decision 4 drop rule

#### R10.1 (lines 914–918)
Old:
> 4. **The drop rule.** The body, the decoded object and the message string exist only inside the one function that returns the two field values. That function catches its own failures and returns `other` for both. Its only log line is a debug record of a failure's exception type name, with no traceback. Its input is never logged, never stored, never placed in an exception's arguments, and never left reachable from `__context__` or `__cause__`.
New:
> 4. **The drop rule.** The body, decoded object and raw message remain inside the bounded classifier/read function. That function catches its own failures, including malformed JSON, non-object root or error, missing fields, decoded-body overflow, read failure and timeout, returning `other` for both fields on failed classification. Capture failure follows the isolation rule below. HTTP 529 discards the parser diagnostics from the rejection columns and outward fields. Classifier failures log only the exception type at DEBUG, without traceback; enabled capture failures additionally emit Amendment 9's fixed WARNING event and closed failure code. No input, raw body or message is logged or placed in exception arguments, `__context__` or `__cause__`.
Reason: Retains raw-value confinement and self-contained classification failure handling; adds fixed capture-failure events and refers capture failure isolation to the following rule.

#### R10.2 (lines 920–922)
Old:
> On the streamed attempt the bounded read of Decision 3 also runs inside that function, so no string or bytes value holding the body is bound in the attempt's own frame.
New:
> On the streamed attempt the bounded read of Decision 3 also runs inside that function, so no string or bytes value holding the body is bound in the attempt's own frame. The sole storage exception is Amendment 9's enabled HTTP 400 class-`other` capture: the classifier passes only redacted, capped text and validated metadata to the private writer, under its reader and retention policy. Header construction supplies the exact-string redaction callable using an already-unwrapped credential; the classifier never unwraps one. Capture failure changes neither classification, accounting nor outward errors. The streamed `_classify_streamed_rejection` retains the decoded `aiter_bytes()` read under `CancelScope(deadline=..., shield=True)` (`lionhub/src/lionhub/providers/anthropic/messages.py:478-503`). Disabled capture retains no text; enabled capture grants no text input to financial or request control.
Reason: Adds only the enabled bounded diagnostic-file exception with its supplied redactor, reader and retention policy; preserves streamed read shielding and isolates capture failure from classification, accounting and outward errors.

### R11 — Amendment 7 Decision 5 evidence and persistence

#### R11.1 (lines 936–941)
Old:
> The settlement that records `request_rejected` writes them in the same statement as the reason, under the same condition, so the first reason recorded on an open attempt stands together with its two fields, as the reason alone does today. No other settlement writes them. They are added to an existing version 1 ledger the same way the warm-prefix table is, when missing, without changing `user_version`, and the check that lets a complete ledger through also requires them.
New:
> An open `request_rejected` reason writes them atomically and retains its first reason/fields. A qualifying 400 writes them with zero settlement; 529 leaves both null and stores its qualifying type only in structured evidence. Typed reconciliation preserves existing fields, and a new final basis snapshots them before replacing outcome. Other settlements introduce neither field, and later classification never replaces stored class. They are added to an existing version 1 ledger the same way the warm-prefix table is, when missing, without changing `user_version`, and the check that lets a complete ledger through also requires them. Amendment 9 additionally requires its evidence column and a locked re-read of extension completeness.
Reason: Adds atomic qualifying-400 fields and failure snapshots, retaining first-failure evidence and locked extension migration.

### R12 — Amendment 7 Decision 6 behavior boundary

#### R12.1 (lines 944–945)
Old:
> 6. **No behaviour keys on them.** Retry, blocking, account choice and warm-prefix clearing ignore both fields. A request rejection is still never retried and never moves to another account.
New:
> 6. **Class remains diagnosis only.** Settlement, retry, blocking, account choice and warm-prefix clearing never read rejection class; diagnostic capture may use class `other` to select a message for inspection. Amendment 9 permits settlement to read the HTTP status and parsed error type. The resulting released ceiling changes D3 utilization and W eligibility indirectly, though their formulas are unchanged. A request rejection is still never retried and never moves to another account. Original ending reason and any block govern warmth under Amendment 9. Repeated typed zero rejections do not automatically block an account. Contradictory prior typed failure evidence can also cause settlement_conflict and clear warmth; diagnostic class cannot cause that conflict.
Reason: Permits typed financial settlement and diagnosis-only class selection for capture; class never controls financial or request actions, and fixed binding remains.

### R13 — Amendment 7 Decision 7 storage scope

#### R13.1 (lines 947–948)
Old:
> No provider text is stored anywhere by this amendment. Keeping a bounded message would be a separate amendment with its own reader policy.
New:
> Provider text stays absent from the ledger and routed errors. Amendment 9 supplies the formerly deferred bounded-message reader policy, authorizing only its enabled private diagnostic file; no other provider-text storage is permitted here.
Reason: Replaces the blanket provider-text storage ban and deferred reader-policy clause with Amendment 9’s enabled private-file exception; ledger and routed errors remain text-free.

### R14 — Amendment 7 diagnosis guarantee

#### R14.1 (lines 988–990)
Old:
> The message stays dropped, so a new wording is found by reproducing the request on the unrouted path, which keeps the body, and is then added to the table by amendment.
New:
> With capture disabled the message stays dropped; enabled Amendment 9 capture may supply redacted bounded wording. Alternatively, reproduce the request on the unrouted path, which keeps the body, and add a new class only by amendment. Neither route guarantees diagnosis or establishes the original attempt's billing.
Reason: Allows opt-in captured wording as a diagnostic route; neither capture nor reproduction proves billing or guarantees diagnosis.

### R15 — Amendment 7 verification

#### R15.1 (lines 1021–1023)
Old:
>
> - For each class, a 400 body carrying the documented message, through both the non-streamed and the streamed routed attempt, raises `AccountRoutingError(request_rejected)` with that class and `provider_error_type="invalid_request_error"`, and the ledger row holds the same two values.
New:
> - For each class, a 400 body carrying the documented message, through both the non-streamed and the streamed routed attempt, raises `AccountRoutingError(request_rejected)` with that class and `provider_error_type="invalid_request_error"`, and the ledger row holds the same two values with outcome `rejection_zero` and zero cost under Amendment 9.
Reason: Requires qualifying 400 fixtures to retain both diagnostic fields and settle rejection_zero at zero cost.

#### R15.2 (lines 1028–1029)
Old:
>
> - A cancellation already pending when a 400 arrives is delivered after the attempt is recorded `request_rejected` with its two fields, on both attempts, as Amendment 5 requires today.
New:
> - A cancellation already pending when a 400 arrives is delivered after the attempt is recorded `rejection_zero` with its two fields for a qualifying 400, otherwise open `request_rejected`, on both attempts.
Reason: Requires pending cancellation to follow qualifying zero or nonqualifying open request_rejected accounting on both HTTP paths. Dropped or re-cited attribution: Amendment 5, `Amendment 5`.

#### R15.3 (lines 1037–1038)
Old:
> An arm that deliberately places the message in the error's arguments fails them.
New:
> An arm that deliberately places the message in the error's arguments fails them. Run these checks with capture both disabled and enabled. Only an eligible enabled capture may put a redacted sentinel in the designated private file; the ledger, logs, error and traceback assertions are unchanged. Verify the chosen credential is absent even when the cap cuts near it, and all diagnostic failures leave classification and settlement unchanged.
Reason: Keeps the sentinel assertions and extends their execution to enabled and disabled capture; only the designated private file is excepted.

#### R15.4 (lines 1039–1042)
Old:
>
> - An unknown value passed to `AccountRoutingError`, `UnknownOutcome` or the ledger write raises `ValueError`.
> - Neither field changes retry, blocking or warm-prefix clearing: a rejection of each class is not retried and clears the same warm-prefix rows as a `request_rejected` settlement does today.
New:
> - An unknown value passed to `AccountRoutingError`, `UnknownOutcome`, either new evidence variant or the ledger write raises `ValueError`, except that a 400 `RejectionZero` with absent class defaults to `other`, and invalid `StreamUsageEstimate.request_id` normalizes to null at construction and deserialization under Amendment 9 Decision 6. An output bound above a known model’s maximum raises `ValueError` before writes.
> - No rejection class changes settlement, retry, blocking, account choice or warmth. Each class of qualifying 400 settles zero, is not retried and clears warmth; changed totals may change subsequent eligibility/order. Qualifying 529 settles zero, clears warmth, keeps both rejection columns/outward fields null and retains its retry classification.
Reason: Extends validation verification to the new variants, absent-class default, normalized stream request ids and above-model output bounds; verifies class-independent accounting and the distinct 529 field/retry policy.

#### R15.5 (lines 1043–1045)
Old:
> A settlement with any other outcome leaves both columns null, and a later reason written to an attempt that already records `request_rejected` changes neither the reason nor the two fields.
New:
> Only the specified 400 settlement introduces those fields outside `UnknownOutcome(request_rejected)`; reconciliation preserves them. A later reason-only write changes neither first reason nor fields. A qualified closure records that snapshot before replacing outcome; verify contradictory typed evidence refuses and blocks, while a changed diagnostic class neither determines eligibility nor overwrites the stored class.
Reason: Extends column-persistence verification to qualifying 400 closure and reconciliation, first-failure snapshots, contradictory typed-evidence blocking and preserved diagnostic class.

### R16 — Amendment 8 Decision 3

#### R16.1 (lines 1050–1051)
Old:
> Purpose: tell a routed rejection caused by the account's billing state apart from one caused by the request, through the class field Amendment 7 added, without changing any behaviour.
New:
> Purpose: tell a routed rejection caused by the account’s billing state apart from one caused by the request through the class field Amendment 7 added. Amendment 9 allows diagnostic capture only for class `other`; neither billing class controls settlement, routing, retry, blocking or warmth.
Reason: Qualifies the former all-behaviour prohibition solely for diagnostic capture.

#### R16.2 (lines 1089–1089)
Old:
> Amendment 7, Decision 6 holds for both classes.
New:
> Amendment 7, Decision 6 as amended by Amendment 9 holds for both classes: no financial or request-control action reads message class; only diagnostic capture selects class `other`.
Reason: Replaces blanket no-behaviour language with the explicit diagnostic-only capture exception; both billing classes still cannot control blocking.

#### R16.3 (lines 1092–1094)
Old:
> Blocking an account on a class would key behaviour on message text, which Amendment 7 rules out, and needs its own decision.
New:
> Blocking an account on a class would key financial control on message text, which Amendment 7 as amended by Amendment 9 still rules out, and needs its own decision.
Reason: Retains the class-based blocking prohibition while acknowledging the separate diagnostic exception.

#### R16.4 (lines 1095–1096)
Old:
> 3. **Nothing else changes.** The two fields, the 64 KiB bound, the drop rule, the ledger columns and the set validation are as Amendment 7 states them. No provider text is stored.
New:
> 3. **Preserved boundaries.** The diagnostic vocabulary is Amendment 7’s set extended by Amendment 8’s `credit_balance_low` and `spend_limit_reached`; the 64 KiB bound and set validation remain. Amendment 9 revises settlement, evidence persistence and the explicit opt-in diagnostic-file exception to the drop rule. No provider text enters the ledger, public errors or ordinary logs.
Reason: Replaces “The two fields” blanket preservation with the extended Amendment 7/8 vocabulary, bounded parsing and validation; settlement and columns follow Amendment 9. Replaces “No provider text is stored” with exclusion from ledger, errors and logs plus only the opt-in diagnostic-file exception.

### R17 — Amendment 5 Decision 4 clearing list

#### R17.1 (lines 354–359)
Old:
> 4. **Clearing.** In the transaction that records the outcome, the ledger deletes every `warm_prefix` row for the provider and account when it records:
> - an `UnknownOutcome` on an open attempt with any reason but `cancelled` and `stream_closed` (the still-open branch, `_ledger.py:910-921`), whether or not an earlier reason already stands: `auth_required`, `rate_limited`, `provider_error`, `request_rejected`, `transport_error`, `usage_incomplete`, `stream_error`, `stream_truncated`;
New:
> 4. **Clearing.** In the transaction that records the outcome or an independent block, the ledger deletes every `warm_prefix` row for the provider and account when it records:
>    - an `UnknownOutcome` on an open attempt with any reason but `cancelled` and `stream_closed` (the still-open branch, `lionagi/service/accounts/_ledger.py:910-921`), whether or not an earlier reason already stands: `auth_required`, `rate_limited`, `provider_error`, `request_rejected`, `transport_error`, `usage_incomplete`, `stream_error`, `stream_truncated`; an automatic Amendment 9 rejection-zero settlement for 400 (`request_rejected`) or 529 (`provider_error`), or stream estimate whose original ending reason is `transport_error`, `stream_error` or `stream_truncated`. Neither new basis creates or refreshes warmth;
Reason: Adds automatic zero and stream-estimate clearing without creating warmth; retains the existing unknown branch and its citation antecedent.

### R18 — Amendment 5 Decision 4 block precedence

#### R18.1 (lines 375–381)
Old:
> It does not clear on a complete settlement. It does not clear on `cancelled`, `stream_closed` or a caller-cancelled `not_sent`, which say the caller ended the request and nothing about the account. A consumer that closes the stream after the provider sent an error event records `stream_error`, not `stream_closed` (item 8), so that failure clears. It does not clear on a reconciliation (`_reconcile_sync`, `_ledger.py:1074`), which prices an old attempt after the fact, or on a settlement of an attempt already settled, which returns before any write (`:908-909` for `UnknownOutcome`, `:929-930` for an identical repeat).
New:
> Complete settlement preserves warmth unless it records a block. Cancellation, consumer close and caller-cancelled not-sent (`ProvenNotSent(caller_cancelled=True)`) say the caller ended the request and nothing about the account; they preserve warmth only in the absence of a block; E > C therefore clears even on a cancelled or closed stream. A consumer closing after an error event has original reason `stream_error`, not `stream_closed` (item 8), in its open outcome or estimate evidence, and clears. Reconciliation (`_reconcile_sync`, `lionagi/service/accounts/_ledger.py:1143-1162`) does not clear merely for pricing an old attempt, but an Amendment 9 reconciliation block clears. An identical repeat or reason-only write on a settled row returns before any write (`lionagi/service/accounts/_ledger.py:929-930` and `lionagi/service/accounts/_ledger.py:908-909`, respectively) and clears nothing; runtime evidence on a reconciled row is also ignored without blocking. Other conflicting final evidence follows Amendment 9's conflict rules.
Reason: Makes block clearing take precedence over cancellation and reconciliation preservation, while keeping the caller-cancellation rationale and no-write repeat behavior. Re-cites `_ledger.py:1074` at the reconciliation write and adjustment insertion.

### R19 — Amendment 5 Decision 4 candidate ordering

#### R19.1 (lines 385–386)
Old:
> Only a 401 or 403 blocks an account (`messages.py:630`, `:1118`).
New:
> HTTP 401/403 still blocks with `auth_required` (`lionhub/src/lionhub/providers/anthropic/messages.py:630`, `lionhub/src/lionhub/providers/anthropic/messages.py:1118`); accounting blocks `ceiling_exceeded` and `settlement_conflict`, and explicit operator blocks, also block an account and clear warmth.
Reason: Replaces authentication-only blocking with authentication, accounting and explicit operator blocks, retaining warmth clearing. Dropped or re-cited attribution: `messages.py:630`, `messages.py:1118`.

#### R19.2 (lines 389–391)
Old:
> A `ProvenNotSent` settles at cost zero (`_ledger.py:866-868`) and an unknown outcome keeps only its attempt's quote open, so a failure need not move the account down that order.
New:
> ProvenNotSent and qualifying typed rejections settle zero; eligible early-ended streams settle E; unresolved ambiguous attempts retain their ceilings. Thus a failure need not move an account down that order, including repeated zero rejections.
Reason: Adds zero and estimate closure to the ranking explanation while retaining the general limitation that a failure need not move an account down the order. Dropped or re-cited attribution: `_ledger.py:866-868`.

### R20 — Amendment 5 clearing verification

#### R20.1 (lines 721–722)
Old:
> A consumer that closes the stream after an error event records `stream_error` and clears; one that closes it without an error event records `stream_closed` and does not.
New:
> A consumer that closes the stream after an error event retains original reason `stream_error` in the open outcome or estimate evidence and clears; one that closes it without an error event retains original reason `stream_closed` and preserves warmth unless blocked.
Reason: Requires close-after-error to retain stream_error in open or estimate evidence; ordinary consumer close preserves warmth only without a block.

#### R20.2 (lines 723–733)
Old:
> With an AnyIO scope cancellation (`fail_after`) pending when the adapter records a 429, the attempt keeps `rate_limited`, the rows are cleared and the caller receives `TimeoutError`; pending at a 401, the account is blocked, the attempt keeps `auth_required`, the rows are cleared and the caller receives `TimeoutError`; pending at a 5xx, the attempt keeps `provider_error`, the rows are cleared and the caller receives `TimeoutError`; pending at a 400, the attempt keeps `request_rejected`, the rows are cleared and the caller receives `TimeoutError`; pending at a transport failure after the request entered, the attempt keeps `transport_error`, the rows are cleared and the caller receives `TimeoutError`; pending at a 200 whose usage is incomplete, the attempt keeps `usage_incomplete` and the rows are cleared; pending at a 200 that prices over the ceiling, the account is blocked with `ceiling_exceeded` and the rows are cleared.
New:
> With an AnyIO scope cancellation (`fail_after`) pending when the adapter records a 429, the attempt keeps `rate_limited`, the rows are cleared and the caller receives `TimeoutError`; pending at a 401, the account is blocked, the attempt keeps `auth_required`, the rows are cleared and the caller receives `TimeoutError`; pending at a qualifying 529, the attempt settles `rejection_zero` with `provider_error` in structured evidence, otherwise a 5xx keeps `provider_error`; pending at a qualifying 400, the attempt settles `rejection_zero` with `request_rejected` in structured evidence and its two rejection columns, otherwise keeps open `request_rejected`; in each case warmth is cleared and the caller receives `TimeoutError`; pending at a transport failure after the request entered, the attempt keeps `transport_error` in the open outcome or eligible stream-estimate evidence, the rows are cleared and the caller receives `TimeoutError`; pending at a non-streamed 200 whose usage is incomplete, the attempt keeps `usage_incomplete` and the rows are cleared; pending at a 200 that prices over the ceiling, the account is blocked with `ceiling_exceeded` and the rows are cleared.
Reason: Updates pending-cancellation verification for qualified 400/529 zero and transport estimates; narrows the incomplete-200 case to the non-streamed path.

#### R20.3 (lines 736–740)
Old:
> `cancelled`, `stream_closed`, a caller-cancelled `not_sent` from either adapter cancellation handler or from `begin` (cancellation class and `GeneratorExit`), a reconciliation, an `UnknownOutcome` on a settled attempt and an identical repeat delete nothing. `ProvenNotSent(caller_cancelled=True)` followed by `ProvenNotSent(caller_cancelled=False)` on one attempt returns `already_settled` and clears nothing.
New:
> `cancelled`, `stream_closed`, a caller-cancelled `not_sent` from either adapter cancellation handler or from `begin` (cancellation class and `GeneratorExit`), and an Amendment 9 reconciliation preserve warmth unless that operation records a block; block precedence is mandatory. A reconciliation without a block, an `UnknownOutcome` on a settled attempt and an identical repeat delete nothing. `ProvenNotSent(caller_cancelled=True)` followed by `ProvenNotSent(caller_cancelled=False)` on one attempt returns `already_settled` and clears nothing. For an Amendment 9 estimate E > C, including cancellation and consumer close, verify the block clears warmth; 529 zero also clears. Neither new settlement basis creates or refreshes a prefix. Typed reconciliation preserves ordinary no-clear behavior unless it records a block. For transport-first cancellation, assert the latched transport reason and shielded write before outward TimeoutError; for cancellation-first, assert cancelled and block precedence, as Decision 2 specifies.
Reason: Adds block precedence and typed reconciliation to clearing verification while retaining no-write repeats and transport-first versus cancellation-first checks.

### R21 — D4 quote and evidence interface

#### R21.1 (lines 1429–1430)
Old:
> Quote = (requested_model: str, ceiling_nusd: int, price_revision: str, request_digest: str)
New:
> Quote = (requested_model: str, ceiling_nusd: int, price_revision: str, request_digest: str, pricing_inputs: PricingInputs | None = None)
Reason: Adds optional immutable pricing_inputs to Quote with a null default for legacy construction.

#### R21.2 (lines 1433–1434)
Old:
> Evidence = complete_usage(served_model, numeric_usage, request_id) | proven_not_sent | unknown(reason_code)
New:
> Evidence = complete_usage(served_model, numeric_usage, request_id) | proven_not_sent | unknown(reason_code) | RejectionZero | StreamUsageEstimate
Reason: Adds the two final evidence variants without changing the existing three.

#### R21.3 (lines 1437–1437)
Old:
> reconcile(adjustment_id, attempt_id, cost_nusd, source_ref)
New:
> reconcile(adjustment_id, attempt_id, cost_nusd, source_ref, *, evidence: RejectionZero | StreamUsageEstimate | None = None)
Reason: Adds optional typed evidence to reconciliation while preserving the existing untyped call form.

### R22 — D4 closure rule

#### R22.1 (lines 1442–1443)
Old:
> Only complete usage or transport-owned proof of no dispatch closes a row; `unknown` records a reason without changing its reservation.
New:
> Runtime settlement closes a row only with complete usage, transport-owned proof of no dispatch, or Amendment 9 typed rejection-zero or stream-estimate evidence. `unknown` records a reason without changing its reservation. Explicit sourced reconciliation closes an open row through its separate operation; Amendment 9 operator closure supplies typed evidence and obeys its current-month eligibility fence.
Reason: Adds typed zero and stream-estimate runtime closure and distinguishes current-month typed operator reconciliation from an unchanged unknown hold.

#### R22.2 (lines 1445–1445)
Old:
> A quote's validity is checked again inside admission.
New:
> A quote's validity is checked again inside admission. Normal runtime settlement after rollover still charges that immutable original month.
Reason: Retains admission quote validation and makes runtime settlement’s original-month attribution explicit after rollover.

### R23 — D4 attempt shape

#### R23.1 (lines 1387–1390)
Old:
> attempt(id, scope_kind, scope_id, provider, account, month_utc, ceiling_nusd, price_revision, requested_model, request_digest, state: open|settled, cost_nusd?, usage_json?, request_id?, outcome?, created_at, settled_at?) PK(id)
New:
> attempt(id, scope_kind, scope_id, provider, account, month_utc, ceiling_nusd, price_revision, requested_model, request_digest, state: open|settled, cost_nusd?, usage_json?, request_id?, outcome?, created_at, settled_at?, provider_error_type?, rejection_class?, settlement_evidence_json?) PK(id)
Reason: Adds structured billing evidence to the private attempt shape without changing lifecycle states or other tables.

### R24 — D5 heading

#### R24.1 (lines 1460–1460)
Old:
> ### D5 — Price a ceiling before every attempt, settle from complete usage
New:
> ### D5 — Price a ceiling before every attempt, settle from complete usage or Amendment 9 evidence
Reason: Extends the heading to include the two new settlement bases.

### R25 — Context admission summary

#### R25.1 (lines 1202–1202)
Old:
> | Admission and cost | D5: conservative priced ceilings; complete provider usage settles once. |
New:
> | Admission and cost | D5: conservative priced ceilings; complete provider usage or Amendment 9 evidence settles once, with sourced reconciliation for open rows. |
Reason: Extends only the admission-and-cost summary to evidenced closure and sourced reconciliation.

### R26 — Amendment 7 class-control boundary

#### R26.1 (lines 878–879)
Old:
> This amendment uses message text only to name a class for diagnosis, and no code path may branch on that class.
New:
> This amendment uses message text for diagnosis only; Amendment 9 permits selecting class `other` for diagnostic capture, but no settlement, routing, blocking, retry or warmth control may branch on the class.
Reason: Allows diagnostic capture to select other while retaining the ban on message-class financial and request control.

### R27 — Amendment 7 class guarantee

#### R27.1 (lines 986–987)
Old:
> It is never assigned a wrong class that a caller acts on, because nothing acts on the class (Decision 6).
New:
> It is never assigned a wrong class that a caller acts on for accounting or request control, because those decisions do not act on the class, so a diagnostic misclassification cannot change those decisions; only the opt-in diagnostic capture may select class `other` (Amendment 7 Decision 6 as amended by Amendment 9).
Reason: Retains no-wrong-class financial/request action; narrows the former nothing-acts-on-class rationale solely for diagnostic capture.

### R28 — Amendment 5 ranking after evidenced closure

#### R28.1 (lines 427–428)
Old:
> Unknown attempts remain open for ranking and admission.
New:
> Unknown attempts remain open for ranking and admission only until authorized Amendment 9 evidence or sourced reconciliation closes them; recorded settled cost then replaces the ceiling in utilization.
Reason: Clarifies that evidenced closure removes the open ceiling from ranking without changing the ranking formula.

### R29 — Amendment 5 schema completeness

#### R29.1 (lines 430–437)
Old:
>
> - The unlocked fast path in `_ensure_schema` (`_ledger.py:423-429`) returns only when the version is 1, `_TABLES | {"warm_prefix"}` is present and both warm-prefix indexes exist.
> - The locked version-0 branch, which a fresh file takes, creates the table and its indexes beside the base tables and before `PRAGMA user_version = 1` (`:569`), in the same transaction.
> - The locked version-1 branch requires `_TABLES` as today and then runs the `CREATE TABLE IF NOT EXISTS` statement and the two `CREATE INDEX IF NOT EXISTS` statements before it returns (`:458-465`), so a file whose table predates the indexes gains them. A version-1 file missing a base table still refuses with `LEDGER_UNAVAILABLE`.
New:
> - The unlocked `_ensure_schema` fast path requires version 1, `_TABLES | {"warm_prefix"}`, both warm-prefix indexes (`warm_prefix_cert` and `warm_prefix_expiry`), both rejection columns and `settlement_evidence_json`.
>    - The locked version-0 branch, used by a fresh file, creates the extension table and indexes beside the base tables, and all required columns, before `PRAGMA user_version = 1` in the same transaction.
>    - The locked version-1 branch re-reads version, tables, indexes and columns, requires `_TABLES`, then runs `CREATE TABLE IF NOT EXISTS`, both `CREATE INDEX IF NOT EXISTS` statements and adds missing extension columns before returning. A table predating the indexes therefore gains them. A version-1 file missing a base table still refuses with `LEDGER_UNAVAILABLE`. These extend the existing checks (`lionagi/service/accounts/_ledger.py:423-429`, `lionagi/service/accounts/_ledger.py:569`, `lionagi/service/accounts/_ledger.py:458-465`); the table remains created in the same transaction as the five D4 tables.
Reason: Extends completeness and locked creation to evidence columns; preserves both index names, the five-table transaction and missing-base refusal.

### R30 — Amendment 5 stream ending reasons

#### R30.1 (lines 506–509)
Old:
>    - The streamed adapter's `GeneratorExit` handler (`messages.py:1229-1234`) records `stream_error`
>      when the stream carried an error event (`saw_error_event`, `:1064`, `:1084-1087`) and
>      `stream_closed` otherwise. Its end-of-stream branch already decides the error-event case the
>      same way and records `stream_truncated` otherwise (`:1213-1215`).
New:
> - The streamed adapter’s `GeneratorExit` handler (`lionhub/src/lionhub/providers/anthropic/messages.py:1229-1234`) selects original reason `stream_error` after an observed error event (`saw_error_event`, `lionhub/src/lionhub/providers/anthropic/messages.py:1064`, `lionhub/src/lionhub/providers/anthropic/messages.py:1084-1087`) and `stream_closed` otherwise. Its end-of-stream branch selects `stream_error` after an error event and `stream_truncated` otherwise (`lionhub/src/lionhub/providers/anthropic/messages.py:1213-1217`). Under Amendment 9 Decision 2, an eligible estimate stores that reason in structured evidence; otherwise `UnknownOutcome` records it on the open row.
Reason: Preserves error-event detection and close/post-loop reasons while storing an eligible reason in estimate evidence rather than an open outcome. Re-cites `:1213-1215` through the recording call at `lionhub/src/lionhub/providers/anthropic/messages.py:1213-1217`.

### R31 — Amendment 5 repeated settlement and failed writes

#### R31.1 (lines 515–518)
Old:
> After `CompleteUsage` that is `already_settled`; on an attempt still open (`usage_incomplete`, a status outcome) a second clearing reason would clear the account's warm-prefix rows again, a row another request wrote in between included. A settlement whose ledger write raises leaves the attempt open for reconciliation, as any unsettled attempt is.
New:
> After complete usage, an Amendment 9 final settlement, or reconciliation, a later reason-only write returns `already_settled` and changes nothing. On an attempt still open (`usage_incomplete` or a nonqualifying status outcome), another clearing reason can clear warmth again, including a row another request wrote between calls. Amendment 9's final-basis consistency and reconciled-row precedence govern final evidence, not reason-only writes. A settlement whose ledger write raises leaves the attempt open for reconciliation, as any unsettled attempt is. Ordinary sourced reconciliation remains available for any open month; only Amendment 9 typed closure has the current-month eligibility fence.
Reason: Extends reason-only no-op protection to new settled bases; distinguishes the current-month typed fence from ordinary reconciliation.

### R32 — Amendment 5 buffered error limitation

#### R32.1 (lines 535–541)
Old:
> An error event the redactors still hold when the consumer closes, or a cancellation that replaces `GeneratorExit` while the stream unwinds, records `stream_closed` or `cancelled` and clears nothing. A frame flagged `call_failed` before `message_stop` makes a close before `message_stop` record `stream_error`, which clears the rows of an account that served the request. Both cost steering only: the next failure there clears a missed row, and the next one-hour write restores a cleared one.
New:
> An error event the redactors still hold when the consumer closes, or a cancellation that replaces `GeneratorExit` while the stream unwinds, retains original reason `stream_closed` or `cancelled` in an open outcome or Amendment 9 estimate and preserves warmth unless a block is recorded. A frame flagged `call_failed` before `message_stop` makes a close before `message_stop` retain original reason `stream_error` in an open outcome or eligible estimate evidence, which clears the rows of an account that served the request even though the final outcome may be `stream_usage_estimate`. Apart from Amendment 9 estimate/block effects, these reason differences affect steering only: the next failure there clears a missed row, and the next one-hour write restores a cleared one.
Reason: Retains the buffered-error detection limitation and served-account clearing scope; permits estimates and mandatory block clearing rather than unconditional preservation.

### R33 — Amendment 5 timeout guarantee

#### R33.1 (lines 628–632)
Old:
>
> - An outer timeout that cancels the task (`asyncio.timeout`, `anyio.fail_after`) reaches the adapter as the cancellation class: before the request is sent it records a caller-cancelled `not_sent`, after it `cancelled`, and neither clears. A transport timeout inside the adapter on an open attempt records `transport_error`, which clears; one after the attempt settled clears nothing.
New:
> - An outer timeout that cancels the task (`asyncio.timeout`, `anyio.fail_after`) reaches the adapter as cancellation: before send it records caller-cancelled `not_sent`; after send the original reason is `cancelled` unless a shielded status/response settlement has already selected its evidence or transport failure was latched first. Those selected bases keep their precedence under Amendment 9 Decision 2 and the shielded-status verification. An eligible stream settles Amendment 9's estimate, otherwise the reason leaves its ceiling held. Neither clears warmth unless a block is recorded; E > C blocks and clears. A transport timeout inside the adapter on an unsettled attempt uses `transport_error` in unknown or estimate evidence and clears. A late reason-only timeout write after final settlement changes nothing. If transport failure was latched first, Decision 2 records transport_error before delivering the pending cancellation; it does not replace that reason with cancelled.
Reason: Distinguishes cancellation-first from transport-first and already-selected shielded status/response evidence; blocks still clear.

### R34 — Amendment 5 clearing alternative

#### R34.1 (lines 668–670)
Old:
>
> - **Clear only on 429 and 5xx.** Rejected: an unresolved key, a 400 for exhausted credit, a transport timeout and a failed stream each left the row, which kept the failing account first for every new scope with that prefix even when another account ranked ahead under D3's order.
New:
> - **Clear only on 429 and 5xx.** Rejected: an unresolved key, a 400 for exhausted credit, a transport timeout or failed stream can leave a warm preference on a failing account. Clearing removes that preference but does not remove the account from D3. Amendment 9 zero settlements may keep it first by utilization until an operator blocks it.
Reason: Retains broad failure clearing and the exhausted-credit example; distinguishes preference removal from removing candidate eligibility.

### R35 — Amendment 5 unwinding verification

#### R35.1 (lines 744–751)
Old:
>
> - Usage shapes: a request with a server tool stores NULL on a write and refreshes nothing on a matching read; a one-hour mark inside a `document` block's `source.content` after the key block sets `hour_after_key`; a top-level one-hour `cache_control` with no markable block after the key block leaves it false; a caller's one-hour mark on a blank system text block is not the key; a non-text `expires_at` counts as absent; an attempt with an unparsable `created_at` writes nothing; a `GeneratorExit` or an exception while unwinding a stream that already settled (a status outcome or `CompleteUsage`) settles nothing more; a repeated `clear_block()` with a recorded adjustment deletes nothing.
New:
> - Usage shapes: a request with a server tool stores NULL on a write and refreshes nothing on a matching read; a one-hour mark inside a `document` block's `source.content` after the key block sets `hour_after_key`; a top-level one-hour `cache_control` with no markable block after the key block leaves it false; a caller's one-hour mark on a blank system text block is not the key; a non-text `expires_at` counts as absent; an attempt with an unparsable `created_at` writes nothing; a `GeneratorExit` or an exception while unwinding a stream that already settled (a status outcome, `CompleteUsage`, Amendment 9 final evidence or reconciliation) settles nothing more; a repeated `clear_block()` with a recorded adjustment deletes nothing. Reason-only ledger writes on settled rows return `already_settled`; adapter settlement-attempt flags also protect still-open status outcomes from a second write.
Reason: Adds new final evidence and reconciliation to unwinding verification, retaining status outcomes and distinguishing reason-only no-ops from the adapter flag’s protection of still-open attempts.

### R36 — Amendment 7 context baseline

#### R36.1 (lines 865–872)
Old:
> The routed Anthropic paths settle such a response `request_rejected` and raise before reading its body, in both the non-streamed and the streamed attempt (`lionhub/src/lionhub/providers/anthropic/messages.py`, the `status != 200` branches of `_request_attempt` and `_routed_stream_attempt`). The settled outcome stores only that reason code, and `AccountRoutingError` carries only the code, the validated ids and the HTTP status. So when accounts are configured, nothing records whether a 400 was a prompt over the context window, a tool result without its call, a thinking setting the model refuses, or any other cause.
New:
> Before Amendment 7, the `status != 200` branches of `_request_attempt` and `_routed_stream_attempt` in `lionhub/src/lionhub/providers/anthropic/messages.py` recorded only `request_rejected` and raised before reading its body. The outcome held only that reason, and `AccountRoutingError` carried only the code, the validated ids and the HTTP status. Thus, when accounts were configured, the old record could not distinguish a prompt over the context window, an unmatched tool result, a refused thinking setting or any other cause. Amendment 7 adds typed/class diagnosis; Amendment 9 additionally permits typed zero for qualifying 400 and 529, with separate outward-field rules.
Reason: Dates the historical body-reading limitation, retaining configured-account scope and every possible unknown cause; identifies the later evidence extension.

#### R36.2 (lines 873–874)
Old:
> Amendment 6, Decision 5 added no database column because a schema change needs its own decision; Decision 5 below is that decision for the two columns it adds.
New:
> Amendment 6, Decision 5 added no database column because a schema change needs its own decision; Decision 5 below is that decision for the two columns it adds. Amendment 9 owns its additional structured-evidence column.
Reason: Attributes the additional structured-evidence column to Amendment 9 without changing the earlier two-column decision.

#### R36.3 (lines 872–873)
Old:
> The unrouted path keeps the whole body in its error, and this amendment leaves it as it is.
New:
> The unrouted path keeps the whole body in its error, and Amendment 7 leaves it as it is.
Reason: Names Amendment 7 explicitly so the historical unchanged-path clause has an unambiguous antecedent.

### R37 — Amendment 7 classifier vocabulary reach

#### R37.1 (lines 958–959)
Old:
> On the rejection branch only the 4xx types other than 401, 403 and 429 can occur. The full documented list is kept so that a type arriving with an unexpected status is still named.
New:
> On the request-rejected branch, only the 4xx types other than 401, 403 and 429 are expected under their documented statuses; Amendment 9 additionally reads a possible 529 with the bounded typed parser before the generic 5xx branch. The full documented type set remains available on the request-rejected branch even with an unexpected status. HTTP 529 is an explicit exception: an unrecognized pair retains the ceiling with null rejection columns/outward fields, and only qualifying overloaded_error enters structured zero evidence. No other pair qualifies for zero.
Reason: Adds bounded 529 parsing without broadening the closed zero table or outward diagnostic fields.

### R38 — Amendment 7 diagnostic field guarantee

#### R38.1 (lines 984–985)
Old:
>
> - A routed rejection's error, its settled outcome and its ledger row name a documented error type or `other`, and a class or `other`. None of them holds provider text.
New:
> - A request rejection's error, evidence variant and ledger row retain a validated documented error type or `other`, and a class or `other`: qualifying HTTP 400 uses RejectionZero and outcome `rejection_zero`, otherwise UnknownOutcome leaves an open `request_rejected` reason. HTTP 529 instead has null rejection columns and outward fields, with its type only in qualifying structured zero evidence. Reconciliation preserves prior columns. None of them holds provider text. The separate enabled diagnostic file is governed by Amendment 9, not by this ledger/error-field guarantee.
Reason: Replaces settled-outcome terminology with exact open/400/529 evidence locations, preserving documented-type/class validation and provider-text exclusion.

#### R38.2 (lines 1114–1115)
Old:
> A routed rejection whose message contains one of the three wordings records its class on the error, the settled outcome and the ledger row, on the non-streamed and the streamed attempt.
New:
> On both HTTP paths, a routed request rejection whose message contains one of the three wordings records the matching class: a qualifying HTTP 400 records its class on the error, the rejection columns and `RejectionZero` evidence, with outcome `rejection_zero`; a nonqualifying request rejection records the class on the error, in `UnknownOutcome` and on the open row. HTTP 529 keeps rejection columns and outward fields null, with `overloaded_error` only in qualifying structured zero evidence.
Reason: Replaces the erroneous settled-outcome field guarantee with separate held/400/529 persistence rules.

#### R38.3 (lines 1116–1117)
Old:
> An account out of credit is still reserved and still rejects each request routed to it, until it is blocked (Decision 2).
New:
> An account out of credit remains eligible for reservation and rejects requests routed to it until blocked (Decision 2); a qualifying HTTP 400 releases each reserved ceiling at zero instead of retaining that hold.
Reason: Distinguishes continued candidate eligibility from retention of a rejected attempt’s ceiling.

### R39 — Amendment 7 capture acceptance

#### R39.1 (lines 1000–1002)
Old:
> Its acceptance is that the next such rejection records a documented type, and a class other than `other` or, failing that, a reproduction on the unrouted path whose captured message extends the table.
New:
> Its acceptance is that the next such rejection records a documented type, and a class other than `other` or, failing that, an enabled Amendment 9 diagnostic capture or a reproduction on the unrouted path supplying sanitized wording for a later class-table amendment. Neither reproduction nor captured wording alone proves the original attempt’s billing. Diagnostic acceptance is independent of accounting: HTTP 400 plus invalid_request_error settles zero regardless of whether its class is named or capture is enabled.
Reason: Adds optional bounded capture to diagnostic acceptance without asserting that it proves the original bill.

### R40 — Amendment 7 rejected text-in-ledger alternative

#### R40.1 (lines 1008–1009)
Old:
> It puts provider text, which can quote request content, in the durable store, and needs its own reader policy.
New:
> Treat provider text as potentially containing request content. Amendment 9's sole durable-text exception is a separate enabled private diagnostic file with its own reader policy; provider prose remains forbidden in the ledger.
Reason: Keeps provider text out of the ledger and supplies only the separate private-file exception.

### R41 — Amendment 7 rejection reason alternative

#### R41.1 (lines 1012–1015)
Old:
> The warm-prefix clearing rule enumerates reason codes (it keeps the warm prefix only for `cancelled` and `stream_closed`), so every new class would be a new reason code that each such rule has to be checked against.
New:
> Warm-prefix clearing uses original reasons and block precedence, including Amendment 9's separate outcomes; it never uses message class. Every new reason requires auditing each clearing rule. Among the original failure reasons, only `cancelled` and `stream_closed` preserve warmth, and only when no block is recorded, under Amendment 9 Decision 3.
Reason: Uses original reason plus independent block precedence for clearing; message class remains excluded.

### R42 — Amendment 8 verification cross-reference

#### R42.1 (lines 1133–1133)
Old:
>
> - Amendment 7's sentinel, cancellation and bound tests run unchanged and pass.
New:
> - Run Amendment 7's sentinel, cancellation and body-bound tests with Amendment 9's explicit changes: capture off and on, qualifying zero versus held rejection, and no text outside the designated diagnostic file. Require all those tests to pass. The body bound itself remains unchanged.
Reason: Updates the sentinel, cancellation and bound verification for the new evidence and capture rules and explicitly requires all tests to pass.

### R43 — D3 unknown-attempt ranking

#### R43.1 (lines 1363–1363)
Old:
> Unknown attempts remain open for both ranking and admission.
New:
> Unknown attempts remain open for ranking and admission except when Amendment 9 evidence or sourced reconciliation closes them; use their recorded costs thereafter.
Reason: Distinguishes genuinely unknown open attempts from evidenced closures for ranking and admission.

### R44 — D4 permitted structured storage

#### R44.1 (lines 1396–1397)
Old:
> Only sanitized numeric usage and a validated bounded provider request id are stored, never prompts, request bodies or headers.
New:
> Only sanitized numeric usage, validated bounded provider request ids, closed diagnostic fields and Amendment 9's validated quote/evidence envelope are stored as response data. Never store prompts, request bodies, headers or provider prose in the ledger; the optional diagnostic file is a separate, explicitly bounded exception to dropping message text.
Reason: Adds validated structured quote/evidence storage; keeps request bodies, headers and provider prose out of the ledger.

### R45 — D4 crash retention

#### R45.1 (lines 1418–1419)
Old:
> Even a crash just before dispatch keeps its ceiling until positive evidence resolves it.
New:
> A crash just before dispatch retains its ceiling until transport-owned proof of no dispatch or explicit sourced reconciliation resolves it. A response from another physical attempt cannot release this hold; neither complete usage nor an HTTP rejection can belong to an attempt that never dispatched. Age or crash alone authorizes no release.
Reason: Clarifies physical-attempt attribution after a pre-dispatch crash; age or another attempt never releases the hold.

### R46 — D4 final-evidence precedence

#### R46.1 (lines 1421–1422)
Old:
> Matching repeated settlement is a no-op; differing final evidence raises `settlement_conflict`, blocks that account, and retains the original row.
New:
> Reason-only writes on settled rows and runtime settlement after reconciliation return `already_settled` without blocking. Otherwise identical repeated final runtime evidence is a no-op and conflicting final evidence commits a `settlement_conflict` block, retains the attempt row and raises `settlement_conflict` after the transaction. Reconcile on a non-open row refuses with `settlement_conflict` and no block write, except identical adjustment replay.
Reason: Adds runtime-after-reconciliation precedence and reason-only no-ops; retains other final-evidence conflict blocks with the post-transaction raise and original row.

### R47 — D4 typed reconciliation

#### R47.1 (lines 1447–1449)
Old:
> `reconcile` atomically closes an open attempt at the supplied nonnegative cost and inserts a zero-delta reconciliation row: the cost enters S **once** through the attempt.
New:
> `reconcile` atomically closes an open attempt at a nonnegative cost and inserts one zero-delta reconciliation row, so cost enters S **once** through the attempt. With Amendment 9 typed evidence it checks current-month eligibility and consistency, recomputes zero or E, and rejects amount mismatch before writing. Untyped sourced reconciliation retains its existing scope. Identical adjustment replay precedes open-state/month checks; a different call on a non-open row refuses without blocking.
Reason: Adds current-month typed reconciliation validation, amount recomputation and replay-before-state precedence without changing untyped scope or the single cost entry.

#### R47.2 (lines 1450–1451)
Old:
> An adjustment/settlement taking S + R above B records the truth and blocks further admission.
New:
> Amendment 9 estimates record E, an accounting policy value, not a verified provider bill. An adjustment or settlement taking recorded S + R above B records the truth of the accounting operation and blocks further admission; E > C also records ceiling_exceeded even when the true bill fits.
Reason: Retains truthful recording of the accounting operation and above-budget blocking; identifies E as a policy estimate and adds the independent E > C block.

#### R47.3 (lines 1452–1454)
Old:
> Duplicate adjustment ids are no-ops only for identical inputs. Later conflicting evidence needs a new sourced correction; neither it nor reconciliation silently replaces a settled cost.
New:
> Duplicate adjustment ids are no-ops only for identical inputs. For typed reconciliation, those inputs include canonical typed evidence and source reference. Later conflicting evidence needs a new sourced correction; neither it nor reconciliation silently replaces a settled cost. Runtime evidence arriving after reconciliation returns `already_settled` without comparison, mutation or blocking; other final-evidence conflicts follow Amendment 9 Decision 6.
Reason: Retains the preceding identical-input sentence and sourced-correction bar; extends typed replay identity and adds the runtime-after-reconciliation no-op while preserving other final-evidence conflicts.

### R48 — D5 incomplete usage exception

#### R48.1 (lines 1508–1510)
Old:
> Otherwise missing/inconsistent usage, an unknown served model or a new billing dimension keeps the ceiling and cost unknown.
New:
> Otherwise missing/inconsistent pricing dimensions, an unknown served model or a new billing dimension keeps the ceiling and cost unknown. Amendment 9 alone permits missing terminal evidence for a priceable early-ended stream with an actually observed output count; it settles an estimate, not complete usage.
Reason: Permits missing terminal evidence only for priceable observed stream usage; unknown pricing dimensions still retain the ceiling.

### R49 — Amendment 5 scope of preserved rules

#### R49.1 (lines 420–421)
Old:
> 6. **Unchanged.** Everything else in D3 and D4 stands, except the three settlement shields, the cancellation delivery point and the stream-close reason in item 8.
New:
> 6. **Preserved rules.** Amendment 5 changes the settlement shields, cancellation delivery and stream-close reason described in item 8. Amendment 9 additionally changes evidence-based closure, diagnostic capture and the clauses explicitly replaced there; the binding and selection boundaries below continue to apply. All other D3/D4 clauses remain in force unless an explicit Amendment 9 replacement changes them: fixed bindings, per-conversation selection, no fallback on unresolved credentials, integer ranking, atomic reservation, immutable attempt identity and sourced corrections remain. No blanket preservation claim overrides the new quote, evidence, reconciliation or block rules.
Reason: Limits preservation language to clauses not explicitly amended; retains binding and selection boundaries.

### R50 — All accounting blocks clear warmth

#### R50.1 (lines 367–368)
Old:
>
> - a block: `settlement_conflict` (`_ledger.py:932`), `ceiling_exceeded` (`:968`) and every `block()` (`_block_sync`, `:1015`), `auth_required` included;
New:
> - a block: `settlement_conflict`, including contradictory final evidence on an open attempt that leaves its row unchanged; `ceiling_exceeded`, including E > C in runtime settlement or typed reconciliation; and every explicit `block()` (`lionagi/service/accounts/_ledger.py:1015-1025`), `auth_required` included;
Reason: Extends accounting block clearing to open-evidence conflicts and estimates; The clearing lead-in supplies the same-transaction rule. Re-cites `_ledger.py:932`, `:968` and `:1015` for the existing writes and clears at `lionagi/service/accounts/_ledger.py:931-936`, `lionagi/service/accounts/_ledger.py:966-972` and `lionagi/service/accounts/_ledger.py:1015-1025` (`_block_sync`); the new conflict and estimate cases extend those sites.

### R51 — Reservation quote extension

#### R51.1 (lines 447–451)
Old:
>
> - D4's `reserve(scope, attempt_id, quote, accounts)` becomes `reserve(scope, attempt_id, quote, accounts, *, cert=None)`; it reads the index when the reservation inserts the scope's binding (`_ledger.py:775-798`). `Reservation` is unchanged: no later step needs to know whether the scope was new, because the read arm applies to every settled attempt.
New:
> - D4's `reserve(scope, attempt_id, quote, accounts)` becomes `reserve(scope, attempt_id, quote, accounts, *, cert=None)`; certificate selection reads the index when inserting the scope binding (`lionagi/service/accounts/_ledger.py:777-799`). The public signature is at `lionagi/service/accounts/_ledger.py:832-840`. Reservation's outer fields and positional construction stay unchanged, but its nested quote gains `pricing_inputs`, persisted atomically before dispatch. No new-scope flag is added; the read arm remains scoped to CompleteUsage for both new and bound scopes.
Reason: Adds persisted nested quote pricing inputs without changing Reservation’s outer fields, positional construction or complete-usage read-arm scope. Dropped or re-cited attribution: `_ledger.py:775-798`.

#### R51.2 (lines 461–463)
Old:
> Only the two settlements that can carry `CompleteUsage` pass them on to `ledger.settle`: `settle_response` (`:183-201`) and `settle_stream_end` (`:240-257`).
New:
> Only the two settlements that can carry `CompleteUsage` pass them on to `ledger.settle`: `settle_response` (`lionagi/service/accounts/_router.py:183-201`) and `settle_stream_end` (`lionagi/service/accounts/_router.py:240-257`). The new `settle_rejection` forwards none of `cert`, `hour_after_key` or `server_tools`: rejection zeros never write warmth.
Reason: Keeps complete-usage forwarding exclusive to those two methods and specifies that rejection settlement forwards no certificate fields.

### R52 — Shielded accounting paths

#### R52.1 (lines 473–480)
Old:
>
> - `AccountRouter.settle_response` (`_router.py:183-201`), `AccountRouter.settle_unknown` (`:203-219`) and `AccountRouter.block` (`:236-238`) run their ledger write under `CancelScope(shield=True)`, as `settle_reservation_not_sent` and `settle_stream_end` already do (`:228-234`, `:240-257`). `settle_response` records Decision 4's `usage_incomplete` and `ceiling_exceeded` on the non-streamed path (`messages.py:699`), and the complete settlements that D6 settles under cancellation (`ADR-0126.md:1550`). A worker write that waits for admission while an AnyIO scope cancellation is pending never starts (`lionagi/ln/concurrency/utils.py:64-65`).
New:
> - `AccountRouter.settle_response` (`lionagi/service/accounts/_router.py:183-201`), `AccountRouter.settle_unknown` (`lionagi/service/accounts/_router.py:203-219`) and `AccountRouter.block` (`lionagi/service/accounts/_router.py:236-238`) run their ledger write under `CancelScope(shield=True)`, as `settle_reservation_not_sent` and `settle_stream_end` already do (`lionagi/service/accounts/_router.py:228-234`, `lionagi/service/accounts/_router.py:240-257`). The response path records `usage_incomplete` (`lionhub/src/lionhub/providers/anthropic/messages.py:682`, `lionhub/src/lionhub/providers/anthropic/messages.py:692`) and keeps that row open (`lionagi/service/accounts/_ledger.py:907-921`); complete usage through `settle_response` (`lionhub/src/lionhub/providers/anthropic/messages.py:699`) can record `ceiling_exceeded` and clear warmth (`lionagi/service/accounts/_ledger.py:966-972`); it retains the complete settlements D6 settles under cancellation. Amendment 9 adds shielded `settle_rejection` and stream estimates through `settle_stream_end`; D6 cancellation can now record those estimates, with E > C blocking. A worker write is scheduled on a thread; the record’s guarantee remains that an unshielded write waiting for admission while AnyIO cancellation is pending never starts. These shields preserve the accounting attempt, subject to Amendment 6's separate bounded waits and ledger failure, rather than promising successful commit.
Reason: Adds rejection and estimate shields without weakening complete-settlement cancellation coverage or the admission-wait condition. Drops `lionagi/ln/concurrency/utils.py:64-65` as renewed implementation attribution; retains the admission-wait requirement. Dropped or re-cited attribution: Decision 4, `ADR-0126.md:1550`, `ADR-0126`, `Decision 4`.

### R54 — Cancellation and final-basis precedence

#### R54.1 (lines 488–498)
Old:
> The clauses that re-raise a routed failure (`messages.py:717-727`, `:1235-1240`) and the two transport-failure branches (`:743-749`, `:1250-1257`) call `await anyio.lowlevel.checkpoint()` before they raise. Without it, the transport branch raises the routed failure with no checkpoint after its shielded write, so a caller under `fail_after` receives the routing error instead of `TimeoutError`, and a status-code branch delivers the cancellation only if closing the HTTP session checkpoints. When the cancellation fires first, the cancellation handler's second settlement records nothing: the first reason on an open attempt stands, and a settled attempt is left as it is (`_ledger.py:908-918`). The comment at `messages.py:626-629` and `:1111-1116` is rewritten: both writes are shielded, and the block still goes first because a ledger error on the first write stops the second, and losing the attempt's reason only keeps its ceiling held.
New:
> The routed-failure handlers call `await anyio.lowlevel.checkpoint()` before re-raising (`lionhub/src/lionhub/providers/anthropic/messages.py:717-727`, `lionhub/src/lionhub/providers/anthropic/messages.py:1235-1240`), as do the non-streamed and streamed outer transport paths (`lionhub/src/lionhub/providers/anthropic/messages.py:743-749`, `lionhub/src/lionhub/providers/anthropic/messages.py:1250-1257`). Amendment 9 moves latched mid-stream transport accounting before output drain and context exit: `settled=True` precedes its shielded write, followed by a cancellation checkpoint. The outer guard then skips a second write and still checkpoints before raising if reached. Cancellation at the new checkpoint re-raises from the cancellation handler, so it need not reach the old outer checkpoint. Without the applicable post-write checkpoint, a pending `fail_after` can receive the routing error instead of `TimeoutError`, and a status-code branch delivers pending cancellation only if closing the HTTP session checkpoints. Reason-only writes retain the first open reason or ignore a settled row (`lionagi/service/accounts/_ledger.py:908-918`). Final 400/529 evidence after a committed cancelled reason instead conflicts, retains the row and blocks/clears. Runtime evidence after reconciliation is ignored. Authentication writes still block first (`lionhub/src/lionhub/providers/anthropic/messages.py:626-631`, `lionhub/src/lionhub/providers/anthropic/messages.py:1111-1119`); both calls are shielded, but a ledger error in the first stops the second and leaves the attempt ceiling held. Rewrite the comments at `lionhub/src/lionhub/providers/anthropic/messages.py:626-629` and `lionhub/src/lionhub/providers/anthropic/messages.py:1111-1116` to state this shielding and block-first order.
Reason: Adds pre-drain transport accounting and final-basis precedence; preserves routed/status checkpoint rationale and first-open-reason/no-op behavior. Dropped or re-cited attribution: `messages.py:626-629`, `:1111-1116`, `messages.py:1111-1116`.

### R55 — Settled state is not execution proof

#### R55.1 (lines 591–593)
Old:
> A failure after the attempt settled (a transport error after a stream's final usage) clears nothing: the attempt is already settled (`_ledger.py:907-909`), and the account served the request.
New:
> A later reason-only failure after settlement, such as a transport error after a stream’s final usage, clears nothing (`lionagi/service/accounts/_ledger.py:907-909`). A complete-usage settlement shows a response with priced usage, but settled state alone is not evidence that an account served the request: rejection zero and stream estimates are settled too.
Reason: Retains the post-final-usage transport example and no-clear guarantee; distinguishes settled state from proof of execution.

### R56 — Cross-version accounting blocks

#### R56.1 (lines 626–627)
Old:
> A re-admission after an accounting block that process set (`settlement_conflict`, `ceiling_exceeded`) deletes rows of an account that served its requests.
New:
> A re-admission after an accounting block that process set (`settlement_conflict`, `ceiling_exceeded`) deletes the account’s warmth regardless of whether it served a request. Separately, the writer at c6a3b4af already implements Amendment 5 and can clear warmth; it may conflict-block a replay against a new rejection-zero outcome. Decision 6 examines that writer only. It makes no compatibility promise for the pre-Amendment-5 process described above; an accounting block is not execution evidence.
Reason: Makes block clearing independent of execution proof and limits compatibility claims to the examined writer.

### R57 — Cancellation clearing alternative

#### R57.1 (lines 665–667)
Old:
>
> - **Clear on every outcome that is not complete, cancellation included.** Rejected: a caller's cancellation says nothing about the account, and a fanout that cancels its losers would keep wiping the rows its winners wrote.
New:
> - **Clear on every outcome that is not complete, cancellation included.** Clearing merely for caller cancellation remains rejected: cancellation alone says nothing about an account, and cancelling losing requests could repeatedly wipe warmth written by successful ones. Amendment 9 makes an independent block an explicit exception: E > C or contradictory final evidence must clear even during cancellation. That conservative deletion can remove useful warmth and is accepted for accounting safety, not as proof the account failed.
Reason: Retains cancellation-alone preservation but makes independently recorded accounting blocks clear warmth.

### R58 — Mutation tests under new evidence

#### R58.1 (lines 783–797)
Old:
>
> - Mutations: replacing the W preference with the plain order turns the first test red; dropping the length comparison from the read arm turns the one-token-longer and one-token-shorter cases red; dropping the block check turns the blocked-account no-write case red; dropping the server-tool check turns the server-tool arm red; reading `Point.nested` for the flag turns the `source.content` arm red; setting `settled` after a status-branch settlement instead of before turns the settled-stream arm red; treating `GeneratorExit` like any other exception in `begin` (`_router.py:154-157`) turns `begin`'s no-clear case red; comparing the stored and current `expires_at` as text turns the `+01:00` case red; removing the shield from `settle_unknown` turns the pending-cancellation 429 case red; removing it from `block` turns the pending-cancellation 401 case red; removing it from `settle_response` turns the pending-cancellation `usage_incomplete` case red; removing the checkpoint from the transport branch turns its `TimeoutError` arm red; ignoring the error event in the `GeneratorExit` handler turns the close-after-error case red; dropping the `block_reason` read from `clear_block()` turns the not-blocked case red; dropping the `warm_prefix_cert` statement turns the query-plan and predates-the-indexes tests red; dropping the purge turns the 26-hour row red; a zero margin turns the 24-hour row and the `-10:00` row red.
New:
> - Mutations: replacing the W preference with the plain order turns the first test red; dropping the length comparison from the read arm turns the one-token-longer and one-token-shorter cases red; dropping the block check turns the blocked-account no-write case red; dropping the server-tool check turns the server-tool arm red; reading `Point.nested` for the flag turns the `source.content` arm red; setting `settled` after a status-branch settlement instead of before turns the settled-stream arm red; treating `GeneratorExit` like any other exception in `begin` (`lionagi/service/accounts/_router.py:154-157`) turns `begin`'s no-clear case red; comparing the stored and current `expires_at` as text turns the `+01:00` case red; removing the shield from `settle_unknown` turns the pending-cancellation 429 case red; removing it from `block` turns the pending-cancellation 401 case red; removing it from `settle_response` turns the pending-cancellation `usage_incomplete` case red; removing the non-streamed outer transport checkpoint turns its `TimeoutError` arm red; removing the new mid-stream post-accounting checkpoint turns the transport-first pending-cancellation arm red; ignoring the error event in the `GeneratorExit` handler turns the close-after-error case red; dropping the `block_reason` read from `clear_block()` turns the not-blocked case red; dropping the `warm_prefix_cert` statement turns the query-plan and predates-the-indexes tests red; dropping the purge turns the 26-hour row red; a zero margin turns the 24-hour row and the `-10:00` row red. Add mutations removing the shield from `settle_rejection` and from estimate settlement. Close-after-error tests must assert the original reason in an open row or estimate evidence, and block precedence, rather than require an open stream_error outcome. The transport TimeoutError arm must assert transport-first and cancellation-first ordering from Decision 2. The settle_response mutation remains the incomplete non-streamed usage case. Exercise the outer streamed checkpoint separately for transport failures without an in-loop latch; it is not the sole checkpoint controlling mid-stream TimeoutError after this amendment.
Reason: Adds mutations for new settlement shields and ordering, retaining the existing unrelated controls.

### R59 — Billing diagnosis context

#### R59.1 (lines 1055–1055)
Old:
> Amendment 7's class table names causes in the request only.
New:
> Before Amendment 8, Amendment 7’s classes named causes in the request only.
Reason: Dates the request-only class vocabulary to the period before Amendment 8.

#### R59.2 (lines 1057–1058)
Old:
> Its message matches no row of that table, so it records `other`, and the account cannot be told apart from a request defect without reproducing the request on the unrouted path.
New:
> Amendment 8 adds billing classes; Amendment 9 settles the exact pair at zero regardless of class. If a 400 remains classed `other`, enabled bounded capture can supply diagnostic wording without reproduction; capture off still drops it, leaving an account with an unmatched billing rejection indistinguishable from a request defect without reproduction on the unrouted path.
Reason: Adds billing classes, typed zero and optional captured diagnosis while retaining the unmatched-billing-versus-request ambiguity with capture disabled.

### R60 — Configuration and diagnostic switch sampling



#### R60.2 (lines 1253–1253)
Old:
> Mid-process configuration reload is DEFERRED: restart to load new declarations.
New:
> Mid-process configuration reload is DEFERRED: restart to load new declarations. Separately, Amendment 9's diagnostic switch is read from process environment exactly once per AccountRouter construction; only "1" enables it. It is not an AppSettings field. Reconstructing the router samples the switch again; an existing router never reloads it.
Reason: Retains the configuration restart requirement and adds separate once-per-router environment sampling for the diagnostic switch.

### R61 — Adopted accounting context

#### R61.1 (lines 1189–1192)
Old:
> This record restates each lion1 decision it adopts (D2 to D6) and rejects two: lion1's ceiling bounded the prompt by the request body's byte count (lion1 ADR-0028 S15), which is an assumption about the tokenizer rather than a provider bound (D5), and lion1's per-wake pick, which binds a long-lived agent rather than a conversation (D3).
New:
> This record restates each lion1 decision it adopts (D2 to D6), except that Amendment 9 permits typed rejection zero, eligible early-stream estimates and sourced closure instead of retaining every unanswered ceiling; it rejects two: lion1's ceiling bounded the prompt by the request body's byte count (lion1 ADR-0028 S15), which is an assumption about the tokenizer rather than a provider bound (D5), and lion1's per-wake pick, which binds a long-lived agent rather than a conversation (D3).
Reason: Qualifies adopted hold accounting with typed zero, stream estimates and sourced closure while retaining the two rejected design choices.

### R62 — Stream cost-contract exception

#### R62.1 (lines 1471–1472)
Old:
> Initially support ordinary Messages requests with text, base64 images, client tools and supported cache TTLs; streaming uses the same cost contract.
New:
> Initially support ordinary Messages requests with text, base64 images, client tools and supported cache TTLs; streaming uses the same admission ceiling and exact complete-usage pricing. Only eligible early-ended streams use Amendment 9’s observed-cost-plus-margin estimate; ambiguous non-streamed calls keep their ceilings.
Reason: Retains the common admission ceiling while distinguishing eligible stream estimates from exact complete usage.

### R63 — Uncertain dispatch retention

#### R63.1 (lines 1539–1540)
Old:
> The owner revokes the one-use dispatch ticket and joins or accounts for that worker before claiming “not sent”; an uncertain handoff keeps the ceiling.
New:
> The owner revokes the one-use dispatch ticket and joins or accounts for that worker before claiming “not sent”; an uncertain handoff keeps the ceiling until transport proof, complete usage, a `RejectionZero` for the same physical attempt with a null or matching recorded request/provider-error reason, or a `StreamUsageEstimate` for that attempt with a null or identical recorded stream-ending reason or explicit sourced reconciliation resolves it. In particular, a committed cancelled reason conflicts with later 400/529 zero evidence; it cannot be released by that pair alone. A committed `cancelled` reason permits only a stream estimate with original reason `cancelled`, priceable observed usage and no complete terminal result; a later transport-reason estimate also conflicts. Joining or accounting for the worker must supply the actual attempt’s typed evidence, not merely observe that the worker finished. Uncertainty itself never proves not-sent or zero.
Reason: Specifies which same-attempt evidence can resolve uncertain handoff; a committed cancelled reason permits only a cancelled stream estimate, not later rejection zeros.

### R64 — Outward error fields

#### R64.1 (lines 1565–1566)
Old:
> Routed errors expose only `code`, optional validated account/attempt ids and HTTP status through the existing event/exception paths.
New:
> Routed errors expose only `code`, optional validated account/attempt ids and HTTP status through the existing event/exception paths. Request rejections additionally expose Amendment 7’s validated type/class fields. A qualifying 529 keeps these outward rejection fields absent and retains provider_error; no provider prose is exposed.
Reason: Adds validated request-rejection type/class fields while preserving the 529 outward-field exception and provider-prose exclusion.

#### R64.2 (lines 1572–1574)
Old:
> The ledger is the authority on routed spend; successful user-facing result types, response bodies and normal chunk shapes stay unchanged apart from secret redaction.
New:
> The ledger is the authority on recorded routed spend, including estimates rather than verified provider charges; successful user-facing result types, response bodies and normal chunk shapes stay unchanged apart from secret redaction.
Reason: Qualifies ledger spend as recorded accounting including estimates; preserves successful result types, bodies and chunks apart from redaction.

### R65 — Rejected alternative heading

#### R65.1 (lines 1659–1659)
Old:
> ### Automatic failover, expiring unknown attempts or status-based zero costs
New:
> ### Automatic failover, expiring unknown attempts or zero costs from status alone
Reason: Distinguishes status alone from the authorized status/type policy.

### R66 — Recorded spend and concentration

#### R66.1 (lines 607–609)
Old:
> A prefix that carries most of a process's traffic stays on one account until the prefix leaves W there (the account fails or passes the bound, or the row lapses) and another candidate ranks ahead of it under D3's order.
New:
> A prefix that carries most of a process's traffic stays on one account until the prefix leaves W there (the account fails, any block clears its rows, it passes the bound, or the row lapses) and another candidate ranks ahead of it under D3's order.
Reason: Makes every block a cause of warmth loss in the existing concentration condition.

#### R66.2 (lines 610–611)
Old:
> The amendment adds no spend: total spend is what the requests cost, wherever they run.
New:
> Amendment 5 changes allocation, not provider pricing. Amendment 9’s estimates can overstate or understate provider spend; recorded totals are therefore not always the actual charges.
Reason: Distinguishes allocation from provider pricing and recorded estimates from actual charges.

### R67 — Stream settlement guard

#### R67.1 (lines 510–512)
Old:
> In the streamed attempt every settlement sets `settled` before it awaits the router (the status branches, `messages.py:1110-1165`, and `:1080-1081`, `:1216-1217`), and the transport-failure branch (`:1250-1257`) settles only when `settled` is false; it still raises.
New:
> In the streamed attempt the existing `settled` boolean is the settlement-attempt flag. Set it before awaiting each router accounting call (the existing status branches, `lionhub/src/lionhub/providers/anthropic/messages.py:1110-1165`, and `lionhub/src/lionhub/providers/anthropic/messages.py:1080-1081`, `lionhub/src/lionhub/providers/anthropic/messages.py:1216-1217`), including Amendment 9's latched transport-loss settlement before output drain or context exit. The outer transport-failure handler (`lionhub/src/lionhub/providers/anthropic/messages.py:1250-1257`) skips accounting when the flag is true but still propagates the failure with a cancellation checkpoint if reached. A failed call is not retried by unwind.
Reason: Extends flag-before-await to pre-drain settlement and forbids unwind retry after a failed call; the following GeneratorExit/context-exit hazard remains unchanged. Dropped or re-cited attribution: `messages.py:1110-1165`, `:1080-1081`, `:1216-1217`, `:1250-1257`, `messages.py:1080-1081`, `messages.py:1216-1217`, `messages.py:1250-1257`.

### R68 — Shield limits and bounded waits

#### R68.1 (lines 499–505)
Old:
> A native `task.cancel()` is not held: pending before the write is admitted, it stops the write; arriving during the worker wait, it returns to the caller at once and the write commits after (`messages.py:704-706`). Both hold for the two existing shields as well. A shielded write also waits without a timeout for a thread-limiter token. D6's five-second bound on the SQLite lock (`busy_timeout_s`) covers that wait, not admission, for the existing shields and for these three alike. Amendment 6 adds a separate admission bound.
New:
> A native `task.cancel()` is not held: pending before worker admission it stops the write; during the worker wait it returns to the caller at once and the write commits after. The worker-dispatch boundary is `lionagi/service/accounts/_ledger.py:343`; the cancellation handler’s settlement write is at `lionhub/src/lionhub/providers/anthropic/messages.py:709-716`. A ledger write that fails still records nothing. This limitation applies to response, unknown, block, not-sent, stream-end and rejection settlement alike. Amendment 6 bounds each call’s thread-token admission wait and SQLite lock wait (`busy_timeout_s`) at five seconds each; the former unbounded admission wait is historical. No combined timeout may report failure after an actual commit.
Reason: Replaces historical unbounded admission with separate five-second waits, keeps busy_timeout_s and the native-cancellation limitation, and cites the executing handler and worker dispatch. Dropped or re-cited attribution: `messages.py:704-706`, D6, `D6`.

#### R68.2 (lines 813–813)
Old:
> Amendment 5, Decision 8 discloses this for the existing shields and for the three it adds.
New:
> Amendment 5, Decision 8, as amended by Amendment 9, describes the former unbounded admission wait as historical and retains the native-task-cancellation limitation for all shielded accounting calls.
Reason: Repoints the shield disclosure after the historical waiting language is replaced; Amendment 6 supplies the separate bound.

### R69 — Connection extension completeness

#### R69.1 (lines 438–440)
Old:
>
> - So every connection `_connect` returns (`_ledger.py:378-396`) holds the table. Every connection is opened writable, so the first open after an upgrade creates it under the lock, and no reader needs a write it could not already make.
New:
> - Every connection `_connect` returns must have the warm-prefix table and indexes, rejection columns and Amendment 9 evidence column. Connections remain writable; the first open after an upgrade creates missing extensions under the schema lock, so readers need no new filesystem write privilege. This extends the existing `_connect`/schema call at `lionagi/service/accounts/_ledger.py:378-396`.
Reason: Extends connection completeness to indexes, rejection columns and evidence while retaining writable creation under the schema lock.

#### R69.2 (lines 442–443)
Old:
> Its failures therefore clear nothing (see the limits below).
New:
> Its failures therefore clear nothing (see the limits below). The older-reader statement describes the pre-warmth extension only. Amendment 9 compatibility is limited to the writer examined in Decision 6.
Reason: Qualifies only the older-reader statement to the pre-warmth extension and the writer examined in Amendment 9 Decision 6.

## Clause index

- l.3 | R01.1
- l.5 | R02.1, R02.2
- l.60 | UNAFFECTED: The CLI fill and output redactor retain their existing credential-isolation limits; accounting and a separate provider diagnostic file do not change child environments.
- l.97 | UNAFFECTED: Credit-period calculation, immutable admission keys and clock-regression refusal remain; daily settlement counts use a separate UTC calendar-day window.
- l.168 | UNAFFECTED: This paragraph explicitly describes the historical pre-affinity implementation at its named revision; new settlement bases do not rewrite that history.
- l.218 | UNAFFECTED: New-scope index choice must occur inside reservation; settlement changes do not move that read outside its transaction.
- l.265 | UNAFFECTED: The certificate-key rules and warm-index identity remain: final frozen-body marks determine the key and points() is unchanged. New quote billing fields neither alter mark placement nor create warmth from an estimate.
- l.266 | UNAFFECTED: The complete table-definition item adds one warm-prefix table keyed by provider, account and certificate. Amendment 9 stores quote/evidence on attempt, so it adds no table, field or key to this warm-prefix item.
- l.269 | UNAFFECTED: The warm-prefix table and its two indexes retain their shape; quote/evidence goes on attempt, not on warm_prefix.
- l.283 | UNAFFECTED: Certificate and expiry indexes retain their lookup/purge roles; neither new evidence variant changes index keys.
- l.299 | UNAFFECTED: Only complete usage within the ceiling on an unblocked certified attempt writes warmth; neither new evidence variant qualifies.
- l.330 | UNAFFECTED: Warm-prefix expiry derives from reservation time; new settlements do not extend that lifetime.
- l.354 | R17.1
- l.356 | R17.1
- l.360 | UNAFFECTED: The complete not-sent clearing item retains transport proof and caller_cancelled: an open-to-settled not-sent that the caller did not cause clears warmth, while caller-caused not-sent does not. Neither new basis changes that distinction.
- l.367 | R50.1
- l.369 | UNAFFECTED: Unblock clears only when the account is blocked and identical adjustment replay returns first; neither new basis changes these predicates.
- l.375 | R18.1
- l.385 | R19.1, R19.2
- l.400 | UNAFFECTED: Configured, unblocked capacity remains the candidate predicate; W arithmetic, integer ordering, no-certificate fallback, atomic binding, fixed scope and absolute cache expiry all remain unchanged when zero costs replace held ceilings.
- l.402 | UNAFFECTED: The W certificate, expiry and 90% inequality retain the same integer arithmetic and budget sources; zero settlement changes their inputs, not this predicate.
- l.411 | UNAFFECTED: No certificate still means empty W; neither new settlement basis supplies a certificate to a request that lacks one.
- l.412 | UNAFFECTED: Selection and binding still read the warm index inside the reservation transaction; evidence settlement does not move that read.
- l.420 | R49.1
- l.422 | UNAFFECTED: A bound scope never consults the warm index or rebinds; settlement changes utilization but never rebinds an existing scope.
- l.426 | R28.1
- l.429 | UNAFFECTED: Schema version remains one and warm_prefix remains outside _TABLES; the following completeness checks are separately edited.
- l.430 | R29.1
- l.432 | R29.1
- l.434 | R29.1
- l.438 | R69.1
- l.441 | R69.2
- l.444 | UNAFFECTED: Warmth is a steering preference, never an admission prerequisite; the evidence-column extension changes no reservation dependence on a warm row.
- l.447 | R51.1
- l.452 | UNAFFECTED: The settle certificate keywords and their meanings remain; existing write/clear rules apply as explicitly amended, without a new forwarding signature.
- l.456 | R51.2
- l.466 | UNAFFECTED: Not-sent evidence, caller_cancelled defaults, delegation and cancellation/GeneratorExit forwarding are unchanged; new rejection accounting never changes proof of no dispatch.
- l.473 | R52.1
- l.482 | UNAFFECTED: The whole paragraph describes pre-Amendment-5 cancellation and the shields that fixed it, including no clear and a held incomplete-200 ceiling; this historical baseline remains true.
- l.488 | R54.1
- l.499 | R68.1
- l.506 | R30.1
- l.510 | R31.1, R67.1
- l.534 | R32.1
- l.583 | R55.1
- l.607 | R66.1, R66.2
- l.619 | R56.1
- l.628 | R33.1
- l.665 | R57.1
- l.668 | R34.1
- l.688 | UNAFFECTED: No-certificate reservation still bypasses the warm table; these tests remain applicable.
- l.698 | UNAFFECTED: Warm-index query and purge behavior remain; no new evidence type changes the indexes.
- l.707 | UNAFFECTED: Stream and non-stream complete-usage settlements still write the same one-hour warmth; the new estimate and rejection bases never do.
- l.719 | R20.1, R20.2, R20.3
- l.744 | R35.1
- l.774 | UNAFFECTED: Bound-scope reservation bypasses warm-table selection; no amendment rebinds it.
- l.778 | UNAFFECTED: Fresh/upgraded warm-table and index creation, missing-base refusal and older-reader admission remain required; evidence-column tests extend these assertions without falsifying any.
- l.783 | R58.1
- l.813 | R68.2
- l.817 | UNAFFECTED: Every call retains separate five-second admission and SQLite bounds, authentication retains two calls, and admission timeout remains non-retryable ledger_busy; new evidence does not change those bounds or errors.
- l.819 | UNAFFECTED: The entire admission-error item preserves ledger_busy propagation and no automatic retry from both begin paths. New settlement evidence operates only after admission and does not change begin error conversion.
- l.860 | UNAFFECTED: No provider text reaches the error or ledger; the optional diagnostic file is neither.
- l.865 | R36.1, R36.2, R36.3
- l.876 | R26.1
- l.883 | R08.1, R08.2
- l.898 | R09.1
- l.914 | R10.1, R10.2
- l.924 | UNAFFECTED: The whole outward-field item retains closed-set validation, internal routed-error pass-through, sanitized _public_routed_failure conversion, exception-context exclusion and streamed propagation. Fields are optional: HTTP 529 passes no rejection fields; qualifying 400 retains them under the replacement policy.
- l.934 | UNAFFECTED: UnknownOutcome keeps its two optional validated diagnostic fields; the two new final variants are distinct types.
- l.935 | R11.1
- l.944 | R12.1
- l.946 | R13.1
- l.952 | R37.1
- l.961 | UNAFFECTED: The message-class substring order remains diagnostic; typed financial control does not change class matching.
- l.966 | UNAFFECTED: Documented diagnostic class match rows remain; the new zero rule does not depend on these message substrings.
- l.973 | UNAFFECTED: Documented diagnostic class match rows remain; the new zero rule does not depend on these message substrings.
- l.976 | UNAFFECTED: The existing spend-limit override and documented-conditions fallback remain; optional capture cannot add a class automatically.
- l.984 | R38.1
- l.986 | R27.1
- l.988 | R14.1
- l.997 | R39.1
- l.1006 | UNAFFECTED: Type alone does not distinguish diagnostic causes; using it for a narrow settlement rule does not give it more diagnostic detail.
- l.1008 | R40.1
- l.1012 | R41.1
- l.1021 | R15.1
- l.1028 | R15.2
- l.1031 | R15.3
- l.1039 | R15.4
- l.1041 | R15.4
- l.1043 | R15.5
- l.1046 | UNAFFECTED: The unrouted body/error path is untouched; new rules apply only to routed attempts.
- l.1050 | R16.1
- l.1055 | R59.1, R59.2
- l.1062 | UNAFFECTED: The captured billing-error JSON remains historical diagnostic evidence; the new policy neither changes its bytes nor treats it as proof for another attempt.
- l.1066 | UNAFFECTED: The original wording remains attributed to a capture rather than documentation; no new documentation provenance is asserted.
- l.1069 | UNAFFECTED: The quoted spend-limit wording and its Amendment 8 override remain unchanged; other named undocumented-message conditions still classify as other.
- l.1086 | UNAFFECTED: The credit-balance class still cites its captured wording; no new wording is asserted.
- l.1089 | R16.2, R16.3
- l.1095 | R16.4
- l.1100 | UNAFFECTED: The conditions item still has no documented/captured disabled-account wording, and class addition still requires a later sourced amendment; 402/429 siblings retain their existing type/rate-limit handling. Optional future capture does not create a class or read a 429 body now.
- l.1114 | R38.2
- l.1116 | R38.3
- l.1119 | UNAFFECTED: The credit-balance row still rests on one capture and rewording still falls to other; diagnostic capture may help later but changes no current match.
- l.1128 | UNAFFECTED: Both paths still retain invalid_request_error and the matched billing class on error and ledger; the new final outcome adds evidence without dropping those fields.
- l.1130 | UNAFFECTED: The existing captured credit-balance fixture still matches credit_balance_low under the unchanged classifier.
- l.1133 | R42.1
- l.1182 | R61.1
- l.1201 | UNAFFECTED: Selection, reservation and settlement remain transactions in one local ledger file; the new read adds no separate accounting authority.
- l.1202 | R25.1
- l.1203 | UNAFFECTED: Retries remain on the same account, uncertainty remains explicit and the unconfigured path remains unchanged.
- l.1251 | R60.2
- l.1260 | UNAFFECTED: Construction without account configuration remains unchanged, outside routed settlement.
- l.1273 | UNAFFECTED: CredentialSources retains dotenv and lookup sources; settlement evidence contains no credentials.
- l.1282 | UNAFFECTED: The complete paragraph fixes one case-preserving credential reference across env, dotenv and lookup. The diagnostic flag is a separate noncredential switch and evidence settlement never resolves or renames a credential, so none of these lookup clauses changes.
- l.1286 | UNAFFECTED: One-name credential lookup, subprocess limits and exception sanitization remain; provider diagnostic capture neither invokes nor exposes lookup subprocesses.
- l.1316 | UNAFFECTED: All key custody, transport sanitization and redaction promises remain; the independent parser retains billing fields only and diagnostics use the supplied redaction callable.
- l.1343 | UNAFFECTED: The full scope-custody paragraph fixes scope before enqueue and forbids shared endpoint mutation and rebinding across errors or rollover. New settlement selects only immutable attempt identity, and no settlement moves an existing binding.
- l.1360 | R43.1
- l.1374 | UNAFFECTED: The ledger remains private accounting beside global settings, separate from StateDB and shared only among cooperating local processes.
- l.1383 | R23.1
- l.1395 | R44.1
- l.1416 | R45.1
- l.1420 | R46.1
- l.1427 | R21.1, R21.2, R21.3
- l.1442 | R22.1, R22.2
- l.1447 | R47.1, R47.2, R47.3
- l.1460 | R24.1
- l.1471 | R62.1
- l.1496 | UNAFFECTED: Final complete usage still prices at captured revision and exact served model; partial estimates use a separate, labeled rule.
- l.1503 | R48.1
- l.1514 | R05.1, R05.2
- l.1531 | UNAFFECTED: The D6 heading retains its failure/cancellation/default scope; no substantive guarantee rests on the heading.
- l.1533 | R63.1
- l.1547 | UNAFFECTED: The 429 table row retains ceiling, rate-limit classification and bounded backoff on the same account. The exact zero table excludes 429 and a 429 before stream start supplies no eligible observed stream vector.
- l.1548 | R03.1
- l.1549 | R03.1
- l.1550 | R03.1
- l.1552 | R04.1
- l.1565 | R64.1, R64.2
- l.1585 | UNAFFECTED: No router service, SDK, registry, public conversation parameter or implicit failover is added; existing default-path and routing verification remains required.
- l.1596 | R06.1
- l.1618 | UNAFFECTED: The dedicated-keychain/full-environment-fill alternative remains rejected for its existing timeout, trust and custody costs; settlement evidence changes none of them.
- l.1659 | R65.1
- l.1661 | R07.1
````

## Edits made after acceptance

Each edit below replaced text that occurred exactly once in the document it was applied to. L1 to L4 are four
successive sets of edits.

````text
### L1.1 — Decision 2: which streams are eligible

Old:
> `message_start` without `message_stop` is included if it carries priceable usage. Merely seeing an event is insufficient: before any usage is seen, the ceiling remains.
New:
> A stream that ends after `message_start` and before any complete `message_delta` with valid usage is not eligible and keeps the ceiling: the output count `message_start` carries is a placeholder, not observed output. Merely seeing an event is insufficient: before a complete `message_delta` with valid usage is seen, the ceiling remains.
Reason: The output count `message_start` carries is a placeholder, so a stream with no complete `message_delta` carrying valid usage has no observed output and keeps the ceiling.

### L1.2 — Decision 2: the observed output count

Old:
> A valid initial output count can be used for the estimate, including zero; it is not evidence that generation stopped there. An observed output count is mandatory: if no complete event has supplied a valid output count, keep the ceiling. A missing later output field retains an earlier valid count; an invalid replacement invalidates it.
New:
> The observed output count comes only from a complete `message_delta` with valid usage; the `message_start` output count never qualifies, whatever its value, while its input and cache counts are still used. A qualifying count is not evidence that generation stopped there. An observed output count is mandatory: if no complete `message_delta` has supplied a valid output count, keep the ceiling. A missing later output field retains an earlier qualifying count; an invalid replacement invalidates it.
Reason: Only a complete `message_delta` supplies the observed output count; the input and cache counts from `message_start` still count.

### L1.3 — Decision 2: why the floor exists

Old:
> The fixed floor gives a nonzero allowance when only initial usage is available;
New:
> The fixed floor gives a nonzero allowance when little output has been observed;
Reason: Follows the two edits above: initial usage alone no longer qualifies, so the floor covers a stream with little observed output.

### L1.4 — Consequences: latency of a 400 or 529

Old:
> the local ledger may undercount actual spend; the margin is not a hard budget guarantee.
New:
> the local ledger may undercount actual spend; the margin is not a hard budget guarantee. A 400 or 529 response can now wait up to fifteen seconds before its routed error is raised: the five-second shielded body read, then the settlement attempt with Amendment 6's per-call worst case of ten seconds; caller cancellation does not shorten the shielded parts.
Reason: States the worst-case wait added before a 400 or 529 error reaches the caller.

### L1.5 — Verification: a stream that ends after `message_start`

Old:
> Verify initial-only and later cumulative stream counts,
New:
> Verify that a stream ending after `message_start` with no qualifying `message_delta` keeps the ceiling, later cumulative stream counts,
Reason: Verification covers the stream that ends after `message_start` without a qualifying `message_delta`.

### L2.1 — Decision 7: list numbering

Old:
> 7. **Typed zero affects utilization; repeated rejection does not automatically block.** Retain D3 ordering, Amendment 5's candidate rule and Amendment 8 Decision 2's operator block. Releasing the held ceiling changes utilization; an account that keeps rejecting remains a candidate until an operator blocks it. Existing authentication and accounting blocks remain. No persistent rejection counter, new block reason, counter schema or migration, or new error code is added. Cost: repeated failures can keep new cold scopes on an account that cannot serve them until an operator blocks it. Zero settlement releases C from open reservations R without adding spend S, removing the accumulation of held ceilings that would otherwise saturate B under D5. A qualifying 529 keeps its bounded same-account retry, with each zero reducing committed utilization from its reserved level; zero accounting therefore supplies no pressure to move that retry or subsequent new scopes away. Fixed bindings remain; a rejected request is not moved or retried on another account.
New:
> 7. **Typed zero affects utilization; repeated rejection does not automatically block.** Retain D3 ordering, Amendment 5's candidate rule and Amendment 8 Decision 2's operator block. Releasing the held ceiling changes utilization; an account that keeps rejecting remains a candidate until an operator blocks it. Existing authentication and accounting blocks remain. No persistent rejection counter, new block reason, counter schema or migration, or new error code is added. Cost: repeated failures can keep new cold scopes on an account that cannot serve them until an operator blocks it. Zero settlement releases C from open reservations R without adding spend S, removing the accumulation of held ceilings that would otherwise saturate B under D5. A qualifying 529 keeps its bounded same-account retry, with each zero reducing committed utilization from its reserved level; zero accounting therefore supplies no pressure to move that retry or subsequent new scopes away. Fixed bindings remain; a rejected request is not moved or retried on another account. <!-- markdownlint-disable-line MD029 -->
Reason: The decision list continues its numbering across the repeated-rejections heading, which markdownlint rule MD029 reports; the comment suppresses that rule on this line only.

### L3.1 — Decision 6: the `StreamUsageEstimate` fields

Old:
> `StreamUsageEstimate` contains the original ending reason, exact served model, validated observed counts, optional validated request id, the captured quote pricing inputs, `observed_cost_nusd`, `margin_tokens`, `margin_nusd` and `cost_nusd`, all amounts/counts integers
New:
> `StreamUsageEstimate` has exactly the fields of its shared-contract signature below, which is its single definition: the original ending reason, exact served model, validated observed counts, optional validated request id, the quote pricing inputs, their `quote_origin`, the applied `rates`, `observed_cost_nusd`, `margin_tokens`, `margin_nusd` and `cost_nusd`, all amounts/counts integers
Reason: The fields were listed both in this prose and in the shared-contract signature, and the prose omitted two; the prose now names the signature as the single definition.

### L3.2 — Consequences: cost of the daily census

Old:
> caller cancellation does not shorten the shielded parts.
New:
> caller cancellation does not shorten the shielded parts. The daily census's validation pass reads every settled attempt row of the provider inside one read transaction, so its cost grows with the ledger's history and recurs for each counted UTC day. Under the ledger's rollback journal that transaction's shared lock holds back settlement commits until it ends, and a commit that waits past `busy_timeout_s` raises `ledger_busy` as under Amendment 6. This amendment does not bound the pass; the retained census record carries the read's elapsed time, so a later amendment can bound it from measurement, for example by a day-keyed scan or chunked validation.
Reason: States the cost of the census validation pass and its lock consequence under the ledger's rollback journal.

### L3.3 — Consequences: retention of diagnostic files

Old:
> Diagnostic capture may support future classification at the cost of an explicitly enabled sensitive local file.
New:
> Diagnostic capture may support future classification at the cost of an explicitly enabled sensitive local file. Its retention is enforced lazily: until a capture is admitted or an explicit cleanup runs, written files stay on disk past seven days, including after capture is turned off, so an operator ends their retention with `cleanup_rejection_diagnostics`.
Reason: States that retention is enforced lazily and names the call that ends it.

### L3.4 — Amendment 7 Purpose

Old:
> while keeping the rule that no provider text reaches either.
New:
> while keeping the rule that no provider text reaches either. Amendment 9 keeps that rule for the routed error and the ledger: settlement of a rejected request reads its HTTP status and error type, never its message, and only Amendment 9's opt-in private diagnostic file (its Decision 5) may hold redacted message text.
Reason: Amendment 7's Purpose says no provider text reaches the routed error or the ledger; it now says what Amendment 9 changed of that rule and what it kept.

### L4.1 — Consequences: trust boundary of typed reconciliation

Old:
> so an operator ends their retention with `cleanup_rejection_diagnostics`.
New:
> so an operator ends their retention with `cleanup_rejection_diagnostics`. Typed reconciliation is an operator act, and its trust boundary is the one every ledger write already has, write access to the ledger file: origin `operator` and the `source_ref` syntax check record the operator's testimony without authenticating it, and a mistaken typed closure stays until a sourced adjustment corrects it, because a later runtime settlement on the reconciled row returns `already_settled` without comparison.
Reason: States that typed reconciliation adds no trust boundary of its own and how a mistaken closure is corrected.
````

## How the edits made on acceptance were checked

The 97 edits were applied by a script and checked mechanically before they were committed:

- Every edit parses, and each Old passage was found at its stated line range in ADR-0126 at c6a3b4af. Seven edits
  do not start or end on a sentence boundary (R01.1, R21.1, R21.2, R21.3, R23.1, R24.1, R65.1); their Old text is
  quoted as it stands in the record.
- No two edits overlap, and applying them changed no text outside the quoted Old passages: 448 diff hunks, none
  outside.
- A code citation an edit removes is named in that edit's Reason. The 87 code citations in the new text point at
  lines that exist in their files at c6a3b4af; citations of a bare `endpoint.py` were not resolved.
- Every paragraph an edit touches, and every paragraph that mentions one of the affected rules, has a row in the
  clause index.
- Deliberately broken copies of the edits (a changed Old word, a dropped citation, a change outside every Old
  passage, overlapping edits, a wrong line range) each fail the matching check.
