# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""V0 behavior-preservation gate for the consolidation manifest.

Compares a fresh capture of live public surfaces (HTTP routes, OpenAPI, CLI
parser tree, MCP projections, machine-mode output, public imports) against
the frozen baseline in ``tests/contracts/data/*.json``. A delta means an
unintended behavior change (fix the code) or an intended one (update the
baseline per the recorded procedure). See docs/internals/contracts.md for
the full protocol, including the host-volatility redaction waiver.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import warnings
from pathlib import Path

import pytest

from tests.contracts import _capture

DATA_DIR = Path(__file__).parent / "data"

# The ONLY way a case's literal captured stdout/stderr may be committed here:
# each entry states why that argv's output is safe (static argparse text,
# no session/host/run state). A case with no entry is untrusted by default
# and redacted (see docs/internals/contracts.md and
# test_new_case_defaults_closed_without_declaration).
_COMMITTABLE_SPECIALIZED_ARGV: dict[tuple[str, ...], str] = {
    ("--help",): "top-level argparse usage/help text",
    ("wait",): "argparse usage + required-argument error",
    ("monitor", "run"): "argparse usage + required-argument error",
    ("bogus-unknown-command",): "argparse invalid-choice error, static command list",
    ("play",): "static usage line, no NAME resolved yet",
    ("play", "--help"): "static usage/flag text, no NAME resolved yet",
    ("o", "flow", "--help"): "argparse usage/help text",
    ("o", "fanout", "--help"): "argparse usage/help text",
    ("o", "flow"): "static required-prompt error",
    ("o", "fanout"): "static required-prompt error",
    ("schedule", "--help"): "argparse usage/help text",
    ("schedule",): "argparse required-subparser error",
    ("schedule", "list", "--bogus"): "argparse unrecognized-argument error",
    ("schedule", "create", "capture-test", "--every", "15m"): (
        "argparse did-you-mean error, static synonym text"
    ),
    ("schedule", "create", "agent", "capture-test"): ("argparse usage + required-argument error"),
    ("schedule", "create", "command", "capture-test", "--every", "15m"): (
        "static validation error, no host state"
    ),
}
_COMMITTABLE_MACHINE_ARGV: dict[tuple[str, ...], str] = {
    ("lifecycle", "--machine"): "static machine-envelope error, no live run/host state",
    ("bogus-unknown-command", "--machine"): (
        "argparse invalid-choice error inside the machine envelope"
    ),
    ("--machine",): "top-level machine-mode usage error",
}


def _volatile_argv_for(file_name: str) -> set[tuple[str, ...]]:
    """Argv captured for *file_name* not declared committable above --
    derived from the live case population, not a hand-maintained denylist."""
    if file_name == "specialized":
        return set(_capture.SPECIALIZED_CASES) - set(_COMMITTABLE_SPECIALIZED_ARGV)
    if file_name == "machine":
        return set(_capture.MACHINE_CASES) - set(_COMMITTABLE_MACHINE_ARGV)
    return set()


def _load(name: str):
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


def _sorted_json(value):
    return json.dumps(value, indent=2, sort_keys=True)


def test_http_route_count_is_143():
    live = _capture.capture_http()
    assert live["count"] == 143


def _routes_by_key(payload: dict) -> dict[str, dict]:
    """Key routes by ``(methods, path)`` identity, dropping ``ordinal``.

    ``ordinal`` reflects decorator-registration order in this process, which
    varies with which test imported a service module first (many tests
    import a single ``lionagi.studio.services.*`` module directly, bypassing
    ``create_app()``). Comparing it positionally turns one incidental
    reordering into a whole-file diff; keying by identity instead means only
    an actual added/removed/changed route diffs.
    """
    by_key: dict[str, dict] = {}
    for route in payload["routes"]:
        key = json.dumps([route["methods"], route["path"]], sort_keys=True)
        by_key[key] = {k: v for k, v in route.items() if k != "ordinal"}
    return by_key


def _openapi_by_shape(payload: dict) -> dict:
    """Openapi block with ``info.version`` dropped -- it's the app's build
    version, bumped on every release regardless of route changes, and would
    otherwise make every release a false behavior-preservation failure here.
    Same reasoning as ``ordinal`` above: drop the field that is not a fact
    about the routes.
    """
    openapi = payload["openapi"]
    info = {k: v for k, v in openapi.get("info", {}).items() if k != "version"}
    return {**openapi, "info": info}


def test_http_routes_match_baseline():
    """http.json is generated from this branch's own base commit and is
    identical (per route, keyed by (methods, path) -- see _routes_by_key) to
    the live capture — the strongest available behavior-preservation proof.
    Regenerate it (and this test's expected count above) only when an
    intentional route change lands, never to paper over an unreviewed drift.
    """
    expected = _load("http")
    live = _capture.capture_http()
    assert live["count"] == expected["count"]
    assert _sorted_json(_routes_by_key(live)) == _sorted_json(_routes_by_key(expected))
    assert _sorted_json(_openapi_by_shape(live)) == _sorted_json(_openapi_by_shape(expected))


def test_http_all_routes_have_responses_field():
    live = _capture.capture_http()
    for route in live["routes"]:
        assert "responses" in route, f"route {route['ordinal']} {route['path']} missing responses"


def test_http_api_routes_have_nonempty_responses():
    live = _capture.capture_http()
    api_routes = [r for r in live["routes"] if r["path"] and r["path"].startswith("/api/")]
    assert api_routes, "expected at least one /api/ business route"
    missing = [r["path"] for r in api_routes if not r["responses"]]
    assert not missing, f"business routes missing responses: {missing[:5]}"


def test_http_openapi_has_full_operations_and_schemas():
    live = _capture.capture_http()
    openapi = live["openapi"]
    assert openapi["path_count"] > 0
    assert openapi["schema_count"] > 0
    _, sample_ops = next(iter(openapi["paths"].items()))
    sample_op = next(iter(sample_ops.values()))
    # Full operation content, not just a path-name list.
    assert "responses" in sample_op
    # operationId is deliberately excluded: derives from the handler's
    # Python qualname, which moves when a handler is absorbed into a new
    # module — an internal migration detail, not an external route field.
    assert "operationId" not in sample_op
    sample_schema = next(iter(openapi["schemas"].values()))
    assert isinstance(sample_schema, dict) and sample_schema, "expected full schema definitions"


def test_cli_registry_has_21_commands():
    live = _capture.capture_cli()
    assert live["registry_count"] == 21
    assert live["name_map_count"] == 23


def test_cli_surface_matches_baseline():
    expected = _load("cli")
    live = _capture.capture_cli()
    assert _sorted_json(live) == _sorted_json(expected)


def _assert_stderr_matches_baseline(
    argv: tuple[str, ...], got_stderr: str, exp_stderr: str
) -> None:
    """Compare stderr against baseline, tolerating only the argparse
    choice-quoting rendering difference (``_capture.normalize_argparse_choice_quoting``)
    -- a changed/added/removed/renamed choice still fails byte-for-byte.
    Warns (rather than passing silently) whenever normalization was load-bearing,
    so the warnings summary shows every case that needed it.
    """
    if got_stderr == exp_stderr:
        return
    norm_got = _capture.normalize_argparse_choice_quoting(got_stderr)
    norm_exp = _capture.normalize_argparse_choice_quoting(exp_stderr)
    assert norm_got == norm_exp, f"stderr changed for {argv}"
    warnings.warn(
        f"{argv}: stderr differed from the frozen baseline only in argparse "
        "choice-name quoting -- normalized before comparing.\n"
        f"  baseline: {exp_stderr!r}\n  live:     {got_stderr!r}",
        stacklevel=2,
    )


def test_cli_specialized_paths_match_baseline():
    """Committable cases capture argparse's own rendered text verbatim, which
    CPython's HelpFormatter changed across this project's CI-tested Python
    versions (see _capture.specialized_baseline_name); the baseline loaded
    here is pinned to the running interpreter rather than a single file."""
    expected = {tuple(c["argv"]): c for c in _load(_capture.specialized_baseline_name())}
    live = {tuple(c["argv"]): c for c in _capture.capture_specialized()}
    # Every frozen case must still be present and, modulo known volatility
    # (W-02, below), unchanged.
    assert set(expected) <= set(live), f"missing from live: {set(expected) - set(live)}"
    volatile = _volatile_argv_for("specialized")
    for argv, exp in expected.items():
        got = live[argv]
        if argv in volatile:
            continue
        assert got["exit_code"] == exp["exit_code"], f"exit code changed for {argv}"
        assert got["stdout"] == exp["stdout"], f"stdout changed for {argv}"
        _assert_stderr_matches_baseline(argv, got["stderr"], exp["stderr"])


def test_normalize_argparse_choice_quoting_is_quoting_agnostic():
    """Property 1: the same choice list, quoted or bare, normalizes to the
    same fragment. Uses this project's own frozen baselines rather than a
    synthetic example: ``specialized.json`` (3.10, quoted) and
    ``specialized_py312.json`` (3.12, bare) are, for
    ``bogus-unknown-command``, identical in every other respect -- both
    predate the 3.13 usage-line-wrap/metavar-collapse change, unlike
    specialized_py314.json, which would conflate two unrelated rendering
    changes -- so this pair isolates exactly the quoting difference the
    normalizer targets. Also why the version map collapses (3, 10)-(3, 12)
    onto one baseline file above."""
    quoted = {tuple(c["argv"]): c for c in _load("specialized")}[("bogus-unknown-command",)][
        "stderr"
    ]
    bare = {tuple(c["argv"]): c for c in _load("specialized_py312")}[("bogus-unknown-command",)][
        "stderr"
    ]
    assert quoted != bare, "fixture no longer exercises the quoting difference"
    assert _capture.normalize_argparse_choice_quoting(
        quoted
    ) == _capture.normalize_argparse_choice_quoting(bare)


def test_normalize_argparse_choice_quoting_still_flags_a_renamed_choice():
    """Property 2 (mutation): renaming one choice name inside the baseline's
    own "(choose from ...)" fragment must still fail after normalization --
    proving the normalizer discards only quoting, never the choice list
    itself. Expected red declared up front: the normalized mutant must NOT
    equal the normalized original."""
    original = {tuple(c["argv"]): c for c in _load("specialized")}[("bogus-unknown-command",)][
        "stderr"
    ]
    assert "'orchestrate'" in original and "'orchestrated'" not in original
    mutant = original.replace("'orchestrate'", "'orchestrated'", 1)
    # Verify the mutation actually applied before trusting anything downstream.
    assert mutant != original
    assert "'orchestrated'" in mutant
    assert _capture.normalize_argparse_choice_quoting(
        original
    ) != _capture.normalize_argparse_choice_quoting(mutant), (
        "normalization swallowed a renamed subcommand choice -- this must fail"
    )


def test_stderr_baseline_comparison_still_fails_on_a_renamed_choice():
    """Same mutation as above, run through the actual comparison helper
    (``_assert_stderr_matches_baseline``) that
    test_cli_specialized_paths_match_baseline uses -- not just the
    normalizer in isolation -- so a normalizer wired in slightly wrong (e.g.
    bypassing the choice-name check) cannot pass silently. Expected red
    declared up front via ``pytest.raises``."""
    original = {tuple(c["argv"]): c for c in _load("specialized")}[("bogus-unknown-command",)][
        "stderr"
    ]
    mutant = original.replace("'orchestrate'", "'orchestrated'", 1)
    assert mutant != original and "'orchestrated'" in mutant
    with pytest.raises(AssertionError, match="stderr changed"):
        _assert_stderr_matches_baseline(("bogus-unknown-command",), mutant, original)


def test_specialized_baseline_covers_ci_matrix():
    """Every Python minor the full-matrix CI job runs (_capture._CI_MATRIX_PYVERS,
    mirroring .github/workflows/ci.yml:174) must have a pinned specialized-CLI
    baseline entry. PRs only exercise the oldest+newest of that matrix, so a
    missing entry for any interpreter in between is invisible to every check
    a PR can run and only breaks main once a push runs the full matrix."""
    missing = [
        v for v in _capture._CI_MATRIX_PYVERS if v not in _capture._SPECIALIZED_BASELINE_BY_PYVER
    ]
    assert not missing, f"no pinned specialized-CLI baseline for CI-matrix Python(s): {missing}"


def test_specialized_baseline_name_resolves_for_known_interpreter(monkeypatch):
    """Mutation arm (a): a Python minor present in the mapping resolves to
    its pinned baseline name instead of raising."""
    monkeypatch.setattr(_capture.sys, "version_info", (3, 10, 99, "final", 0))
    assert _capture.specialized_baseline_name() == "specialized"


def test_specialized_baseline_name_raises_for_unknown_interpreter(monkeypatch):
    """Mutation arm (b): a Python minor absent from the mapping raises
    RuntimeError rather than silently falling back -- this is what makes a
    matrix gap a guaranteed failure instead of a silent breakage on main."""
    monkeypatch.setattr(_capture.sys, "version_info", (3, 99, 0, "final", 0))
    with pytest.raises(RuntimeError, match="no pinned specialized-CLI baseline"):
        _capture.specialized_baseline_name()


def test_mcp_available_paths_match_baseline():
    expected = _load("mcp")
    live = _capture.capture_mcp()
    assert live["available_paths"] == expected["available_paths"]
    assert live["available_path_count"] == expected["available_path_count"]


def test_mcp_catalog_matches_baseline():
    expected = _load("mcp")
    live = _capture.capture_mcp()
    assert live["catalog"] == expected["catalog"]


def test_mcp_catalog_entry_shapes_are_locked_by_hand():
    """Second, independent lock on the shape of a catalog entry.

    Every caller reads this listing, so a field added to or dropped from all 70
    entries changes what the whole surface says while leaving verb names and
    counts identical. Hand-typed rather than derived from the JSON, so the
    baseline and this test have to be wrong the same way to pass together.
    """
    live = _capture.capture_mcp()
    shapes = {tuple(keys): count for keys, count in live["catalog"]["entry_key_sets"]}
    # The complete mapping, not a sample of it: asserting two shapes leaves the
    # other four free to change under a refreshed baseline without anything
    # here noticing.
    assert shapes == {
        # a deliberately unavailable verb says so and routes the caller; its
        # reason is one targeted help call away, not in every read of the listing
        ("available", "cli_path", "summary", "verb"): 26,
        # a runnable verb with required parameters names them and nothing else
        ("required", "summary", "verb"): 24,
        # a runnable verb with no required parameters carries neither key
        ("summary", "verb"): 16,
        # spawn verbs additionally publish what their fingerprint covers
        ("required_unenforced", "schema_fingerprint", "summary", "verb"): 2,
        ("required", "required_unenforced", "schema_fingerprint_varies_with", "summary", "verb"): 1,
        (
            "required_unenforced",
            "schema_fingerprint",
            "schema_fingerprint_varies_with",
            "summary",
            "verb",
        ): 1,
    }
    assert sum(shapes.values()) == 70
    # No shape pairs cli_path with an inline reason. A verb whose schema failed
    # to build is the one entry that does carry a reason inline, and it has no
    # cli_path — that path reports a defect in this server and must stay
    # readable without a second call, so it is not asserted away here.
    assert not any("cli_path" in keys and "reason" in keys for keys in shapes)


def test_mcp_projections_match_baseline():
    expected = _load("mcp")
    live = _capture.capture_mcp()
    assert live["projections"] == expected["projections"]
    assert live["projection_errors"] == expected["projection_errors"]
    assert live["errors"] == expected["errors"]


def test_mcp_projects_every_available_path():
    live = _capture.capture_mcp()
    assert live["projection_count"] + live["projection_error_count"] == live["available_path_count"]
    assert live["available_path_count"] == 75
    assert live["projection_count"] == 62
    assert live["projection_error_count"] == 13


def test_mcp_projection_errors_are_classified():
    live = _capture.capture_mcp()
    valid_classes = {
        "unresolved_subcommand",
        "unsupported_argparse_type",
        "empty_command_path",
        "no_such_command",
        "no_such_command_path",
        "other",
    }
    for path, err in live["projection_errors"].items():
        assert err["class"] in valid_classes, f"{path}: unclassified error {err}"
        assert err["class"] != "other", f"{path}: fell through to 'other' — {err}"
    classes = {err["class"] for err in live["projection_errors"].values()}
    assert "unresolved_subcommand" in classes
    # The one known unsupported-argparse-type case (`mirror --since`).
    assert "unsupported_argparse_type" in classes
    assert live["projection_errors"]["mirror"]["class"] == "unsupported_argparse_type"


def test_mcp_aliases_derived_from_live_seed_table():
    live = _capture.capture_mcp()
    assert live["projections"]["orchestrate flow"]["aliases"] == ["o flow"]
    assert live["projections"]["monitor"]["aliases"] == ["mon"]
    # A path with no aliased head carries no aliases key at all.
    assert "aliases" not in live["projections"]["agent"]


def test_mcp_absent_verbs_are_captured_in_full():
    live = _capture.capture_mcp()
    assert live["absent_verb_count"] == 26
    by_name = {v["name"]: v for v in live["absent_verbs"]}
    assert "mirror" in by_name
    assert "casts" in by_name
    for entry in live["absent_verbs"]:
        assert entry["summary"]
        assert entry["reason"]
        assert entry["cli_path"]


def test_mcp_negative_cases_cover_every_error_class():
    live = _capture.capture_mcp()
    classes = {e["class"] for e in live["errors"]}
    assert {
        "empty_command_path",
        "no_such_command",
        "no_such_command_path",
        "unresolved_subcommand",
        "unsupported_argparse_type",
    } <= classes


def test_machine_classification_matches_baseline():
    expected = {tuple(c["argv"]): c for c in _load("machine")}
    live = {tuple(c["argv"]): c for c in _capture.capture_machine()}
    assert set(live) == set(expected)
    volatile = _volatile_argv_for("machine")
    for argv, exp in expected.items():
        got = live[argv]
        assert got["exit_code"] == exp["exit_code"], f"exit code changed for {argv}"
        assert got["envelope_ok"] == exp["envelope_ok"], f"envelope ok changed for {argv}"
        if argv in volatile:
            continue
        assert got["stdout"] == exp["stdout"], f"stdout changed for {argv}"


def test_public_imports_match_baseline():
    expected = _load("imports")
    live = _capture.capture_imports()
    assert live["root_all"] == expected["root_all"]
    assert live["root_all_count"] == expected["root_all_count"] == 61
    assert live["symbols"] == expected["symbols"]
    assert live["lazy_map_keys"] == expected["lazy_map_keys"]
    assert live["compat_modules"] == expected["compat_modules"]
    # `import_laziness` is new this round (see test_import_laziness_* below);
    # imports.json's four existing keys above are unaffected by its addition.


# Exit codes for the play / orchestrate-flow / orchestrate-fanout / schedule
# quick-create and did-you-mean specialized-CLI cases, asserted directly as a
# structural check independent of the byte-for-byte fixture comparison above.
_NEW_SPECIALIZED_EXPECTED_EXIT: dict[tuple[str, ...], int] = {
    ("play",): 1,
    ("play", "list"): 0,
    ("play", "nonexistent"): 1,
    ("play", "--help"): 1,
    ("o", "flow", "--help"): 0,
    ("o", "fanout", "--help"): 0,
    ("o", "flow"): 1,
    ("o", "fanout"): 1,
    ("schedule", "--help"): 0,
    ("schedule",): 2,
    ("schedule", "list", "--bogus"): 2,
    ("schedule", "create", "capture-test", "--every", "15m"): 2,
    ("schedule", "create", "agent", "capture-test"): 2,
    ("schedule", "create", "command", "capture-test", "--every", "15m"): 1,
}


def test_specialized_new_branches_have_expected_exit_codes():
    live = {tuple(c["argv"]): c for c in _capture.capture_specialized()}
    for argv, expected_exit in _NEW_SPECIALIZED_EXPECTED_EXIT.items():
        assert argv in live, f"{argv} missing from capture_specialized() output"
        got_exit = live[argv]["exit_code"]
        assert got_exit == expected_exit, (
            f"{argv} exit code {got_exit} != expected {expected_exit}\n"
            f"stdout={live[argv]['stdout']!r}\nstderr={live[argv]['stderr']!r}"
        )


def test_schedule_did_you_mean_suggests_synonym():
    live = {tuple(c["argv"]): c for c in _capture.capture_specialized()}
    case = live[("schedule", "create", "capture-test", "--every", "15m")]
    assert "did you mean '--interval'?" in case["stderr"]


def test_schedule_quick_create_validates_before_any_network_call():
    """Both quick-create negative cases must fail through argparse/local
    validation alone — no DB or HTTP state may be created by this contract
    test. Asserted implicitly: both stderr messages name the missing
    argument/flag rather than any connection or server error."""
    live = {tuple(c["argv"]): c for c in _capture.capture_specialized()}
    agent_case = live[("schedule", "create", "agent", "capture-test")]
    assert "--profile" in agent_case["stderr"]
    command_case = live[("schedule", "create", "command", "capture-test", "--every", "15m")]
    assert "trailing" in command_case["stderr"]


# Cross-seed imports that are known, source-grounded, and pre-existing —
# unrelated to this module's own registry/dispatch consolidation, so the
# import-laziness oracle allowlists exactly these two instead of asserting a
# blanket zero that would misreport them as regressions:
#   - orchestrate: lionagi/cli/orchestrate/_common.py:14 does a *module-level*
#     `from .. import team as _team_module` (team-mode support), so importing
#     lionagi.cli.orchestrate always pulls in lionagi.cli.team.
#   - stats: lionagi/cli/stats.py:14 does a *module-level*
#     `from .monitor import _since_timestamp`, reusing a helper function.
_KNOWN_CROSS_SEED_IMPORTS: dict[str, tuple[str, ...]] = {
    "orchestrate": ("lionagi.cli.team",),
    "stats": ("lionagi.cli.monitor",),
}


def test_import_laziness_traces_all_21_seeds_cleanly():
    live = _capture.capture_imports()
    trace = live["import_laziness"]
    assert trace["seed_count"] == 21
    assert len(trace["seed_names"]) == 21
    for name, result in trace["traces"].items():
        assert not result.get("_error"), f"{name}: subprocess trace failed: {result.get('_error')}"
        assert not result["http_registry_realized"], (
            f"{name}: loading this CLI seed realized the HTTP registry "
            f"(count={result['http_registry_count']})"
        )
        assert result["cli_realized_names"] == [name], (
            f"{name}: expected only itself in _cli_realized, got {result['cli_realized_names']}"
        )
        allowed = set(_KNOWN_CROSS_SEED_IMPORTS.get(name, ()))
        leaked = set(result["other_seed_modules_imported"])
        unexpected = leaked - allowed
        assert not unexpected, f"{name}: unexpected cross-seed imports {sorted(unexpected)}"


def test_import_laziness_casts_seed_stays_cli_only():
    """The one seed whose module declares both a CLI and an HTTP marker
    (lionagi/casts/surfaces.py) — loading it as a CLI seed must not eagerly
    realize the HTTP registry as a side effect, the risk a single module
    serving both surfaces creates."""
    live = _capture.capture_imports()
    casts = live["import_laziness"]["traces"]["casts"]
    assert not casts.get("_error")
    assert casts["cli_realized_names"] == ["casts"]
    assert not casts["http_registry_realized"]
    assert casts["other_seed_modules_imported"] == []


@pytest.mark.parametrize("case", _capture.MACHINE_CASES, ids=lambda c: " ".join(c))
def test_machine_envelope_shape_is_well_formed(case):
    result = _capture._run_cli(list(case))
    stdout = result["stdout"].strip()
    if not stdout:
        return
    envelope = json.loads(stdout)
    assert "ok" in envelope
    assert "contract_version" in envelope


# Contract fixtures are committed to a public repository and are compared
# byte-for-byte, so a captured value that varies per machine breaks both at
# once: it publishes whatever the capturing developer's home directory held,
# and it makes the suite pass only on that machine. Cases whose output is
# genuinely host-dependent are excluded from comparison above and their stdout
# is redacted rather than committed. This guards both properties at once.
_HOST_STATE_PATTERNS = (
    "/Users/",
    "/home/",
    "khive-work",
)


def test_fixtures_carry_no_host_specific_state():
    offenders = []
    for path in sorted(DATA_DIR.glob("*.json")):
        text = path.read_text()
        for pattern in _HOST_STATE_PATTERNS:
            if pattern in text:
                line = next((i for i, ln in enumerate(text.splitlines(), 1) if pattern in ln), None)
                offenders.append(f"{path.name}:{line} contains {pattern!r}")
    # Positive control: the check can see a planted value, so an empty result
    # means "no host state" rather than "the search was broken".
    assert any(p in "/Users/someone/x" for p in _HOST_STATE_PATTERNS)
    assert not offenders, (
        "contract fixtures must not carry host-specific state (see the note above): "
        + "; ".join(offenders)
    )


# `test_fixtures_carry_no_host_specific_state` above only catches leaks
# matching a few path patterns -- it says nothing about host state leaking
# through some other shape (a session id, a library version, a directory
# listing). The check below instead requires every case in the volatile
# population (_volatile_argv_for) to carry either an empty stream or the
# redaction marker, never literal captured text -- no currently-visible leak
# is an exemption, since pinning volatile bytes has no oracle value.
_REDACTION_MARKER_RE = re.compile(r"^\[redacted: .+\]$", re.DOTALL)


def _unredacted_fields(file_name: str, cases: list, *, label: str | None = None) -> list[str]:
    """(file, argv, field) labels for every case classified volatile for
    *file_name* whose captured stream is neither empty nor the redaction
    marker -- i.e. still carries literal captured output. *file_name* selects
    the volatile-case population (SPECIALIZED_CASES / MACHINE_CASES); *label*
    overrides the fixture name in the offender message when *cases* came from
    a differently-named file (e.g. a per-Python-version pinned baseline)."""
    volatile = _volatile_argv_for(file_name)
    label = label or file_name
    offenders = []
    for case in cases:
        argv = tuple(case["argv"])
        if argv not in volatile:
            continue
        for field in ("stdout", "stderr"):
            value = case.get(field, "")
            if value and not _REDACTION_MARKER_RE.match(value):
                offenders.append(f"{label}.json {argv} {field}")
    return offenders


def test_volatile_fixture_cases_are_fully_redacted():
    offenders = []
    for name in _capture._SPECIALIZED_BASELINE_BY_PYVER.values():
        offenders += _unredacted_fields("specialized", _load(name), label=name)
    offenders += _unredacted_fields("machine", _load("machine"))
    assert not offenders, (
        "volatile fixture cases still carry literal captured output instead of the "
        "redaction marker: " + "; ".join(offenders)
    )


def test_redaction_check_flags_unredacted_volatile_stdout():
    """Mutation arm (a): a disposable copy with a volatile case's stdout put
    back to literal text must turn the population check red, naming the case."""
    argv = ("agent", "status")
    cases = [{"argv": list(argv), "stdout": "SESSION deadbeef-...", "stderr": "[redacted: ok]"}]
    assert _unredacted_fields("specialized", cases) == [f"specialized.json {argv} stdout"]


def test_redaction_check_flags_unredacted_volatile_stderr():
    """Mutation arm (b): same as (a) but for stderr -- this is the arm that
    matters, since the original defect was a stderr leak the stdout-only
    remedy would not have caught."""
    argv = ("agent", "status")
    cases = [{"argv": list(argv), "stdout": "[redacted: ok]", "stderr": "Linked SQLite 3.46.0 ..."}]
    assert _unredacted_fields("specialized", cases) == [f"specialized.json {argv} stderr"]


def test_new_case_defaults_closed_without_declaration(monkeypatch):
    """Mutation arm (a): a brand-new case appended to the LIVE capture set
    (_capture.SPECIALIZED_CASES) -- with no entry added to
    _COMMITTABLE_SPECIALIZED_ARGV -- must be classified volatile purely
    because it is absent from the allowlist, and a literal fixture entry for
    it must turn the redaction check red. No edit to _VOLATILE_ARGV, to
    _unredacted_fields, or to any other test is needed: the population is
    read live from _capture.py, so this is a rule over the whole capture
    set, not a list someone has to remember to extend."""
    new_argv = ("totally", "new", "specialized", "case")
    assert new_argv not in _COMMITTABLE_SPECIALIZED_ARGV
    monkeypatch.setattr(_capture, "SPECIALIZED_CASES", (*_capture.SPECIALIZED_CASES, new_argv))
    assert new_argv in _volatile_argv_for("specialized")
    cases = [{"argv": list(new_argv), "stdout": "literal unredacted output", "stderr": ""}]
    assert _unredacted_fields("specialized", cases) == [f"specialized.json {new_argv} stdout"]


def test_new_case_becomes_committable_only_via_declaration(monkeypatch):
    """The complement of arm (a): the same new case, once given a reasoned
    entry in _COMMITTABLE_SPECIALIZED_ARGV, drops out of the volatile
    population and its literal fixture text is accepted -- proving
    committing literal text is available, but only through the deliberate,
    reviewable act of adding a reason, not by silence."""
    new_argv = ("totally", "new", "specialized", "case")
    monkeypatch.setattr(_capture, "SPECIALIZED_CASES", (*_capture.SPECIALIZED_CASES, new_argv))
    monkeypatch.setattr(
        sys.modules[__name__],
        "_COMMITTABLE_SPECIALIZED_ARGV",
        {
            **_COMMITTABLE_SPECIALIZED_ARGV,
            new_argv: "test fixture: reviewed, static, no host state",
        },
    )
    assert new_argv not in _volatile_argv_for("specialized")
    cases = [{"argv": list(new_argv), "stdout": "literal reviewed output", "stderr": ""}]
    assert _unredacted_fields("specialized", cases) == []


# A committable declaration decays: the same command can grow env-, cwd-, or
# clock-derived content later without ever leaving the allowlist. The checks
# below re-verify the property that actually matters -- committable output
# must not depend on the machine, environment, or clock at all -- via
# `differential_capture_many` (see docs/internals/contracts.md). A hostname is
# identifying but constant per machine, so it can't show up as cross-run
# variance; that gap is closed separately by `known_machine_identity`.


def _offending_fields(runs: list[dict]) -> list[str]:
    """Field names ("stdout"/"stderr") whose captured content is not
    byte-identical across *runs* -- i.e. varies with environment, working
    directory, or wall clock and therefore cannot be committed as literal
    fixture text."""
    offenders = []
    for field in ("stdout", "stderr"):
        values = {r.get(field, "") for r in runs}
        if len(values) > 1:
            offenders.append(field)
    return offenders


def test_differential_capture_batch_crosses_the_clock_boundary_once(monkeypatch):
    now = [100.25]
    sleeps: list[float] = []

    def fake_run(argv, **_kwargs):
        return {
            "argv": list(argv),
            "stdout": f"second={int(now[0])}",
            "stderr": "",
            "exit_code": 0,
        }

    def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(_capture, "_run_cli_env", fake_run)
    monkeypatch.setattr(_capture.time, "time", lambda: now[0])
    monkeypatch.setattr(_capture.time, "sleep", fake_sleep)

    runs = _capture.differential_capture_many((("first",), ("second",)))

    assert len(sleeps) == 1
    assert sleeps[0] > 0
    assert set(runs) == {("first",), ("second",)}
    for case_runs in runs.values():
        assert len(case_runs) == 3
        assert case_runs[0]["stdout"] == case_runs[1]["stdout"] == "second=100"
        assert case_runs[2]["stdout"] == "second=101"


def _identity_hits(text: str, identity: frozenset[str]) -> list[str]:
    """Whole-word/whole-path occurrences of a known machine-identity value in
    *text*, including as a path-root prefix of a longer path (``/Users/lion``
    must hit inside ``/Users/lion/deeper/path`` -- a value under a redacted
    root is as identifying as the root itself), while a same-prefixed but
    distinct name (``/Users/lionx``) is not a hit. Word-bounded on the
    leading edge too: a short real username can legitimately be a substring
    of an unrelated CLI word (this repo's checkout username is a substring
    of "lionagi"), and a raw substring match would misreport that as a leak.
    """
    return [v for v in identity if v and re.search(rf"(?<![\w/]){re.escape(v)}(?!\w)", text)]


def test_offending_fields_accepts_identical_runs():
    """Positive control: byte-identical runs (the shape every genuinely
    static committable case must produce) report no offending fields."""
    runs = [
        {"stdout": "usage: li [-h]\n", "stderr": ""},
        {"stdout": "usage: li [-h]\n", "stderr": ""},
        {"stdout": "usage: li [-h]\n", "stderr": ""},
    ]
    assert _offending_fields(runs) == []


def test_offending_fields_flags_env_derived_variance():
    """Mutation arm: reproduces a username/hostname pair that a vocabulary
    check would accept because both words are independently public in this
    repo's own source, at the level that actually matters -- the value
    differs across two runs made under different simulated identities,
    exactly what a real env-derived "Connected to {user}@{host}" banner
    would do."""
    runs = [
        {"stdout": "Connected to admin@runner-1", "stderr": ""},
        {"stdout": "Connected to admin@runner-2", "stderr": ""},
    ]
    assert _offending_fields(runs) == ["stdout"]


def test_offending_fields_flags_clock_derived_variance():
    """Mutation arm: reproduces a timestamp / tmp-path shape the old token
    check was structurally blind to, since it only scanned alphabetic runs
    and three literal path prefixes. A clock- or tmpdir-derived value
    necessarily differs between two wall-clock-separated runs; this check
    needs no shape enumeration to catch it."""
    runs = [
        {
            "stdout": "session 2026-08-07T12:34:56.123456 /private/tmp/run-0123456789",
            "stderr": "",
        },
        {
            "stdout": "session 2026-08-07T12:34:57.654321 /private/tmp/run-9876543210",
            "stderr": "",
        },
    ]
    assert _offending_fields(runs) == ["stdout"]


def test_committable_case_output_is_env_and_clock_invariant():
    """Every committable case's LIVE output -- captured fresh under
    deliberately varied HOME/TMPDIR/USER/cwd and across a wall-clock gap --
    must be byte-identical across all runs. This is what catches a
    declared-safe command's output starting to depend on the machine later:
    the case-level declaration only asserts the command was safe when it was
    reviewed, not that it stays safe forever."""
    argvs = (*_COMMITTABLE_SPECIALIZED_ARGV, *_COMMITTABLE_MACHINE_ARGV)
    captures = _capture.differential_capture_many(argvs)
    offenders: dict[str, list[str]] = {}
    for argv in argvs:
        bad = _offending_fields(captures[argv])
        if bad:
            offenders[str(argv)] = bad
    assert not offenders, (
        "committable case output varies across environment/cwd/wall-clock "
        f"runs -- carries machine-, env-, or clock-derived state: {offenders}"
    )


def test_known_identity_check_flags_a_planted_hostname():
    """Mutation arm for the constant-identity gap: a hostname cannot vary
    between two runs on the same machine, so differential capture alone
    cannot catch it. Splice this machine's own real hostname into an
    "admin@{host}"-shaped banner -- the same env-derived-banner shape as
    ``test_offending_fields_flags_env_derived_variance`` above, but constant
    rather than varying -- and confirm the known-value check names it."""
    identity = _capture.known_machine_identity()
    hostname = socket.gethostname()
    text = f"Connected to admin@{hostname}"
    assert hostname in _identity_hits(text, identity)


def test_identity_hits_matches_a_path_roots_descendant():
    """Fix for a redaction-gap regression: a value found *underneath* a
    redacted path root -- not just the root occurring verbatim -- must also
    be caught. Before this fix, ``_identity_hits`` treated a trailing `/` as
    disqualifying (the same boundary that rejects a mid-word continuation),
    so a value like ``/Users/lion/some/deeper/path`` produced
    ``predicate_hits=[]`` for identity value ``/Users/lion`` while
    ``/Users/lion`` alone was caught -- the gate stayed green on exactly the
    kind of leak it exists to catch."""
    identity = frozenset({"/Users/lion"})
    descendant = "wrote output to /Users/lion/some/deeper/path"
    assert _identity_hits(descendant, identity) == ["/Users/lion"]
    # A same-prefixed but distinct name must still not be misreported.
    sibling = "wrote output to /Users/lionx/unrelated"
    assert _identity_hits(sibling, identity) == []

    # Point the control at the fix: no-op it (restore the pre-fix trailing
    # boundary, which excluded a following `/`) and require this exact
    # descendant case to go red.
    def _pre_fix_identity_hits(text: str, identity: frozenset[str]) -> list[str]:
        return [v for v in identity if v and re.search(rf"(?<![\w/]){re.escape(v)}(?![\w/])", text)]

    assert _pre_fix_identity_hits(descendant, identity) == [], (
        "control is not sensitive to the fix -- the pre-fix pattern should "
        "have missed this descendant path"
    )


def test_committable_case_output_has_no_known_machine_identity():
    """Every committable case's LIVE stdout/stderr must not contain this
    machine's own hostname, real username, home directory, or repo checkout
    path -- the one class of identifying value that is constant rather than
    cross-run variant, and so invisible to
    test_committable_case_output_is_env_and_clock_invariant above."""
    identity = _capture.known_machine_identity()
    live_specialized = {tuple(c["argv"]): c for c in _capture.capture_specialized()}
    live_machine = {tuple(c["argv"]): c for c in _capture.capture_machine()}

    offenders: dict[str, list[str]] = {}
    for argv in _COMMITTABLE_SPECIALIZED_ARGV:
        case = live_specialized[argv]
        for field in ("stdout", "stderr"):
            hit = _identity_hits(case.get(field, ""), identity)
            if hit:
                offenders[f"specialized {argv} {field}"] = hit
    for argv in _COMMITTABLE_MACHINE_ARGV:
        case = live_machine[argv]
        for field in ("stdout", "stderr"):
            hit = _identity_hits(case.get(field, ""), identity)
            if hit:
                offenders[f"machine {argv} {field}"] = hit

    assert not offenders, (
        "committable case output contains this machine's own hostname, "
        f"username, home directory, or checkout path: {offenders}"
    )
