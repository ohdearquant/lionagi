from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

_DURATION_FIELDS = ("min", "mean", "median", "max")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_stat(name: str, side: str, stat: dict) -> str | None:
    """Return an error message if `stat` (one arm's result entry for
    scenario `name`) is not safe to compute a delta from, else None.

    Without this, a missing or malformed numeric field flows straight into
    the delta arithmetic below and silently produces a meaningless-but-
    passing result: `stat.get("median", 0)` on a missing key defaults to
    0.0 instead of failing, and a literal NaN -- valid JSON under the
    stdlib parser's non-standard NaN/Infinity extension, and directly
    reachable from a malformed or corrupted producer -- makes `delta >
    threshold` evaluate to False for ANY threshold, since every comparison
    against NaN is False in Python. Either way a broken measurement would
    pass the regression gate instead of failing it, so every numeric field
    is checked for presence, correct type, and finiteness before any delta
    is computed.
    """
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
        base = base_results.get(name)
        if not base:
            lines.append(f"- {name}: no baseline; skipping")
            continue
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
        # Belt-and-suspenders: ci_check_provenance.py is meant to catch an
        # empty-or-disjoint scenario set before this ever runs, but if that
        # step is ever skipped, reordered, or removed, this function would
        # otherwise silently skip every scenario as "no baseline" above and
        # return ok=True -- a gate that ran, compared nothing, and reported
        # success. Zero comparisons is always a failure here, independent
        # of why (empty baseline, empty current, or disjoint scenario
        # names), and applies even without --normalize-by.
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
        type=float,
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
        print(f"[ci_compare] Baseline missing: {baseline_path}. Skipping gating.")
        return 0
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
