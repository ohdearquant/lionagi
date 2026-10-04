---
adr: ADR-0022
status: proposed
liveness: partial (the host's `/v2` routes pass their tests; the app is written and not yet compiled, so its tests have never run; neither is on main)
date: "2026-10-04"
area: app
kind: new
depends_on:
  - "[[ADR-0013-the-agent|ADR-0013]]"
  - "[[ADR-0017-the-desk|ADR-0017]]"
  - "[[ADR-0021-the-served-actor|ADR-0021]]"
tags:
  - adr
  - app
  - areas
---

# ADR-0022: The app and its host

## Context

This record serves the author's requests of 2026-10-04:

- 07:25:30: not good enough; rebuild the app, as asked before
- 07:30:45: "needs attention" means the author's attention
- 07:31:38: a quick-reply feature
- 07:32:12: too intrusive
- 08:02:09: more consistent design across add-model, plugin, add-tool and similar pages
- 08:09:12: usable first
- 08:13:17: everything here is long-running
- 08:14:36: configuration, rarely used, belongs in Settings
- 08:15:21: never show an API key
- 08:23:08: consistent header heights across pages

The residents already serve from their directories, and the areas server already reads, holds, wakes
and restarts them ([[ADR-0017-the-desk#^c11|ADR-0017/C11]]). This record fixes the first usable
release over them: a Mac app that speaks to one host, the `/v2` routes the host answers, and what
each route reads and writes.

It fixes no plugin format, shell policy, provider credential, shared limit or subagent tool; each
waits for its own record (S13 to S16). Source: `apps/cli/lion_cli/daemon.py`, `_v2` in
`apps/cli/lion_cli/areas.py`, `hub/areas.py`, `apps/mac/`.

## Definitions

- **host**: the areas server, `lion areas --serve`, as the app's one address: the `/v2` routes over
  the registry's residents, the person's mail and the model library.
- **app**: the Mac app in `apps/mac/`, a sandboxed client of one host.
- **definition**: the part of a resident's `chores.toml` the app shows and edits, read as one JSON
  object.
- **capability list**: the `can` list in the `daemon` object of `GET /v2/today`, naming each write
  the host serves by a short name.
- **For you**: the person's unread mail as the app lists it, every item open.
- **once key**: the 32 hex digits a write carries; a repeat of the same method, path and key gets
  the first answer.
- **model library**: the models the person added, kept in `models.toml` beside the registry,
  together with every model a resident runs.
- **demo mode**: the app launched with `-demo`, reading recordings bundled with it and reaching no
  host.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | A resident reads its `hold` and `wake-now` files at each wake, and `[budget]` and `[wake]` of its `chores.toml` at each poll, so a control or a change to those tables reaches it with no signal from the host. | `Agent.wake` calls `hold` first, `_hand_wake` consumes `wake-now` and `_reload` follows the two tables (`hub/agent/agent.py`) | a control answers 200 and nothing changes; the host would have to signal the process |
| A2 | An agent that needs the person mails the identity the host runs as. | `_items` reads the inbox of the `--as` identity | For you misses what needs the person |
| A3 | OpenRouter's model list answers with no key, and its `/key` route answers the key's own limit and usage to an ordinary key. | `_OPENROUTER_MODELS` and `_OPENROUTER_KEY` in `daemon.py` | the catalogue or the usage answers 502 and shows nothing |
| A4 | One launchd job runs each resident, described by the one plist in its directory. | `_restart` reads that plist's `Label` and kickstarts it | a restart is refused with 409 and the person restarts the resident by hand |

## Claims

### C1: Every agent has one id from the registry, and one process owns its lifecycle _(enforced: mechanical)_ ^c1

- **Subject**: every agent a `/v2` route names, and every restart.
- **Violated when**: two agents of one read share an id; an id is built from anything but the area's
  name and the directory; or the host starts a resident's process, or restarts one whose launchd job
  it cannot name.

The app shows each resident of the registry as an agent. Its id is `<area>/<directory name>`. When
two directories of one area share a name, or the name is empty, each gets `~` and six hex digits of
a hash of its path (D3).

The resident's own process holds its identity's lock ([[ADR-0013-the-agent#^c1|ADR-0013/C1]]), and
the host starts no process. A restart kickstarts the launchd job of the one plist in the resident's
directory. It is refused when there is none or more than one, and during a wake unless forced.

### C2: The `/v2` routes answer loopback only, check Host and Origin on every method, and take a once key on every write _(enforced: mechanical)_ ^c2

- **Subject**: every request to a `/v2` route, the event stream included.
- **Violated when**: a peer off loopback is answered; a request whose Host is not a loopback
  spelling of the bound port, or whose Origin is present and names another host, is answered on any
  method; or a write without a well-formed once key acts.

Before it reads a write's body, the host answers a route it does not serve with 404. A write then
carries `once`, 32 hex digits with a UUID's hyphens allowed, or it is refused with 400 and nothing
is done. Writes run one at a time, and a repeat of the same method, path and once key gets the first
answer (S8).

A Host is `127.0.0.1:<port>` or `localhost:<port>`, and an Origin is `http://` and one of them.
Refusal is 403, before anything is read or done. The older routes keep their guard
([[ADR-0017-the-desk#^c11|ADR-0017/C11]]), which also admits the tailnet and `--allow` names (S2).

### C3: The app offers only what the capability list names, and the list is no grant _(enforced: mechanical; owed, S3)_ ^c3

- **Subject**: every control the app shows, and the capability list.
- **Violated when**: the app shows a control for a write the list does not name, or one the host
  refuses whatever its body. Also when the list names a write no route serves or omits one a route
  serves, or a route takes a listed name as permission.

The app reads the capability list from `GET /v2/today` and shows a write's control only when its
name is listed. Before that first read it offers none. A write the host does not serve answers 404
before its body is read, and its name is absent from the list.

The list states what exists. Admission is C2's, and each route checks its own body.

### C4: A definition write changes only editable keys, in place, all or nothing _(enforced: mechanical)_ ^c4

- **Subject**: every `PUT /v2/agents/{id}/definition`.
- **Violated when**: a key outside the agent's editable paths changes, or a byte outside the changed
  values; or the file is left partly written or in a form the resident's loaders refuse. Also when
  the host writes over a file whose bytes, at its compare, differ from those it read.

An unknown path, a read-only one or a value outside its rule refuses the whole write with 400,
naming the path. The answer lists each change, old and new, and whether it waits for a restart.

The host sets each key in the file's own text, reads the result back, requires that only those keys
changed, and runs the resident's loaders on it. A dry run stops there. Otherwise it compares the
file's bytes with those it read, refuses with 409 when they differ, and replaces the file by one
rename, keeping its mode. The compare is not a lock (S6).

### C5: Hold, resume, wake now and restart are four controls with four effects _(enforced: mechanical)_ ^c5

- **Subject**: every `POST /v2/agents/{id}/control`.
- **Violated when**: an action outside the four acts; a control acts on a directory the registry
  does not name; or one control has another's effect.

Hold writes a `hold` file with its reason, and while it stands the resident starts no wake, a
requested one included. Resume removes the file. Wake now writes a `wake-now` file; at its next poll
the resident wakes on the mail that waits or on its queue's top task, and does not wake when neither
waits. Restart kickstarts the resident's launchd job (C1).

Each control answers the agent's row read after it. The host offers no stop of a running wake.

### C6: For you holds the person's mail, an answer goes back on its thread, and no text is read as an approval _(enforced: mechanical)_ ^c6

- **Subject**: `GET /v2/foryou`, and every answer and done.
- **Violated when**: an item is anything but mail to the identity the host runs as; or an answer
  goes to another recipient or thread, or twice for one once key. Also when an answer's text or a
  choice is taken as an approval, or a done leaves an open item unread.

For you lists the person's unread mail. An item on a thread the person wrote on is a reply, any
other a note, and none carries choices: the host refuses a `choice`.

An answer is the person's text, sent to the item's sender on its thread with `Re:` before its
subject. The once key is the send's idempotency key, and the item is then marked read. A done marks
it read. Either way it leaves the list; an id not listed answers 404. Nothing in the host reads an
answer's text, so no reply is taken as an approval.

### C7: No route returns a saved provider key, and this host takes none _(enforced: mechanical)_ ^c7

- **Subject**: every answer of every `/v2` route, and the capability list.
- **Violated when**: an answer holds a saved key or any part of one; or the host lists
  `provider_key` or serves a route that writes or removes a key.

A provider answers only whether a key is set, as `key` with one field, `set`, read from the keychain
without its value. Writing a key belongs to the record on providers and credentials (S15). Until it
lands the host serves no key route and the capability list omits `provider_key`. The app already
shows its key field only under that name.

### C8: A figure the host cannot read from its source is unknown, never estimated _(enforced: mechanical)_ ^c8

- **Subject**: every cost, count, price, context size and usage figure a `/v2` answer carries.
- **Violated when**: a figure its source did not report is answered as a number, or a partial one as
  whole; or the host computes a price or a usage figure the provider did not report.

A model's price, context size and image input come from the provider's list, and are null when it
lists none. A router's price, which OpenRouter does not fix, is null. A usage field OpenRouter does
not report is absent, and its log is empty for an ordinary key. Without an identity the For you
count is null, never 0. A CLI's sign-in is `unknown`, since the host does not ask it.

A cost missing calls the resident counted as of unknown cost is null, for a day or a wake, as is
every sum over one. So is the day's cap when a resident's budget is refused (S5).

### C9: Demo mode is chosen at launch, labelled on every screen, and reaches no host _(enforced: mechanical; owed, S1)_ ^c9

- **Subject**: every launch of the app.
- **Violated when**: the app enters demo mode without `-demo` outside its tests, or falls back to it
  when the host is down; a screen in demo mode lacks the label; or a demo read or write leaves the
  app.

Demo mode reads the fixtures bundled with the app, plays every write against them in memory and
replays one recorded event stream (D8). Every screen's header shows a `demo` mark, the sidebar says
the data is recorded, and the host address cannot be changed. A host that cannot be reached shows as
down, never as the demo.

### C10: The sandboxed app reads only what the person drops, and the host opens only under registered directories _(enforced: mechanical; owed, S1)_ ^c10

- **Subject**: every file and connection the app opens, and every `POST /v2/open`.
- **Violated when**: the app reads a file the person did not drop, or accepts a connection; or the
  host opens a path that, its links resolved, lies under no directory the registry names, opens a
  folder in an editor, or runs what it opens.

The app's entitlements are the sandbox and outgoing connections, nothing else. So it reads its own
bundle and container, and an image dropped on a message box arrives as data.

A file or a folder is opened by the host, after the person picks Open on an agent's menu or a
plugin's page. The host resolves the path and every directory the registry names, and opens a file
in the text editor (`open -t`) and a folder in the Finder (`open -R`). It refuses anything else with
400 or 404.

### C11: The tree holds no signing identity, no bundle prefix and no machine's name _(enforced: mechanical; owed, S11)_ ^c11

- **Subject**: every tracked file under `apps/mac/`.
- **Violated when**: a team id, a real bundle prefix, a host name, a tailnet address or a home path
  is tracked, or the project builds only with one.

The app is signed ad hoc, with no team. `project.yml` takes `LION_BUNDLE_PREFIX` from
`Local.xcconfig`, which git ignores and `build.sh` copies from the tracked example when it is
missing; the example's prefix is `com.example.lion`. The generated project is ignored too.

### C12: A screen is read against named qualities before it lands _(enforced: process)_ ^c12

- **Subject**: every screen a change adds or alters.
- **Violated when**: a screen lands without a screenshot read against the qualities below, or with a
  quality it fails and does not name.

Every screen has one header of one height, with back and forward, and back returns to the page shown
before. Moving between pages never resizes the window, and a pane that overflows scrolls inside it.
The side panels collapse.

Every scrolling pane has the one thin scrollbar the For you list has. Adding a model, a plugin or a
provider is one form. Nothing asks for attention that is not the person's: For you keeps a fixed
height, and a reply field opens only when the person asks for it.

### C13: After a reconnect the app shows current state with no row missing or doubled _(enforced: mechanical; owed, S10)_ ^c13

- **Subject**: what the app shows after its event stream drops and reopens.
- **Violated when**: after the reopen, a page shows a row the host no longer has, lacks one the host
  has, or shows one row twice.

When the stream reopens after a failure, the app reads Today, For you, the model library, the
plugins and the page it shows again. A wake event replaces the wake of the same id or is added once.
The host's stream does not resend what it missed, so a page not shown at the reopen keeps what it
had.

## Decisions

### D1: `apps/mac/`, generated by xcodegen and built under the machine's build locks ^d1

Serves C9, C10, C11 and C12. `project.yml` declares the app, its fixtures as a resource folder and
an XCTest target the app hosts: macOS 14, sandboxed, signed ad hoc. `build.sh` regenerates the
project, then builds and tests under the two lock wrappers in `LION_LOCK_DIR`, read from the
environment or `Local.xcconfig`. Unset or missing wrappers refuse with 78, `none` builds unlocked,
and a lock's refusal exits 75. The derived data and the build log go outside the tree by default.
Every screen draws its header with one `ScreenBar`.

- **Landing evidence**: owed: `build.sh` passing `Tests/DecodingTests.swift` and
  `Tests/StoreTests.swift`, which are written and have never run (S1).

### D2: The `/v2` routes: `Daemon` in `apps/cli/lion_cli/daemon.py`, behind `_v2` in `areas.py` ^d2

Serves C2, C3, C5 and C8. `lion areas --serve` builds one `Daemon` over the registry and the `--as`
identity. `_v2` hands reads to `Daemon.get` and writes to `Daemon.route`, which answers None for a
write it does not serve; the control goes to the server's own `_act`. Every write runs under the
lock `/control` takes. A route not offered answers 404 and its name is absent from the capability
list (D7).

| method and path | does | answers |
| --------------- | ---- | ------- |
| `GET /v2/today` | totals, each area with its agents, the For you count, the host's version and name, and the capability list | 200 |
| `GET /v2/areas/{name}` | the chair, the agents, and a timeline of wakes and escalations, newest first | 200; 404 |
| `GET /v2/agents/{id}` | the row, the definition, its editable paths and the path of `chores.toml` | 200; 404 |
| `GET /v2/agents/{id}/wakes?before=&limit=` | the wakes in `serve.log`, newest first, with `more` | 200; 400, 404 |
| `GET /v2/agents/{id}/record?wake=&after=` | the lines of `serve.log` with their numbers: one wake's, or those after a number | 200; 400, 404 |
| `GET /v2/foryou` | For you (D5) | 200; 502 |
| `GET /v2/plugins` and `GET /v2/plugins/lion` | the default plugin, read only: the registered commands, the profiles and the agents that use them | 200; 404 for another name |
| `GET /v2/library/models` | the providers and the model library (D6) | 200 |
| `GET /v2/library/providers/openrouter/catalogue?q=` and `.../usage` | the provider's list (D6), and its figures for the key | 200; 404 for another provider, 409, 502 |
| `GET /v2/profiles` | the four profiles the tree builds | 200 |
| `GET /v2/events` | a stream of `today`, `agent`, `wake`, `foryou`, `library` and `ping` events | 200 as `text/event-stream` |
| `PUT /v2/agents/{id}/definition` | C4 | 200; 400, 404, 409 |
| `POST /v2/agents/{id}/control` | hold, resume, wake or restart (C5) | 200 with the row; 400, 404, 409, 502 |
| `POST /v2/agents/{id}/message` | text as mail to the agent's actor; images refused | 200; 400, 404, 409, 502 |
| `POST /v2/foryou/{id}/answer` and `.../done` | C6 | 200; 400, 404, 409, 502 |
| `POST /v2/library/models`; `PATCH` and `DELETE /v2/library/models/{id}` | D6 | 200; 400, 404, 409, 502 |
| `POST /v2/library/providers/{name}/check` | codex's release, or OpenRouter's word on the key | 200; 404 for `claude_code` or an unknown provider |
| `POST /v2/open` | C10 | 200; 400, 404, 502 |
| `PUT` and `DELETE /v2/library/providers/{name}/key` | not offered (C7, S4) | 404 before the body is read |
| `POST /v2/agents`, `/v2/agents/inspect`, `/v2/library/models/{id}/test`, `/v2/library/providers` and `/v2/give`; `PATCH /v2/library/providers/{name}`; every other `/v2/plugins` route and every `/v2/marketplaces` and `/v2/shell-rules` route | not offered | 404 |
| any route | the shared answers | also 403 (C2), and 500 for a registry or a file read mid-write |

- **Landing evidence**: `test_every_v2_write_takes_a_well_formed_once_and_without_one_does_nothing`,
  `test_control_holds_and_resumes_answers_the_row_and_refuses_what_it_does_not_serve`,
  `test_events_send_today_then_each_new_wake_and_a_change_of_the_library_or_the_mail`, and the
  refusals of C2: `test_v2_refuses_a_peer_off_loopback_before_it_reads_or_does_anything`,
  `test_v2_refuses_a_tailnet_peer_the_older_routes_admit`,
  `test_v2_refuses_a_host_name_added_with_allow_that_the_older_routes_answer`,
  `test_v2_refuses_a_foreign_origin_on_a_get_that_the_older_routes_answer`,
  `test_v2_refuses_a_foreign_origin_on_a_write_before_its_body_is_read`,
  `test_the_v2_event_stream_refuses_what_every_v2_route_refuses`, and the unknowns of C8:
  `test_an_agents_partial_cost_is_unknown_and_so_is_every_sum_over_it`,
  `test_the_days_cap_is_unknown_when_a_residents_budget_is_refused`,
  `test_a_wake_that_missed_calls_of_unknown_cost_has_no_cost`.

### D3: An agent's id is its area and its directory's name ^d3

Serves C1. `agent_ids` walks the registry in order and names each directory `<area>/<name>`. When
the name is empty or repeated in the area, it adds `~` and the first six hex digits of SHA-256 over
the directory as the registry writes it. `agent_path` reads an id from two path segments or from one
holding `%2F`. `Daemon.find` reads the registry again on every request.

- **Landing evidence**: `test_two_desks_of_one_area_with_one_name_get_ids_that_tell_them_apart`,
  `test_an_agent_answers_its_row_its_definition_and_what_the_app_may_change`, and in
  `tests/test_areas.py`,
  `test_restart_refuses_without_one_plist_or_in_a_wake_and_kickstarts_the_label`.

### D4: The definition: `chores.toml` read as the app's shape and written back key by key ^d4

Serves C4. `_definition` reads the model, where a plist's `--model` wins, then `[budget]`, `[wake]`
and a chair's `system`. The editable paths are `model`, `limits.cost_per_day`,
`wake.at_most_per_hour`, `wake.quiet` and `instructions.extra`. `model` drops out when a plist names
one, `wake.quiet` when the file holds several ranges, and `instructions.extra` on anything but a
chair. A model must be in the model library. `_set_key` finds a value as the shortest span that
reads back as it, so a comment after it stays.

- **Landing evidence**:
  `test_a_definition_write_changes_its_keys_and_keeps_every_other_byte_and_a_replay_writes_nothing`,
  `test_a_definition_the_loaders_or_the_app_shape_refuse_leaves_the_file_byte_identical`,
  `test_a_definition_saved_elsewhere_after_the_read_is_refused_409_and_left_as_saved`.

### D5: For you is read from the person's mail through the host's identity ^d5

Serves C6. `lion areas --serve --as DIR` gives the host an identity; without one For you is empty
and the For you count is null. For you lists the person's unread mail: `_items` reads the unread box
page by page to its end, and one page of sent mail. It names an item's agent when the sender is a
resident's actor. `answer` refuses a choice, sends the text, then marks the item read; `done` marks
it read, and an id not listed answers 404.

- **Landing evidence**:
  `test_for_you_lists_the_persons_unread_mail_newest_first_and_names_its_agent`,
  `test_an_answer_goes_back_to_the_asker_on_its_thread_and_marks_the_item_done`,
  `test_a_done_marks_the_item_read_and_it_leaves_the_list_and_the_count`,
  `test_the_for_you_count_is_the_open_items_and_null_when_the_mail_is_not_read`,
  `test_today_counts_each_resident_and_names_what_this_server_does`.

### D6: The model library in `models.toml`, and OpenRouter's list kept for a day ^d6

Serves C8. `models.toml` beside the registry holds the models the person added, written whole by a
rename. A model is added only through `claude_code`, `codex` or `openrouter`, and for OpenRouter
only when its list holds the id; a dry run writes nothing, and no test call is made. A change takes
only `shown_as`, and a removal is refused while a resident runs the model. OpenRouter's list is kept
in `daemon/` beside the registry and fetched again once a day old; a failed fetch keeps the old one.

- **Landing evidence**:
  `test_a_model_is_added_only_when_its_provider_lists_it_and_removed_only_when_unused`,
  `test_the_catalogue_and_the_usage_are_openrouters_own_recorded_answers`,
  `test_the_library_lists_the_providers_and_every_model_added_used_or_run_by_default`.

### D7: The capability list is `CAN` in `daemon.py` ^d7

Serves C3 and C7. `CAN` names `definition`, `control`, `message`, `foryou`, `models`,
`check_provider` and `open`, and no `provider_key`, since no route writes or removes a key (S4).
`GET /v2/today` answers it as `daemon.can`. The app's `Store.can` reads it and answers false before
the first read, and each control in the app's views is shown under one name.

- **Landing evidence**: `test_can_names_exactly_the_writes_a_route_serves`,
  `test_no_route_writes_or_removes_a_key_and_no_answer_carries_one`, and
  `testActionsFollowTheDaemonsCanList`, not yet run (S1).

### D8: Fixtures, and demo mode over them ^d8

Serves C9. `Fixtures/` holds recorded answers for the routes the app reads and for some writes, and
one recorded event stream, bundled with the app and with its tests. `DemoTransport` answers reads
from them, plays writes against its copies in memory and keeps a list of them, and replays the
stream once. The app's clock stands at the recording's time. `LionApp` takes it only under `-demo`
or XCTest.

- **Landing evidence**: `testEveryFixtureDecodesAsTheAnswerItsNameSays`,
  `testTheEventRecordingParses`, `testAnsweringAForYouRowRemovesIt`; written, not yet run (S1).

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| Serving each resident through the served actor before the app ships | nothing of the served actor is on main, and the first usable release reads what the residents already write ([[ADR-0021-the-served-actor\|ADR-0021]]) |
| A second server for the app | a second admission surface beside the areas server's; `/v2` rides that server and its guard, narrowed to loopback (C2) |
| `/v2` admitted for the tailnet as the older routes are | `/v2` rewrites definitions and the model library, and a tailnet peer is another machine (S2) |
| A key typed into the app in this release | who owns the key, how the app and the host share the keychain and how a key is replaced belong to the record on providers (S15) |
| Returning a key's first characters so the person can tell keys apart | they are the key's characters too (C7) |
| A lock file around the definition write | a person's editor takes none, so it would bound only the host's own writes, which already run one at a time (C4) |
| Opening files from the app | the sandbox would need read access to the person's directories; the host already reads the ones the registry names (C10) |
| Demo mode as the fallback when the host is down | recorded rows would read as live state (C9) |
| A price or a usage figure computed from token counts | it would read as the provider's own; a figure the provider does not report stays unknown (C8) |

## Consequences

- **S1**: The app is written and not yet compiled, so its XCTest cases have never run. C3, C9, C10
  and C13 are owed on the app's side until `build.sh` passes them. The host's tests in
  `tests/test_daemon.py` pass.
- **S2**: `guarded` takes `/v2` and `/v2/events` by their own test on every method it serves, before
  any route runs: a loopback peer, one of the two Hosts and no other Origin. Any other method,
  `HEAD` and `OPTIONS` included, gets the standard library's 501 and nothing runs. `[::1]` is
  refused, since the server listens on IPv4 alone. A test pins each refusal. Loopback is not
  identity: every process on the machine is answered, a proxy forwarding another machine's request
  with a loopback Host included.
- **S3**: The app offers three things the host refuses whatever their body: Stop run under
  `control`, an image dropped on the message box under `message`, and the default model in Settings
  under `models`. Each leaves the app, or waits for a name of its own, before C3 holds. The help
  text of Run now promises a run whatever the rules say, while the host wakes only on what waits
  (C5).
- **S4**: The host lists no `provider_key` and serves no key route: `PUT` and `DELETE` on
  `/v2/library/providers/{name}/key` answer 404 before a body is read. A provider's `key` says
  whether the keychain item exists, asked without its value. The app hides its key field by itself,
  which has not run (S1).
- **S5**: An agent's `today.cost` is null while `unknown_today` in `hub/areas.py` is not 0, a day's
  row from before that count included, and so are `totals.cost` and an area's `today.cost` over it.
  A wake's `cost` is null when its line counts such calls. `totals.cap` is null when a resident's
  `chores.toml` does not read or `Budget.load` refuses its `[budget]`. A resident whose notes cannot
  be read still answers 0 for its day, beside its `error` state.
- **S6**: The compare before the rename is not a lock. A person's editor that saves between the two
  loses its save to the host's, and nothing reports it. A test saves the file after the host's read
  and before its compare, and gets 409 with the other writer's bytes left in place.
- **S7**: An id lasts while the registry lists the same directory in the same area. A second
  directory of the same name in that area changes the first one's id, and the app's links to the old
  id answer 404.
- **S8**: The host keeps the answers of the last 100 writes in memory, and a restart forgets them. A
  repeat with the same key and another body gets the first answer. The app mints a key per call, so
  a second press is a second write. For a message and an answer the key also goes to the mail as the
  send's idempotency key.
- **S9**: An answer is a send and then a mark. When the mark fails after the send, the host answers
  502 saying the text was sent, and the item stays open; a second answer from the app carries a new
  key and sends again. The app offers Done only on an item already read, and the host lists none, so
  it offers none; a done for an id not listed answers 404.
- **S10**: Nothing pins C13. The host's stream sends a wake only for a line its log gains while the
  stream is open, and an `agent` event only for a change it saw, so a reopened stream carries none
  of what it missed. A test that drops the stream across a wake and checks every page after the
  reopen is owed.
- **S11**: No scan of the tracked files under `apps/mac/` is in the tree, so C11 rests on review
  until one lands with a known-positive control. A recording taken from a live host carries the
  machine's name, since `daemon.host` is the host's name; the bundled one reads `127.0.0.1:8877`.
- **S12**: No test enforces C12; a person reads each screen's screenshot against it.
- **S13**: Plugins, grants and marketplaces, with the MCP client, are a later record. Until it lands
  the app shows the default plugin read only, as `GET /v2/plugins` answers it, and offers no
  install, switch, grant, marketplace or MCP server.
- **S14**: Execution boundaries and saved shell policies are a later record. Until it lands the app
  shows nothing for them: a definition's `shell_rules` is null, and the host serves no shell-rules
  route.
- **S15**: Providers, credentials and shared limits are a later record. Until it lands the app shows
  each provider read only: its state, whether a key is set, codex's release after a check and
  OpenRouter's usage figures. Once S4 lands it offers no key entry, and it offers no limits, which
  the host answers as null.
- **S16**: The subagent tool is a later record. Until it lands the app shows nothing for it.
- **S17**: The host is the areas server, and the residents run their own loops; the two servers of
  [[ADR-0021-the-served-actor|ADR-0021]] S2 still stand. A desk served through the actor, reached by
  the app through the host, changes D2's table and amends this record.
