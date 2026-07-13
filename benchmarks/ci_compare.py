from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

_DURATION_FIELDS = ("min", "mean", "median", "max")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _finite_float(raw: str) -> float:
    """argparse type= for --threshold: rejects NaN/Infinity (every delta > threshold is False for a NaN threshold)."""
    value = float(raw)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be a finite number, got {raw!r}")
    return value


def _validate_stat(name: str, side: str, stat) -> str | None:
    """Error message if `stat` isn't safe to compute a delta from (missing/non-numeric/NaN), else None."""
    if not isinstance(stat, dict):
        return f"{name} ({side}): entry must be an object, got {type(stat).__name__}: {stat!r}"
    runs = stat.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs <= 0:
        return f"{name} ({side}): 'runs' must be a positive integer, got {runs!r}"
    for field in _DURATION_FIELDS:
        value = stat.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{name} ({side}): '{field}' must be a number, got {value!r}"
        if not math.isfinite(value):
            return f"{name} ({side}): '{field}' is not finite, got {value!r}"
        if value < 0:
            return f"{name} ({side}): '{field}' must be non-negative, got {value!r}"
    return None


def compare(
    current: dict,
    baseline: dict,
    threshold: float,
    normalize_by: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, report). ok=False if any scenario regresses beyond threshold.

    Threshold is relative increase on median time (e.g., 0.2 = 20%).
    """
    lines = []
    norm_note = f" (normalized by {normalize_by})" if normalize_by else ""
    lines.append(f"Threshold: {threshold:.0%} (negative = faster, positive = slower){norm_note}")
    ok = True
    compared = 0

    cur_results = current.get("results", {})
    base_results = baseline.get("results", {})

    # Determine anchors if normalization requested
    cur_anchor = None
    base_anchor = None
    if normalize_by:
        ca = cur_results.get(normalize_by)
        ba = base_results.get(normalize_by)
        if ca and ba:
            try:
                cur_anchor = float(ca.get("median", 0)) or None
                base_anchor = float(ba.get("median", 0)) or None
            except Exception:
                cur_anchor = base_anchor = None

    for name, cur in sorted(cur_results.items()):
        if name not in base_results:
            lines.append(f"- {name}: no baseline; skipping")
            continue
        base = base_results[name]
        compared += 1

        error = _validate_stat(name, "current", cur) or _validate_stat(name, "baseline", base)
        if error:
            lines.append(f"- {name}: INVALID -- {error}")
            ok = False
            continue

        cur_med = float(cur.get("median", 0))
        base_med = float(base.get("median", 0))

        # Normalize by anchor if available and not comparing the anchor itself
        if (
            normalize_by
            and cur_anchor
            and base_anchor
            and name != normalize_by
            and cur_anchor > 0
            and base_anchor > 0
        ):
            cur_med = cur_med / cur_anchor
            base_med = base_med / base_anchor
        if base_med == 0:
            delta = float("inf") if cur_med > 0 else 0.0
        else:
            delta = (cur_med - base_med) / base_med
        line = f"- {name}: median {cur_med:.6f}s vs {base_med:.6f}s -> {delta:+.1%}"
        if normalize_by and name != normalize_by and cur_anchor and base_anchor:
            line += f" (normalized by {normalize_by})"
        lines.append(line)
        if delta > threshold:
            ok = False

    if compared == 0:
        # Belt-and-suspenders for ci_check_provenance.py's same check, in case that step is ever skipped or reordered.
        lines.append(
            "ERROR: zero scenarios were actually compared (baseline and current share "
            "no scenario in common) -- this gate produced no signal, not a clean pass."
        )
        ok = False

    return ok, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="Path to baseline JSON")
    ap.add_argument("--current", required=True, help="Path to current JSON")
    ap.add_argument(
        "--threshold",
        type=_finite_float,
        default=0.2,
        help="Relative regression threshold (e.g., 0.2 = 20%)",
    )
    ap.add_argument(
        "--normalize-by",
        type=str,
        default=None,
        help="Scenario name to normalize medians by",
    )
    args = ap.parse_args()

    baseline_path = Path(args.baseline)
    current_path = Path(args.current)

    if not baseline_path.exists():
        print(f"[ci_compare] Baseline missing: {baseline_path}.")
        return 1
    if not current_path.exists():
        print(f"[ci_compare] Current results missing: {current_path}.")
        return 1

    try:
        base = load(baseline_path)
        cur = load(current_path)
        ok, report = compare(cur, base, args.threshold, args.normalize_by)
        print(report)
        return 0 if ok else 2
    except Exception as e:
        print(f"[ci_compare] Failed to compare: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
