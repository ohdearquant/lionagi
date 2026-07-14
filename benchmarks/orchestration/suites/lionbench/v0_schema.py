"""lionbench v0 durable record schemas — the join-keyed rows that make up one
campaign's evidence trail (DESIGN.md §2.2 "Required durable records", §8 item 1).

Every artifact row carries the same join keys so `campaign.json`, `cell.json`,
`injection.jsonl`, `usage.*.jsonl`, and `claims.json` can all be reconciled by
`{campaign_id, episode_id, treatment, iteration, run_id, branch_id,
session_id?, namespace_id, execution_scope}` (DESIGN.md §2.2). Records
round-trip losslessly through plain JSON, matching the ``to_dict``/
``from_dict`` convention already used by ``schema.py``'s ``Instance``.

A duplicate ``{campaign_id, episode_id, treatment, iteration}`` cell key must
fail closed rather than silently resume or overwrite a prior attempt
(DESIGN.md §6.2 item 7, §8 item 1) — see ``CellRegistry``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Treatment(str, Enum):
    """Arm B fleet switch (DESIGN.md §4.1): identical profile apart from this."""

    ON = "ON"
    OFF = "OFF"


class Iteration(str, Enum):
    """The two fresh-process runs of one episode (DESIGN.md §2.1)."""

    N = "N"
    N_PLUS_1 = "N+1"


class CellStatus(str, Enum):
    """The decision-rule states a cell can settle into (DESIGN.md §9)."""

    VALID = "valid"
    INSTRUMENT_INVALID = "instrument_invalid"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    MEASURED_UNVERIFIED = "measured_unverified"
    VERIFIED = "verified"


# --- content hashing helpers (DESIGN.md §2.2 "hashes of every raw artifact") ---


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def hash_json(obj: Any) -> str:
    """Canonical content hash of a JSON-serializable object: sorted keys, no
    whitespace ambiguity, so the same logical config always hashes the same."""
    return hash_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str))


def hash_file(path: str | Path) -> str:
    return sha256_hex(Path(path).read_bytes())


@dataclass(slots=True)
class JoinKeys:
    """The durable join keys every lionbench v0 record row carries (DESIGN.md
    §2.2). ``cell_key`` is the uniqueness key a ``CellRegistry`` rejects
    duplicates on (§6.2 item 7)."""

    campaign_id: str
    episode_id: str
    treatment: Treatment
    iteration: Iteration
    run_id: str
    branch_id: str
    namespace_id: str
    execution_scope: str  # "host" | "sandbox"
    session_id: str | None = None

    @property
    def cell_key(self) -> tuple[str, str, str, str]:
        return (self.campaign_id, self.episode_id, self.treatment.value, self.iteration.value)

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "episode_id": self.episode_id,
            "treatment": self.treatment.value,
            "iteration": self.iteration.value,
            "run_id": self.run_id,
            "branch_id": self.branch_id,
            "namespace_id": self.namespace_id,
            "execution_scope": self.execution_scope,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> JoinKeys:
        return cls(
            campaign_id=d["campaign_id"],
            episode_id=d["episode_id"],
            treatment=Treatment(d["treatment"]),
            iteration=Iteration(d["iteration"]),
            run_id=d["run_id"],
            branch_id=d["branch_id"],
            namespace_id=d["namespace_id"],
            execution_scope=d["execution_scope"],
            session_id=d.get("session_id"),
        )


@dataclass(slots=True)
class Campaign:
    """``campaign.json`` (DESIGN.md §2.2 table). Immutable once the campaign
    begins. ``fingerprint`` changes whenever model, profile hash, price
    table, sandbox image, or isolation setting changes (§8 item 1)."""

    campaign_id: str
    git_sha: str
    git_dirty: bool
    lionagi_version: str
    khive_version: str
    cli_version: str
    image_digest: str
    model: str
    model_revision: str
    profile_hash: str
    price_table_hash: str
    isolation_requested: str
    isolation_effective: str
    seed: int = 0
    dataset_hashes: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def fingerprint(self) -> str:
        """Content hash of the fields that define a distinct measurement
        configuration. Deliberately excludes ``campaign_id``/``created_at``
        (identity/timing, not configuration) and ``git_sha``/``git_dirty``
        (provenance, not a configuration knob) so identical inputs always
        fingerprint identically."""
        payload = {
            "model": self.model,
            "model_revision": self.model_revision,
            "profile_hash": self.profile_hash,
            "price_table_hash": self.price_table_hash,
            "image_digest": self.image_digest,
            "isolation_requested": self.isolation_requested,
            "isolation_effective": self.isolation_effective,
            "lionagi_version": self.lionagi_version,
            "khive_version": self.khive_version,
            "cli_version": self.cli_version,
            "seed": self.seed,
            "dataset_hashes": self.dataset_hashes,
            "parameters": self.parameters,
        }
        return hash_json(payload)

    def to_dict(self) -> dict:
        return {
            "campaign_id": self.campaign_id,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "lionagi_version": self.lionagi_version,
            "khive_version": self.khive_version,
            "cli_version": self.cli_version,
            "image_digest": self.image_digest,
            "model": self.model,
            "model_revision": self.model_revision,
            "profile_hash": self.profile_hash,
            "price_table_hash": self.price_table_hash,
            "isolation_requested": self.isolation_requested,
            "isolation_effective": self.isolation_effective,
            "seed": self.seed,
            "dataset_hashes": self.dataset_hashes,
            "parameters": self.parameters,
            "created_at": self.created_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Campaign:
        return cls(
            campaign_id=d["campaign_id"],
            git_sha=d.get("git_sha", ""),
            git_dirty=d.get("git_dirty", False),
            lionagi_version=d.get("lionagi_version", ""),
            khive_version=d.get("khive_version", ""),
            cli_version=d.get("cli_version", ""),
            image_digest=d.get("image_digest", ""),
            model=d["model"],
            model_revision=d.get("model_revision", ""),
            profile_hash=d.get("profile_hash", ""),
            price_table_hash=d.get("price_table_hash", ""),
            isolation_requested=d.get("isolation_requested", ""),
            isolation_effective=d.get("isolation_effective", ""),
            seed=d.get("seed", 0),
            dataset_hashes=dict(d.get("dataset_hashes", {})),
            parameters=dict(d.get("parameters", {})),
            created_at=d.get("created_at", ""),
        )


@dataclass(slots=True)
class Episode:
    """One episode's identity/pairing metadata (DESIGN.md §2.1): the N and
    N+1 counterbalanced task variants sharing one family."""

    episode_id: str
    campaign_id: str
    family_id: str
    variant_order: str  # "A_then_B" | "B_then_A"
    replica_index: int
    base_commit: str
    task_variant_n: str
    task_variant_n1: str
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "campaign_id": self.campaign_id,
            "family_id": self.family_id,
            "variant_order": self.variant_order,
            "replica_index": self.replica_index,
            "base_commit": self.base_commit,
            "task_variant_n": self.task_variant_n,
            "task_variant_n1": self.task_variant_n1,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Episode:
        return cls(
            episode_id=d["episode_id"],
            campaign_id=d["campaign_id"],
            family_id=d["family_id"],
            variant_order=d["variant_order"],
            replica_index=d["replica_index"],
            base_commit=d["base_commit"],
            task_variant_n=d["task_variant_n"],
            task_variant_n1=d["task_variant_n1"],
            created_at=d.get("created_at", ""),
        )


@dataclass(slots=True)
class Cell:
    """``cell.json`` (DESIGN.md §2.2 table): arm/iteration/task identity,
    manifest path, StateDB locator, status, duration, oracle result, and
    hashes of every raw artifact."""

    keys: JoinKeys
    manifest_path: str
    status: CellStatus = CellStatus.INCOMPLETE
    duration_s: float = 0.0
    statedb_locator: str | None = None
    oracle_passed: bool | None = None
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "keys": self.keys.to_dict(),
            "manifest_path": self.manifest_path,
            "status": self.status.value,
            "duration_s": self.duration_s,
            "statedb_locator": self.statedb_locator,
            "oracle_passed": self.oracle_passed,
            "artifact_hashes": self.artifact_hashes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Cell:
        return cls(
            keys=JoinKeys.from_dict(d["keys"]),
            manifest_path=d["manifest_path"],
            status=CellStatus(d.get("status", CellStatus.INCOMPLETE.value)),
            duration_s=d.get("duration_s", 0.0),
            statedb_locator=d.get("statedb_locator"),
            oracle_passed=d.get("oracle_passed"),
            artifact_hashes=dict(d.get("artifact_hashes", {})),
            created_at=d.get("created_at", ""),
        )


def _reject_unredacted(field_name: str, value: str | None) -> None:
    """Redaction tripwire for ``InjectionTrace``: hashes and IDs never contain
    whitespace, so a value carrying spaces or newlines is almost certainly
    un-redacted memory content or a credential leaking into the trace store —
    fail closed rather than persist it."""
    if value is not None and any(ch.isspace() for ch in value):
        raise ValueError(
            f"InjectionTrace.{field_name} contains whitespace; it must be a "
            "redacted hash or id (the injection collector redacts before this "
            "record is built), never raw content or credentials"
        )


@dataclass(slots=True)
class InjectionTrace:
    """One redacted ``injection.jsonl`` row: a recall, compose, auto-feedback,
    writeback, or failure event (DESIGN.md §2.2 table). Carries only redacted
    hashes and IDs, never memory content or credentials — construction fails
    closed (see ``_reject_unredacted``) when a hash/id field holds whitespace,
    a tripwire for un-redacted content. The injection collector is responsible
    for redacting before this row is built."""

    keys: JoinKeys
    event_type: str  # "recall" | "compose" | "auto_feedback" | "writeback" | "failure"
    policy_hash: str
    query_hash: str | None = None
    returned_ids: list[str] = field(default_factory=list)
    written_ids: list[str] = field(default_factory=list)
    token_count: int = 0
    error_class: str | None = None
    timestamp: str = ""

    def __post_init__(self) -> None:
        _reject_unredacted("policy_hash", self.policy_hash)
        _reject_unredacted("query_hash", self.query_hash)
        _reject_unredacted("error_class", self.error_class)
        for _id in self.returned_ids:
            _reject_unredacted("returned_ids", _id)
        for _id in self.written_ids:
            _reject_unredacted("written_ids", _id)

    def to_dict(self) -> dict:
        return {
            "keys": self.keys.to_dict(),
            "event_type": self.event_type,
            "policy_hash": self.policy_hash,
            "query_hash": self.query_hash,
            "returned_ids": self.returned_ids,
            "written_ids": self.written_ids,
            "token_count": self.token_count,
            "error_class": self.error_class,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> InjectionTrace:
        return cls(
            keys=JoinKeys.from_dict(d["keys"]),
            event_type=d["event_type"],
            policy_hash=d["policy_hash"],
            query_hash=d.get("query_hash"),
            returned_ids=list(d.get("returned_ids", [])),
            written_ids=list(d.get("written_ids", [])),
            token_count=d.get("token_count", 0),
            error_class=d.get("error_class"),
            timestamp=d.get("timestamp", ""),
        )


@dataclass(slots=True)
class UsageRow:
    """One ``usage.{host,sandbox}.jsonl`` row. Dimensions match the shared
    meter's normalized billing dimensions exactly (``harness/cost.py``'s
    ``Usage``: uncached input, cache read, cache write, output) so a
    lionbench ledger can be built straight from serialized rows."""

    keys: JoinKeys
    generation_id: str
    model: str
    uncached_input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    num_turns: int = 0
    reasoning_disclosed: bool = True
    usage_source: str = "none"  # "reported" | "estimated" | "mixed" | "none"

    def to_dict(self) -> dict:
        return {
            "keys": self.keys.to_dict(),
            "generation_id": self.generation_id,
            "model": self.model,
            "uncached_input": self.uncached_input,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "output": self.output,
            "num_turns": self.num_turns,
            "reasoning_disclosed": self.reasoning_disclosed,
            "usage_source": self.usage_source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> UsageRow:
        return cls(
            keys=JoinKeys.from_dict(d["keys"]),
            generation_id=d["generation_id"],
            model=d["model"],
            uncached_input=d.get("uncached_input", 0),
            cache_read=d.get("cache_read", 0),
            cache_write=d.get("cache_write", 0),
            output=d.get("output", 0),
            num_turns=d.get("num_turns", 0),
            reasoning_disclosed=d.get("reasoning_disclosed", True),
            usage_source=d.get("usage_source", "none"),
        )


@dataclass(slots=True)
class ClaimRecord:
    """One ``claims.json`` row (DESIGN.md §5, §2.2 table): a verification
    claim span joined to its supporting tool-trace evidence and oracle
    outcome, with an explicit true/false/ambiguous adjudication."""

    keys: JoinKeys
    claim_id: str
    text_span: str
    rule_label: str
    supporting_event_ids: list[str] = field(default_factory=list)
    oracle_outcome: bool | None = None
    adjudication_state: str = "auto"  # "auto" | "human_pending" | "human_reviewed"
    result: str = "ambiguous"  # "true" | "false" | "ambiguous"

    def to_dict(self) -> dict:
        return {
            "keys": self.keys.to_dict(),
            "claim_id": self.claim_id,
            "text_span": self.text_span,
            "rule_label": self.rule_label,
            "supporting_event_ids": self.supporting_event_ids,
            "oracle_outcome": self.oracle_outcome,
            "adjudication_state": self.adjudication_state,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ClaimRecord:
        return cls(
            keys=JoinKeys.from_dict(d["keys"]),
            claim_id=d["claim_id"],
            text_span=d["text_span"],
            rule_label=d["rule_label"],
            supporting_event_ids=list(d.get("supporting_event_ids", [])),
            oracle_outcome=d.get("oracle_outcome"),
            adjudication_state=d.get("adjudication_state", "auto"),
            result=d.get("result", "ambiguous"),
        )


class DuplicateCellKeyError(ValueError):
    """A ``{campaign_id, episode_id, treatment, iteration}`` key was already
    registered (DESIGN.md §6.2 item 7: fail closed, never silently resume)."""


class CellRegistry:
    """Tracks ``Cell`` join keys and rejects duplicates. A partially written
    cell is ``incomplete``, not resumably re-scored under the same key
    (DESIGN.md §6.2 item 7) — retrying requires a new attempt/run_id, which
    is a different key only if it changes ``run_id`` without changing the
    campaign/episode/treatment/iteration identity checked here."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, str, str]] = set()

    def add(self, cell: Cell) -> None:
        key = cell.keys.cell_key
        if key in self._seen:
            raise DuplicateCellKeyError(
                f"duplicate cell key {key!r} — a cell with this "
                "{campaign_id, episode_id, treatment, iteration} was already registered"
            )
        self._seen.add(key)

    def __contains__(self, key: tuple[str, str, str, str]) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)
