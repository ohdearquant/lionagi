# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""V0 behavior-preservation gate for the consolidation manifest.

Each test loads the frozen baseline captured before any consolidation edit
(``tests/contracts/data/*.json``, generated once by running the functions in
``_capture.py`` against the pre-consolidation worktree) and compares it,
field for field, against a fresh capture of the live code. A delta here means
an observable public surface changed; the manifest requires behavior
preservation, so these baselines must never be refreshed to match a change —
only to correct a capture bug.

Two fields carry inherent host-state volatility and are excluded from the
byte-for-byte comparison: the "agent status" specialized-CLI case includes a
live session UUID and elapsed timers, and machine-mode "monitor"/"agent"
cases report live run state. Their exit codes and envelope shape are still
compared; their literal stdout is not.

Waiver:
  W-02: the "agent status" / "doctor --machine" / "handshake --machine" /
    "runs --machine" cases carry live session, git-identity, and daemon
    state that is non-deterministic across checkouts and wall clocks. These
    -- and every other case captured by SPECIALIZED_CASES / MACHINE_CASES --
    are redacted by default: only an argv listed in
    _COMMITTABLE_SPECIALIZED_ARGV / _COMMITTABLE_MACHINE_ARGV, with a stated
    reason, is compared byte-for-byte or committed as literal text below.
    See test_new_case_defaults_closed_without_declaration for why this is a
    population rule, not a list someone has to remember to grow.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from tests.contracts import _capture

DATA_DIR = Path(__file__).parent / "data"

# Case-level allowlist: the ONLY way a case's literal captured stdout/stderr
# may be committed to this public repository. Each entry states why that
# argv's output is safe -- static argparse usage/help/error text, derived
# from this repo's own source, carrying no session/host/run state. A case
# captured by SPECIALIZED_CASES / MACHINE_CASES (tests/contracts/_capture.py)
# with no entry here is untrusted BY DEFAULT: excluded from the byte-for-byte
# comparison below, and its committed fixture entry must carry the
# redaction marker (test_volatile_fixture_cases_are_fully_redacted enforces
# this against the live population, not a fixed list -- see
# test_new_case_defaults_closed_without_declaration). Making a new case's
# output committable requires adding a reasoned entry here: a deliberate,
# reviewable diff, not something that happens by omission.
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
    """Every argv captured for *file_name* that is NOT declared committable
    above -- derived from the live capture-case population in _capture.py,
    not a hand-maintained denylist. A case appended to SPECIALIZED_CASES /
    MACHINE_CASES with no matching committable-allowlist entry is
    automatically volatile: excluded from byte-for-byte comparison and
    required to be redacted in the committed fixture."""
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


def test_http_route_count_is_133():
    live = _capture.capture_http()
    assert live["count"] == 133


def test_http_routes_match_baseline():
    """http.json is generated from this branch's own base commit and is
    byte-for-byte identical to the live capture — the strongest available
    behavior-preservation proof. Regenerate it (and this test's expected
    count above) only when an intentional route change lands, never to
    paper over an unreviewed drift.
    """
    expected = _load("http")
    live = _capture.capture_http()
    assert _sorted_json(live) == _sorted_json(expected)


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


def test_cli_specialized_paths_match_baseline():
    expected = {tuple(c["argv"]): c for c in _load("specialized")}
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
        assert got["stderr"] == exp["stderr"], f"stderr changed for {argv}"


def test_mcp_available_paths_match_baseline():
    expected = _load("mcp")
    live = _capture.capture_mcp()
    assert live["available_paths"] == expected["available_paths"]
    assert live["available_path_count"] == expected["available_path_count"]


def test_mcp_catalog_matches_baseline():
    expected = _load("mcp")
    live = _capture.capture_mcp()
    assert live["catalog"] == expected["catalog"]


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
    assert live["absent_verb_count"] == 30
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
# neither introduced by nor related to A0/C1D/C1X/C1's registry/dispatch
# work, so the import-laziness oracle allowlists exactly these two instead of
# asserting a blanket zero that would misreport them as regressions:
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
    (lionagi/casts/surfaces.py) — the exact boundary C1X's own result flagged
    as the eager-import risk to watch (implementer-4/c1x_result.md:71-76)."""
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


# `test_fixtures_carry_no_host_specific_state` above only catches leaks that
# happen to match one of a few path patterns. That is a pattern-shaped check:
# it says nothing about a captured field that leaks host state through some
# other shape (a session id, a library version, a CVE posture, a directory
# listing). The check below instead enumerates the *population* every
# byte-for-byte comparison already excludes as volatile (_volatile_argv_for,
# above -- derived from _COMMITTABLE_SPECIALIZED_ARGV / _COMMITTABLE_MACHINE_
# ARGV, not a hand-written denylist) and requires every captured stream of
# every case in that population to be either empty or carry the redaction
# marker -- never literal captured text. A case's content having no
# *currently visible* host-specific value is not an exemption: if it is
# excluded from comparison, pinning its literal bytes has no oracle value
# and is pure downside if the command ever starts reporting live state
# through that field.
_REDACTION_MARKER_RE = re.compile(r"^\[redacted: .+\]$", re.DOTALL)


def _unredacted_fields(file_name: str, cases: list) -> list[str]:
    """(file, argv, field) labels for every case classified volatile for
    *file_name* whose captured stream is neither empty nor the redaction
    marker -- i.e. still carries literal captured output."""
    volatile = _volatile_argv_for(file_name)
    offenders = []
    for case in cases:
        argv = tuple(case["argv"])
        if argv not in volatile:
            continue
        for field in ("stdout", "stderr"):
            value = case.get(field, "")
            if value and not _REDACTION_MARKER_RE.match(value):
                offenders.append(f"{file_name}.json {argv} {field}")
    return offenders


def test_volatile_fixture_cases_are_fully_redacted():
    offenders = []
    for file_name in ("specialized", "machine"):
        offenders += _unredacted_fields(file_name, _load(file_name))
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
