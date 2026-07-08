"""lionbench instance schema — a harvested-PR eval task, SWE-bench shaped.

One instance = one merged PR from our own fleet's history, decomposed exactly
like SWE-bench: a ``gold_patch`` (the reference fix, never shown to the agent),
a held-out ``test_patch`` (applied only after the agent finishes, then run),
and a scrubbed ``task_text`` describing the symptom without leaking the fix.
See the lion-bench v0 design contract, §1 (instance schema).

Instances round-trip through plain JSON, laid out **subject-first** (v0 amendment:
target scale is ~25 instances per subject across several subjects — rust-systems,
lean-proofs, python-framework, numerics-parity, kg-agentic, ...):

    data/{subject}/{instance_id}.json
    data/{subject}/manifest.json      # index of instance ids for that subject
    data/{subject}/rejected/{instance_id}.json
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_STEM = "manifest"


@dataclass(slots=True)
class OracleSpec:
    """The deterministic scoring oracle for one instance."""

    kind: str  # "pytest" (v0; other suites' oracle kinds land in later tiers)
    held_out_paths: list[str]  # test paths/node-ids applied via test_patch
    command: str  # e.g. "uv run pytest tests/x.py::test_y -q"
    test_patch: str  # the PR's test-file diff; hidden from the agent, applied at eval time

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> OracleSpec:
        return cls(
            kind=d["kind"],
            held_out_paths=list(d.get("held_out_paths", [])),
            command=d["command"],
            test_patch=d.get("test_patch", ""),
        )


@dataclass(slots=True)
class Provenance:
    """Where the instance came from — the nomination + PR/issue it decomposes."""

    pr: int
    issue: int | None = None
    nominated_by: str = ""
    why: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Provenance:
        return cls(
            pr=d["pr"],
            issue=d.get("issue"),
            nominated_by=d.get("nominated_by", ""),
            why=d.get("why", ""),
        )


@dataclass(slots=True)
class Validation:
    """Both-direction validation result (DESIGN_CONTRACT §5).

    ``gold_passes``/``null_fails`` are ``None`` until validation has actually
    run — an instance with either at ``None`` or ``False`` is not usable and
    must not be fed to the runner.
    """

    gold_passes: bool | None = None
    null_fails: bool | None = None
    leak_review: str = "pending"  # "pending" | "pass" | "fail"
    gold_output: str = ""
    null_output: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.gold_passes) and bool(self.null_fails) and self.leak_review == "pass"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Validation:
        return cls(
            gold_passes=d.get("gold_passes"),
            null_fails=d.get("null_fails"),
            leak_review=d.get("leak_review", "pending"),
            gold_output=d.get("gold_output", ""),
            null_output=d.get("null_output", ""),
        )


@dataclass(slots=True)
class Instance:
    """One lionbench T1 code-fix instance (DESIGN_CONTRACT §1)."""

    instance_id: str
    repo: str
    base_commit: str
    task_text: str
    oracle: OracleSpec
    gold_patch: str
    merged_at: str
    subject: str = "lionagi"  # rust-systems | lean-proofs | python-framework | ... (grouping axis)
    tier: str = "T1"
    provenance: Provenance = field(default_factory=lambda: Provenance(pr=0))
    validation: Validation = field(default_factory=Validation)
    needs_review: bool = True
    # Identifies the single merged PR this instance was derived from, e.g.
    # "lionagi#1843". Multiple instances (a fix instance, a diagnosis instance,
    # a long-horizon composite, ...) can share one source_pr — solving one
    # leaks the others, so the runner enforces a derivation-split: instances
    # sharing a source_pr never co-occur in the same eval run (see runner.py's
    # enforce_derivation_split).
    source_pr: str | None = None

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "task_text": self.task_text,
            "oracle": self.oracle.to_dict(),
            "gold_patch": self.gold_patch,
            "merged_at": self.merged_at,
            "subject": self.subject,
            "tier": self.tier,
            "provenance": self.provenance.to_dict(),
            "validation": self.validation.to_dict(),
            "needs_review": self.needs_review,
            "source_pr": self.source_pr,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Instance:
        return cls(
            instance_id=d["instance_id"],
            repo=d["repo"],
            base_commit=d["base_commit"],
            task_text=d["task_text"],
            oracle=OracleSpec.from_dict(d["oracle"]),
            gold_patch=d.get("gold_patch", ""),
            merged_at=d.get("merged_at", ""),
            subject=d.get("subject", "lionagi"),
            tier=d.get("tier", "T1"),
            source_pr=d.get("source_pr"),
            provenance=Provenance.from_dict(d.get("provenance", {"pr": 0})),
            validation=Validation.from_dict(d.get("validation", {})),
            needs_review=d.get("needs_review", True),
        )


def _subject_dir(data_dir: Path, subject: str) -> Path:
    return Path(data_dir) / subject


def save_instance(instance: Instance, data_dir: Path) -> Path:
    """Write one instance JSON under its subject dir + refresh that subject's
    manifest index. Returns the file path."""
    sdir = _subject_dir(data_dir, instance.subject)
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{instance.instance_id}.json"
    path.write_text(json.dumps(instance.to_dict(), indent=2, sort_keys=True))
    _write_manifest(sdir)
    return path


def save_rejection(instance_id: str, reason: str, data_dir: Path, subject: str = "lionagi") -> Path:
    """Write a rejection record next to (not into) the accepted-instance subject dir."""
    rej_dir = _subject_dir(data_dir, subject) / "rejected"
    rej_dir.mkdir(parents=True, exist_ok=True)
    path = rej_dir / f"{instance_id}.json"
    path.write_text(json.dumps({"instance_id": instance_id, "reason": reason}, indent=2))
    return path


def _write_manifest(subject_dir: Path) -> None:
    ids = sorted(p.stem for p in Path(subject_dir).glob("*.json") if p.stem != MANIFEST_STEM)
    manifest = {"instances": ids, "n": len(ids)}
    (Path(subject_dir) / f"{MANIFEST_STEM}.json").write_text(json.dumps(manifest, indent=2))


def list_subjects(data_dir: Path) -> list[str]:
    """Subject subdirectories present under ``data_dir`` (each with a manifest.json)."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return []
    return sorted(
        p.name for p in data_dir.iterdir() if p.is_dir() and (p / f"{MANIFEST_STEM}.json").exists()
    )


def load_manifest(data_dir: Path, subject: str | None = None) -> list[Instance]:
    """Load instance JSONs. ``subject=None`` loads every subject under ``data_dir``;
    a specific subject loads only that subdirectory."""
    data_dir = Path(data_dir)
    subjects = [subject] if subject else list_subjects(data_dir)
    instances: list[Instance] = []
    for subj in subjects:
        sdir = _subject_dir(data_dir, subj)
        for path in sorted(sdir.glob("*.json")):
            if path.stem == MANIFEST_STEM:
                continue
            instances.append(Instance.from_dict(json.loads(path.read_text())))
    return instances


def usable_instances(data_dir: Path, subject: str | None = None) -> list[Instance]:
    """Instances whose validation actually passed both directions + leak review."""
    return [i for i in load_manifest(data_dir, subject=subject) if i.validation.ok]
