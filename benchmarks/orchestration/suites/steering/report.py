"""Provider-by-arm adherence table — the committed evidence artifact (ADR-0088)."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from harness.stats import Proportion, wilson

from .arms import Arm
from .runner import SteerRunResult

# Verbatim from ADR-0088 "Success gate (pre-registered, the number that
# decides B)" and the task's restatement — do not recompute or alter.
GATE_TEXT = (
    "PASS if arm2-arm1 >= 0.4 absolute AND arm2 >= 0.8 on >= 2 of 4 providers AND arm0 <= 0.1"
)


def _valid(results: list[SteerRunResult]) -> list[SteerRunResult]:
    return [r for r in results if r.error is None]


def _proportions(results: list[SteerRunResult]) -> dict[tuple[str, str], Proportion]:
    """(provider, arm) -> Wilson proportion over valid trials."""
    by_cell: dict[tuple[str, str], list[SteerRunResult]] = defaultdict(list)
    for r in _valid(results):
        by_cell[(r.provider, r.arm)].append(r)
    return {
        cell: wilson(sum(r.adherent for r in trials), len(trials))
        for cell, trials in by_cell.items()
    }


def evaluate_gate(props: dict[tuple[str, str], Proportion]) -> dict:
    """Evaluate the pre-registered gate per provider; returns a verdict dict."""
    providers = sorted({provider for provider, _arm in props})
    per_provider = {}
    clearing = 0
    for provider in providers:
        arm0 = props.get((provider, Arm.NO_STEER.value))
        arm1 = props.get((provider, Arm.STEER_BURIED.value))
        arm2 = props.get((provider, Arm.STEER_RENDERED.value))
        if not (arm0 and arm1 and arm2 and arm0.n and arm1.n and arm2.n):
            per_provider[provider] = {"clears": False, "reason": "incomplete cells"}
            continue
        lift = arm2.p - arm1.p
        clears = lift >= 0.4 and arm2.p >= 0.8 and arm0.p <= 0.1
        per_provider[provider] = {
            "clears": clears,
            "arm0": arm0.p,
            "arm1": arm1.p,
            "arm2": arm2.p,
            "lift": lift,
        }
        if clears:
            clearing += 1
    return {
        "gate_text": GATE_TEXT,
        "per_provider": per_provider,
        "providers_clearing": clearing,
        "pass": clearing >= 2,
    }


def build_report(results: list[SteerRunResult], *, smoke: bool = False) -> tuple[str, dict]:
    """Return (markdown, json-dict) for the provider-by-arm adherence table."""
    props = _proportions(results)
    verdict = evaluate_gate(props)
    providers = sorted({r.provider for r in results})
    arms = [a.value for a in (Arm.NO_STEER, Arm.STEER_BURIED, Arm.STEER_RENDERED)]
    errors = sum(1 for r in results if r.error is not None)

    lines = []
    label = "SMOKE (N far below pre-registered — NOT evidence)" if smoke else "EVIDENCE"
    lines.append(f"# ADR-0088 steer-adherence table — {label}")
    lines.append("")
    lines.append(f"**Pre-registered gate**: {GATE_TEXT}")
    lines.append("")
    header = "| provider | " + " | ".join(arms) + " |"
    sep = "|---|" + "---|" * len(arms)
    lines.append(header)
    lines.append(sep)
    for provider in providers:
        row = [provider]
        for arm in arms:
            p = props.get((provider, arm))
            row.append(p.fmt() if p else "N/A")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append(f"Errored trials excluded from proportions: {errors}")
    lines.append("")
    lines.append(
        f"**Gate result**: {'PASS' if verdict['pass'] else 'FAIL'} "
        f"({verdict['providers_clearing']} of {len(providers)} providers clear)"
    )
    if smoke:
        lines.append("")
        lines.append(
            "This table is a SMOKE run (N=2/arm, claude_code only) proving the "
            "harness runs end-to-end and produces the table — it is NOT the "
            "pre-registered N>=20/cell evidence run and the gate result above "
            "is not a real verdict."
        )
    markdown = "\n".join(lines) + "\n"

    payload = {
        "smoke": smoke,
        "gate_text": GATE_TEXT,
        "table": {
            f"{provider}/{arm}": (
                props[(provider, arm)].fmt() if (provider, arm) in props else None
            )
            for provider in providers
            for arm in arms
        },
        "verdict": verdict,
        "errors": errors,
        "raw_results": [asdict(r) for r in results],
    }
    return markdown, payload


def write_report(results: list[SteerRunResult], out_dir: Path, *, smoke: bool = False) -> None:
    """Write the committed markdown + json artifact pair."""
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown, payload = build_report(results, smoke=smoke)
    (out_dir / "adherence_table.md").write_text(markdown, encoding="utf-8")
    (out_dir / "adherence_table.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
