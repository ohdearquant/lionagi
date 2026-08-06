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

Waivers (see delta_resolution.md for full evidence):
  W-01: cli.json / mcp.json / imports.json cannot be regenerated from base
    16e3d7ea6 with this capture code -- lionagi/_auto.py (the A0 registry
    these oracles inspect) has zero lines at base. This is a permanent
    structural property, not a pending task. Where these fixtures currently
    pass, they are self-consistency proofs against a post-A0-captured
    fixture, not base-equivalence proofs.
  W-02: the "agent status" / "doctor --machine" / "handshake --machine" /
    "runs --machine" cases carry live session, git-identity, and daemon
    state that is non-deterministic across checkouts and wall clocks --
    excluded below via _VOLATILE_ARGV / _VOLATILE_MACHINE_ARGV.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.contracts import _capture

DATA_DIR = Path(__file__).parent / "data"

# W-02 waiver (see delta_resolution.md): every argv below carries live
# session/git/daemon state that varies across checkouts and wall clocks by
# design, not because of a consolidation code change.
_VOLATILE_ARGV = {
    ("agent", "status"),
    ("monitor", "--machine"),
    ("agent", "--machine"),
    ("doctor", "--machine"),  # reports a live timestamp and working-tree cleanliness
    ("play", "list"),  # playbook names read from ~/.lionagi/playbooks/, host-specific
    ("play", "nonexistent"),  # error text lists the same host-specific playbook names
    ("skill", "list"),  # skill names read from ~/.lionagi/skills/, host-specific
}
_VOLATILE_MACHINE_ARGV = {
    ("handshake", "--machine"),  # data.comparison_ref reads live git state
    ("doctor", "--machine"),  # reports a live timestamp and working-tree cleanliness
    ("runs", "--machine"),  # lists live run/artifact state on disk
}


def _load(name: str):
    with open(DATA_DIR / f"{name}.json") as f:
        return json.load(f)


def _sorted_json(value):
    return json.dumps(value, indent=2, sort_keys=True)


def test_http_route_count_is_126():
    live = _capture.capture_http()
    assert live["count"] == 126


def test_http_routes_match_baseline():
    """http.json is now regenerated from unmodified base 16e3d7ea6 (see
    fixture_regeneration.md) and is byte-for-byte identical to the live
    capture (SHA-256 d7ab9a44...) — the strongest available
    behavior-preservation proof. The former xfail(strict=False), which
    documented this gate as temporarily stale pending base regeneration, no
    longer applies and has been removed rather than left to XPASS silently.
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


# W-01: cli.json is not a true base 16e3d7ea6 capture (lionagi/_auto.py,
# which capture_cli() reads, does not exist at base) -- this test currently
# passes as a self-consistency check against a post-A0-captured fixture, not
# as base-equivalence proof. See delta_resolution.md.
def test_cli_surface_matches_baseline():
    expected = _load("cli")
    live = _capture.capture_cli()
    assert _sorted_json(live) == _sorted_json(expected)


def test_cli_specialized_paths_match_baseline():
    expected = {tuple(c["argv"]): c for c in _load("specialized")}
    live = {tuple(c["argv"]): c for c in _capture.capture_specialized()}
    # `live` is a superset of `expected`: this round adds oracle coverage for
    # play/orchestrate-flow/orchestrate-fanout/schedule cases the frozen
    # fixture predates. specialized.json has since been regenerated from
    # base 16e3d7ea6 (see fixture_regeneration.md) — EXACT MATCH once the
    # W-02 environmental-noise cases below are excluded. Every
    # previously-frozen case must still be present and, modulo known
    # volatility, unchanged.
    assert set(expected) <= set(live), f"missing from live: {set(expected) - set(live)}"
    for argv, exp in expected.items():
        got = live[argv]
        if argv in _VOLATILE_ARGV:
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


@pytest.mark.xfail(
    reason=(
        "W-01 (permanent, not a pending task — see delta_resolution.md): "
        "mcp.json cannot be regenerated from base 16e3d7ea6 with this "
        "capture code because capture_mcp() reads the live CLI seed table "
        "via lionagi._auto (the A0 registry), which has zero lines at base "
        "-- confirmed by a real base-worktree regeneration attempt that "
        "hard-fails with ModuleNotFoundError on every run, not flakily. "
        "The frozen mcp.json fixture instead reflects a pre-oracle-repair, "
        "post-A0 capture (7-path PROJECTION_SAMPLE_PATHS sample), so it is "
        "methodologically incomparable to the current 75-path capture. "
        "Resolving this gate would require a design change to what this "
        "oracle asserts (e.g. new-registry self-consistency instead of "
        "base equivalence) -- out of this round's authority. Coverage of "
        "the new projections/errors is asserted structurally by the "
        "test_mcp_* self-validation tests below, independent of the stale "
        "fixture."
    ),
    strict=False,
)
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
    for argv, exp in expected.items():
        got = live[argv]
        assert got["exit_code"] == exp["exit_code"], f"exit code changed for {argv}"
        assert got["envelope_ok"] == exp["envelope_ok"], f"envelope ok changed for {argv}"
        if argv in _VOLATILE_ARGV or argv in _VOLATILE_MACHINE_ARGV:
            continue
        assert got["stdout"] == exp["stdout"], f"stdout changed for {argv}"


# W-01: imports.json is not a true base 16e3d7ea6 capture (capture_imports()
# reads lionagi._auto, which does not exist at base) -- this test currently
# passes as a self-consistency check against a post-A0-captured fixture, not
# as base-equivalence proof. See delta_resolution.md.
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


# Exit codes for the fourteen new specialized-CLI cases this round adds
# (play / orchestrate flow / orchestrate fanout / schedule quick-create and
# did-you-mean), live-verified against the worktree. specialized.json
# predates these cases, so they cannot be checked against a frozen baseline
# yet (see test_cli_specialized_paths_match_baseline's subset comparison and
# oracle_implementation.md); this is the structural self-validation for them.
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
