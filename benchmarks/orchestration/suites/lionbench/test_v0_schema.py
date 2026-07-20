"""Unit tests for lionbench v0's durable record schemas + campaign config
(DESIGN.md §2.2, §8 item 1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from v0_config import assemble_campaign, git_sha_and_dirty, hash_price_table  # noqa: E402
from v0_schema import (  # noqa: E402
    Campaign,
    Cell,
    CellRegistry,
    CellStatus,
    ClaimRecord,
    DuplicateCellKeyError,
    Episode,
    InjectionTrace,
    Iteration,
    JoinKeys,
    Treatment,
    UsageRow,
    hash_file,
    hash_json,
    hash_text,
)


def _keys(**overrides) -> JoinKeys:
    defaults = dict(
        campaign_id="camp-1",
        episode_id="ep-1",
        treatment=Treatment.ON,
        iteration=Iteration.N,
        run_id="run-1",
        branch_id="branch-1",
        namespace_id="ns-1",
        execution_scope="sandbox",
        session_id="sess-1",
    )
    defaults.update(overrides)
    return JoinKeys(**defaults)


def _campaign(**overrides) -> Campaign:
    defaults = dict(
        campaign_id="camp-1",
        git_sha="deadbeef",
        git_dirty=False,
        lionagi_version="0.22.6",
        khive_version="1.0.0",
        cli_version="1.0.0",
        image_digest="sha256:aaa",
        model="claude-code/sonnet",
        model_revision="2026-06-01",
        profile_hash="hash-profile-a",
        price_table_hash="hash-prices-a",
        isolation_requested="strict",
        isolation_effective="strict",
        seed=42,
        dataset_hashes={"flywheel": "hash-data-a"},
        parameters={"max_turns": 40},
        created_at="2026-07-14T00:00:00Z",
    )
    defaults.update(overrides)
    return Campaign(**defaults)


# --- JSON round trip for every record type (Task 1 acceptance #1) ---


def test_campaign_round_trip():
    camp = _campaign()
    restored = Campaign.from_dict(camp.to_dict())
    assert restored == camp
    assert restored.fingerprint == camp.fingerprint


def test_episode_round_trip():
    ep = Episode(
        episode_id="ep-1",
        campaign_id="camp-1",
        family_id="family-a",
        variant_order="A_then_B",
        replica_index=2,
        base_commit="deadbeef",
        task_variant_n="A",
        task_variant_n1="B",
        created_at="2026-07-14T00:00:00Z",
    )
    restored = Episode.from_dict(ep.to_dict())
    assert restored == ep


def test_cell_round_trip():
    cell = Cell(
        keys=_keys(),
        manifest_path="/runs/run-1/run.json",
        status=CellStatus.VALID,
        duration_s=12.5,
        statedb_locator="statedb://sessions/abc",
        oracle_passed=True,
        artifact_hashes={"tool_trace.jsonl": "abc123"},
        created_at="2026-07-14T00:00:00Z",
    )
    restored = Cell.from_dict(cell.to_dict())
    assert restored == cell


def test_injection_trace_round_trip():
    trace = InjectionTrace(
        keys=_keys(),
        event_type="writeback",
        policy_hash="policy-hash",
        query_hash="query-hash",
        returned_ids=["mem-1", "mem-2"],
        written_ids=["mem-3"],
        token_count=128,
        error_class=None,
        timestamp="2026-07-14T00:00:01Z",
    )
    restored = InjectionTrace.from_dict(trace.to_dict())
    assert restored == trace


def test_injection_trace_rejects_unredacted_content():
    """A hash/id field carrying whitespace is treated as un-redacted content and
    rejected at construction (the redaction tripwire)."""
    with pytest.raises(ValueError):
        InjectionTrace(
            keys=_keys(),
            event_type="recall",
            policy_hash="policy-hash",
            query_hash="the user asked about their password reset",
        )
    with pytest.raises(ValueError):
        InjectionTrace(
            keys=_keys(),
            event_type="writeback",
            policy_hash="policy-hash",
            written_ids=["mem-1", "leaked secret value with spaces"],
        )


def test_usage_row_round_trip():
    row = UsageRow(
        keys=_keys(execution_scope="host"),
        generation_id="gen-1",
        model="claude-code/sonnet",
        uncached_input=1000,
        cache_read=200,
        cache_write=50,
        output=300,
        num_turns=4,
        reasoning_disclosed=True,
        usage_source="reported",
    )
    restored = UsageRow.from_dict(row.to_dict())
    assert restored == row


def test_claim_record_round_trip():
    claim = ClaimRecord(
        keys=_keys(iteration=Iteration.N_PLUS_1),
        claim_id="claim-1",
        text_span="I ran the tests and they passed.",
        rule_label="tests_pass",
        supporting_event_ids=["evt-1", "evt-2"],
        oracle_outcome=False,
        adjudication_state="human_reviewed",
        result="false",
    )
    restored = ClaimRecord.from_dict(claim.to_dict())
    assert restored == claim


def test_join_keys_round_trip_without_optional_session_id():
    keys = _keys(session_id=None)
    restored = JoinKeys.from_dict(keys.to_dict())
    assert restored == keys
    assert restored.session_id is None


# --- Duplicate cell key rejection, fail-closed (Task 1 acceptance #2) ---


def test_duplicate_cell_key_is_rejected():
    registry = CellRegistry()
    cell_n = Cell(keys=_keys(iteration=Iteration.N), manifest_path="/runs/run-1/run.json")
    dup = Cell(
        keys=_keys(iteration=Iteration.N, run_id="run-1-retry"),
        manifest_path="/runs/run-1-retry/run.json",
    )

    registry.add(cell_n)
    with pytest.raises(DuplicateCellKeyError):
        registry.add(dup)

    assert len(registry) == 1


def test_distinct_iteration_or_treatment_is_not_a_duplicate():
    registry = CellRegistry()
    cell_n = Cell(keys=_keys(iteration=Iteration.N), manifest_path="/runs/run-1/run.json")
    cell_n1 = Cell(keys=_keys(iteration=Iteration.N_PLUS_1), manifest_path="/runs/run-2/run.json")
    cell_off = Cell(
        keys=_keys(iteration=Iteration.N, treatment=Treatment.OFF),
        manifest_path="/runs/run-3/run.json",
    )

    registry.add(cell_n)
    registry.add(cell_n1)
    registry.add(cell_off)

    assert len(registry) == 3


def test_duplicate_key_ignores_non_key_fields():
    """Same {campaign, episode, treatment, iteration} but a different run_id
    is still a duplicate — run_id/branch_id are not part of the cell key."""
    registry = CellRegistry()
    registry.add(Cell(keys=_keys(run_id="run-a"), manifest_path="/runs/run-a/run.json"))
    with pytest.raises(DuplicateCellKeyError):
        registry.add(Cell(keys=_keys(run_id="run-b"), manifest_path="/runs/run-b/run.json"))


def test_duplicate_key_ignores_both_run_id_and_branch_id():
    """Same {campaign, episode, treatment, iteration} is a duplicate even when
    BOTH run_id and branch_id differ — neither is part of the cell key."""
    registry = CellRegistry()
    registry.add(
        Cell(keys=_keys(run_id="run-a", branch_id="br-a"), manifest_path="/runs/run-a/run.json")
    )
    with pytest.raises(DuplicateCellKeyError):
        registry.add(
            Cell(keys=_keys(run_id="run-b", branch_id="br-b"), manifest_path="/runs/run-b/run.json")
        )


# --- Campaign fingerprint stability + change detection (acceptance #3) ---


def test_fingerprint_stable_for_identical_inputs():
    a = _campaign()
    b = _campaign()
    assert a.fingerprint == b.fingerprint


def test_fingerprint_stable_across_non_config_fields():
    """campaign_id/created_at/git_sha/git_dirty are identity/provenance, not
    configuration — changing them must not change the fingerprint."""
    a = _campaign()
    b = _campaign(
        campaign_id="camp-2",
        created_at="2026-08-01T00:00:00Z",
        git_sha="cafebabe",
        git_dirty=True,
    )
    assert a.fingerprint == b.fingerprint


@pytest.mark.parametrize(
    "field_name,new_value",
    [
        ("model", "claude-code/opus"),
        ("profile_hash", "hash-profile-b"),
        ("price_table_hash", "hash-prices-b"),
        ("image_digest", "sha256:bbb"),
        ("isolation_effective", "unheld"),
        ("isolation_requested", "unheld"),
        ("parameters", {"max_turns": 80}),
    ],
)
def test_fingerprint_changes_when_config_factor_changes(field_name, new_value):
    a = _campaign()
    b = _campaign(**{field_name: new_value})
    assert a.fingerprint != b.fingerprint


def test_hash_price_table_changes_fingerprint_via_assemble_campaign(tmp_path):
    common = dict(
        campaign_id="camp-1",
        model="claude-code/sonnet",
        model_revision="2026-06-01",
        profile_hash="hash-profile-a",
        image_digest="sha256:aaa",
        isolation_requested="strict",
        isolation_effective="strict",
        lionagi_version="0.22.6",
        khive_version="1.0.0",
        cli_version="1.0.0",
        created_at="2026-07-14T00:00:00Z",
    )
    campaign_a = assemble_campaign(price_table={"claude-code/sonnet": [3.0, 15.0, 0.3]}, **common)
    campaign_b = assemble_campaign(price_table={"claude-code/sonnet": [5.0, 25.0, 0.5]}, **common)

    assert campaign_a.fingerprint != campaign_b.fingerprint
    assert campaign_a.price_table_hash == hash_price_table({"claude-code/sonnet": [3.0, 15.0, 0.3]})


def test_git_sha_and_dirty_defaults_when_not_a_git_repo(tmp_path):
    sha, dirty = git_sha_and_dirty(tmp_path)
    assert sha == ""
    assert dirty is False


# --- content hashing helpers ---


def test_hash_json_is_order_independent_and_deterministic():
    a = hash_json({"x": 1, "y": 2})
    b = hash_json({"y": 2, "x": 1})
    assert a == b
    assert hash_json({"x": 1, "y": 3}) != a


def test_hash_text_matches_hash_file(tmp_path):
    content = "some raw artifact content\n"
    path = tmp_path / "artifact.txt"
    path.write_text(content)
    assert hash_file(path) == hash_text(content)
