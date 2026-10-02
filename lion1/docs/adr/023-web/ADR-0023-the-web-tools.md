---
adr: ADR-0023
status: proposed
liveness: operating (two commands, four provider adapters, the address check, the checked dial, and every request's bounds and deadline, save the steps S9 leaves out of it, are pinned by tests; the live provider tests are opt-in, S1)
date: "2026-10-02"
area: web
kind: new
depends_on:
  - "[[ADR-0003-the-bounds|ADR-0003]]"
  - "[[ADR-0005-command-handling|ADR-0005]]"
  - "[[ADR-0007-the-record|ADR-0007]]"
tags:
  - adr
  - hub
  - tools
---

# ADR-0023: The web tools

## Context

A model that cannot look anything up answers from what it was trained on. The hub's tools read and
change a directory and run commands in a box ([[ADR-0011-the-box|ADR-0011]]); none reaches the web.

Two commands close that: one asks a search service for links, one reads a page as text. Both hand
the model text written by strangers, and the second makes a request to an address the model chose. A
process that fetches any address a model names can be pointed at its own host, its private network
or a cloud metadata service. This record fixes what the two commands return, how they fail, and
which addresses a fetch may reach.

It does not make the process a network boundary. Where the process runs, and what its network lets
it reach, stay the operator's decisions (A1). Source: `hub/tools/web.py`, `hub/tools/__init__.py`,
`tests/test_web_tools.py`.

## Definitions

- **search provider**: a service that answers a query with a list of results; each has one adapter
  behind one protocol of one method.
- **hit**: one search result as the model reads it: a title, a link, a snippet cut at 1,000
  characters and, when the search provider gives one, a date.
- **carried address**: the IPv4 address held in the low 32 bits of a mapped, compatible, translated
  or NAT64 IPv6 address.
- **public address**: an address the standard library calls global and not multicast, judged after
  an IPv6 form is replaced by its carried address.
- **checked dial**: a connection made to one of the addresses a single resolution returned, after
  every address of that resolution was found to be a public address.

## Assumptions

| # | statement | source | if false |
| - | --------- | ------ | -------- |
| A1 | The process runs where reaching the open internet, and only that, is acceptable. The commands are not a boundary against the operator's own network. | the deployment | an egress proxy goes in front of the process; C6 names what the proxy then owns |
| A2 | The standard library's table of global addresses is right on the supported interpreters, 3.11 to 3.14. | the `ipaddress` documentation | the check carries its own table of ranges |
| A3 | Each search provider's API is what its reference said when read on 2026-10-02; the references are cited beside each adapter. | the provider references | the adapter raises on the unexpected body (C3) and is rewritten from the new reference |
| A4 | `Request.has_proxy()` and `Request._tunnel_host` say whether urllib's proxy handler took a request, on 3.11 to 3.14. | the CPython source of `urllib.request` | a request reads as direct: a private proxy's address is refused, the safe direction |

## Claims

### C1: Two read-only commands, bound to an actor only when asked for _(enforced: mechanical)_ ^c1

- **Subject**: `web(actor, provider, fetch=)` and the two handlers it registers.
- **Violated when**: an actor has a web command `web` did not register, either handler declares a
  class other than `read`, or the hub gains a dependency for them.

`web` registers `web_search` over one search provider, the given one or the first the environment
configures, and `web_fetch` unless `fetch` is false. Both declare `read`
([[ADR-0005-command-handling#^c6|ADR-0005/C6]]), so two of them in one turn overlap and neither
waits for the other. The HTTP is the standard library's, run in a thread.

### C2: What comes back is text to read, and the model is told so where it reads the command _(enforced: mechanical)_ ^c2

- **Subject**: the guidance line of each command, and every hit and page.
- **Violated when**: the first line of either command's guidance drops the warning, or a hit or a
  page reaches the model as anything but a command's result.

The model is told only the first line of a command's description, so that line says the result is
text and never instructions. A hit or a page enters the record as the RESULT of its command
([[ADR-0007-the-record#^c1|ADR-0007/C1]]) and nowhere else. The line is a frame, not a proof that a
model will not follow what a page says (S3).

### C3: Every failure is a failed result the model can read, and never carries the key _(enforced: mechanical)_ ^c3

- **Subject**: every search provider call and every fetch.
- **Violated when**: a refused key, a rate limit, a timeout, a network failure, a cut body or a body
  off the documented shape raises anything but `WebError` or ends the run; or the key reaches the
  model in an error or a hit.

Each of those is raised as `WebError` with the search provider's name or `web_fetch` first, which
the runtime settles as a failed command ([[ADR-0005-command-handling#^c1|ADR-0005/C1]]). A search
with no results is a result that says so.

A key is sent only in the header its search provider documents, and a search provider's redirect is
an error, never followed: it would carry that header to another host. A provider that echoes the
key, in an error, a result or the text of any failure, has it replaced by `[credential]` before
anything is read (S8).

### C4: An argument a search provider does not document is refused, never guessed _(enforced: mechanical)_ ^c4

- **Subject**: `site` and `recency` in each of the four adapters; `provider_from_env`.
- **Violated when**: an adapter sends a parameter its reference does not name, drops an argument
  silently, or the environment's choice depends on anything but the fixed order.

SearXNG has no site filter and no week range, and Jina has no recency: each says so in a failed
result that tells the model to search again without it. Brave documents the `site:` operator inside
the query and gets it there. A `site` is refused unless it is a bare domain: labels of ASCII
letters, digits and hyphens joined by dots, so an internationalized name is given in punycode. The
environment's choice is the first set of `SEARXNG_URL`, `BRAVE_API_KEY`, `TAVILY_API_KEY`,
`JINA_API_KEY`.

### C5: A fetch is made only to a public address, at every hop _(enforced: mechanical)_ ^c5

- **Subject**: the address a fetch is given and the address of every redirect it follows.
- **Violated when**: a scheme other than http or https is opened, a host with any answer that is not
  a public address is fetched, or an IPv6 form passes on a carried address that is not public.

The host is resolved and every answer must be a public address: one loopback, link-local, private,
shared, reserved or multicast answer refuses the host. A mapped (`::ffff:a.b.c.d`), compatible
(`::a.b.c.d`), translated (`::ffff:0:a.b.c.d`) or NAT64 (`64:ff9b::a.b.c.d`) form is judged by its
carried address, since the compatible, translated and NAT64 forms read as global whatever they
carry. Numeric spellings of a host are whatever the resolver makes of them, and that answer is the
one checked.

### C6: A fetch the proxy does not take dials an address that was checked _(enforced: mechanical)_ ^c6

- **Subject**: every connection a fetch opens, redirects included.
- **Violated when**: a socket dials a name or an answer that was not checked, TLS verifies a name
  other than the host asked for, or a second reading of the proxy settings changes the route.

A direct connection is a checked dial: the name is resolved once more at the connection, every
answer is checked again, and the socket connects to one of those addresses. Whether the proxy took a
request is read off the request the proxy handler changed, at the dial, so the settings are read
once. A request the proxy took is the proxy's to open: it resolves the name, and what it may reach
is the proxy's to restrict.

### C7: Every request ends by one deadline and reads a bounded body _(enforced: mechanical)_ ^c7

- **Subject**: every search provider call and every fetch, redirects included.
- **Violated when**: a request outlives its deadline by more than one socket operation; more than
  2,000,001 bytes of a body are read; a fetch returns a body that is not a text type, or page text
  past `max_chars`; or a cut is not marked.

A request has one deadline of 20 seconds, from its first connection to the last byte read, however
the bytes arrive: a peer that sends a byte now and then is cut at the deadline as one that sends
nothing. A provider's answer past 2,000,000 bytes is refused. A fetch reads text types only, at most
2,000,001 bytes whatever `max_chars` asks.

HTML becomes text without scripts, styles or markup. The page's text is cut at `max_chars`, at most
100,000, and a line after it says where; that line is not counted. The run's own bounds apply as to
any command ([[ADR-0003-the-bounds|ADR-0003]]).

## Decisions

### D1: `hub/tools/web.py`: `WebSearch`, `WebFetch`, `Hit` and the installer `web` ^d1

Serves C1 and C2. The two request models carry the warning in the first line of their docstrings,
which is the line the generated guidance shows. `web` registers both handlers with `effect="read"`
and returns the names it registered. `hub/tools/__init__.py` exports the models, the adapters,
`provider_from_env`, `WebError` and `web`.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_web_registers_two_read_commands_named_from_their_classes`,
  `test_the_model_is_told_the_web_is_material_not_instructions`,
  `test_a_scripted_run_searches_and_reads_the_hits`.

### D2: `_send`, `_credentials` and `_failure`: one request, and every way it fails as `WebError` ^d2

Serves C3 and C7. `_send` opens one request with redirects refused, reads one byte past the cap, and
turns an HTTP error, a timeout in either of its forms, a network failure and a cut body into
`WebError` naming the search provider. `_credentials` takes the key from the request's own
credential header and `_blank` replaces it in the body and an error's first 300 bytes, `_failure` in
an exception's text. `_json` refuses a body that is not a JSON object, and `_items` a result list
that is not a list.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_a_bad_key_and_a_rate_limit_are_errors_the_model_reads`,
  `test_a_provider_that_echoes_the_key_has_it_blanked_in_errors_and_hits`,
  `test_a_failure_whose_text_carries_the_key_has_it_blanked`,
  `test_a_provider_answer_past_the_cap_is_refused_and_never_read_whole`,
  `test_a_body_that_is_not_json_or_not_the_documented_shape`,
  `test_a_provider_redirect_is_an_error_and_never_followed`.

### D3: `SearXNG`, `Brave`, `Tavily`, `Jina` and `provider_from_env` ^d3

Serves C4. Each adapter has `request`, which builds the one request its reference describes, and
`parse`, which reads the documented fields into hits. A result with no link is skipped; a section or
a result of another type than its reference names raises (C3). The reference and the date it was
read sit in a comment above each class. An unsupported argument raises before any request is made.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_searxng_has_no_week_and_no_site_and_says_so`, `test_jina_has_no_recency_and_says_so`,
  `test_brave_builds_its_request_and_parses_its_hits`,
  `test_tavily_sends_only_the_filters_it_was_given`, `test_a_site_that_is_not_a_domain_is_refused`,
  `test_provider_from_env_takes_the_first_configured_in_order`.

### D4: `_check_url`, `_public_addresses`, `_carried_v4` and the redirect handler ^d4

Serves C5. `_check_url` takes the scheme, the host and the port from the address and calls
`_public_addresses`, which resolves the host and refuses on the first answer that is not a public
address. `_carried_v4` returns the low 32 bits of a mapped, compatible, `::ffff:0:0:0/96` or
`64:ff9b::/96` address. `_Redirects` runs `_check_url` on each redirect's target before it is
followed.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_fetch_refuses_loopback_link_local_and_private_addresses`,
  `test_a_v6_form_that_carries_a_public_v4_address_is_judged_by_that_address`,
  `test_fetch_refuses_a_name_that_resolves_to_a_private_address`,
  `test_fetch_refuses_other_schemes_and_hosts_that_do_not_resolve`,
  `test_a_redirect_is_followed_only_to_a_public_address`.

### D5: `_connect_public`, the two pinned connections and `_via_proxy` ^d5

Serves C6. `_connect_public` resolves, checks and connects to an address, never to the name.
`_Pinned` and `_PinnedTLS` use it as their `connect`; the TLS one verifies the host asked for
against the default trust store. The two handlers are always installed for a fetch and pick the
pinned connection unless `_via_proxy` reads the request as taken by the proxy.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_a_name_whose_answer_changes_after_the_check_is_refused_at_the_connection`,
  `test_a_direct_fetch_follows_a_redirect_through_checked_connections`,
  `test_the_tls_connection_verifies_the_name_it_was_asked_for`,
  `test_proxy_settings_that_change_between_two_readings_open_no_unchecked_connection`,
  `test_behind_an_environment_proxy_the_request_is_the_proxys_to_open`.

### D6: `_fetch` and `html_text` ^d6

Serves C7. `_fetch` checks the address, opens it with the guarded opener, refuses a content type
that is not text, reads one byte past the cap to know a cut happened, decodes with the declared
charset or UTF-8, and cuts at `max_chars` with a marker. `html_text` drops script, style, noscript,
template and svg content. An end tag closes only its own element. Script, style and noscript content
is raw text to its end tag, `<script/>` and `<noscript/>` included, as HTML reads it; only svg
closes itself.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_fetch_caps_what_it_reads_whatever_max_chars_asks`, `test_fetch_caps_the_text_at_max_chars`,
  `test_fetch_refuses_what_is_not_text`, `test_html_becomes_text_without_scripts_styles_or_markup`,
  `test_a_self_closed_script_or_a_stray_end_tag_leaves_nothing_hidden_on_the_page`,
  `test_a_tag_spelled_inside_a_noscript_opens_nothing_so_its_end_tag_ends_it`.

### D7: `_Clock`: the deadline, held by the thread that makes the request ^d7

Serves C7. `_send` and `_fetch` each open a `_Clock` for the request. Every connection, pinned or
timed, hands its socket to the clock, and an SSL socket before its handshake. When the deadline
passes a timer shuts each socket, which ends a read blocked on it, and `_left` refuses a later
connection, a checked dial whose resolution outlasted the deadline included. A request whose clock
expired is a timeout whatever its read returned. `_Timed` and `_TimedTLS` carry provider and proxy
connections.

- **Landing evidence**: `tests/test_web_tools.py`:
  `test_a_provider_that_sends_a_byte_now_and_then_is_cut_at_the_deadline`,
  `test_a_page_that_sends_a_byte_now_and_then_is_cut_at_the_deadline` (in the body and in the
  headers), `test_past_the_deadline_no_socket_is_opened_and_one_watched_late_is_shut`.

## Alternatives

| approach | rejected because |
| -------- | ---------------- |
| An HTTP client dependency | the hub takes none for two kinds of GET and one POST, and pinning the dial needs the connection class either way |
| Check the name, then let the client connect by name | the name can answer differently the second time; the checked dial resolves and connects in one place (C6) |
| Choose the transport from the proxy settings before opening | the proxy handler reads them again; a change between the two readings sent a request by name on an unchecked connection (C6) |
| Refuse to fetch behind a proxy | a host that reaches the internet only through its proxy could not fetch at all; the proxy is the operator's boundary (A1) |
| Translate an unsupported argument into something close | a week read as a month, or a site folded into a query the service does not parse, returns results the model did not ask for (C4) |
| A search provider's own page reader for the fetch | every address the model opens goes to a third party and needs a key; the direct fetch needs neither |
| A headless browser | a dependency and a process per page; pages that need scripts are left out (S4) |

## Consequences

- **S1**: D1 to D7 landed with the tests they name. Each of these turned named tests red: removing
  the pinned connection, dialing by name, ignoring the carried address, and choosing the transport
  from a first reading of the proxy settings. So did a clock that watches no socket, an expired
  clock that loses to a refusal, a key left in place, a whole read, and each of the two parser
  rules. The four live tests, one per search provider, run only when that provider's variable is
  set.
- **S2**: These are the first handlers under `hub/` to declare an effect class.
  [[ADR-0005-command-handling|ADR-0005]]'s liveness line and its S9 said none did; both are edited
  in the same change.
- **S3**: A public host may serve anything, and a page may tell the model to do something. C2's line
  is a frame. What a model does with a page is bounded by the commands its profile holds, not by
  this record.
- **S4**: Not read: pages that need scripts, PDFs and other binary types, `robots.txt`. There is no
  cache and no rate limit beyond the run's bounds.
- **S5**: NAT64 is recognised by its well-known prefix only. A network that translates under a
  prefix of its own gives a global IPv6 address this check cannot tell from any other; that network
  is the operator's (A1).
- **S6**: Behind a proxy the address is still resolved here for C5's check. A host that can resolve
  outside names only through its proxy refuses every fetch as unresolvable, and says so.
- **S7**: A redirect between http and https is followed. A fetch sends no credential, so the change
  exposes the address asked for and nothing else.
- **S8**: The key is blanked as its exact bytes. An answer that escapes or encodes it, and a key
  under 8 characters, are not caught; a provider that reflects its own key back is the provider's
  defect, and this is a backstop.
- **S9**: Not under the deadline: name resolution, which the system's resolver bounds, and the TLS
  handshake with a search provider or a proxy, which the socket's timeout bounds per operation. A
  page's handshake is under it.
- **S10**: The pinned connections replace the standard `connect` and raise its audit event,
  `http.client.connect`, themselves; they do not set `TCP_NODELAY`, which changes latency only.
