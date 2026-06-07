#!/usr/bin/env python3
"""
Benchmark Report Generator
--------------------------
Reads benchmark CSV/JSON and generates a comprehensive Markdown report with:
- Ranked tables per category & mode
- Percentage deltas vs best performer
- LionAGI vs #2 advantage callouts
- Feature parity matrix with documentation sources
"""

import argparse
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

# -----------------------------
# Reporting configuration
# -----------------------------
# Any category/mode with a best median below this is considered timer-noise/lazy-load dominated.
TINY_BASELINE_THRESHOLD_MS = 1.0
# Exclude these (category, mode) pairs from headline composites and headline summary
EXCLUDE_FROM_COMPOSITE = {("imports", "cold")}
# Headline composite uses only cold paths that users actually feel (no imports)
HEADLINE_CATEGORIES = [
    "basic_primitives",
    "orchestrators",
    "workflow_setup",
    "data_processing",
]
HEADLINE_MODES = ["cold"]
# Alternative composite without 'data_processing' (more conservative)
HEADLINE_CATEGORIES_NO_DP = [
    "basic_primitives",
    "orchestrators",
    "workflow_setup",
]


def pick_latest(pattern):
    paths = sorted(glob.glob(pattern))
    return paths[-1] if paths else None


def load_summary(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            # Coerce numeric fields
            for k in [
                "mean_ms",
                "median_ms",
                "stdev_ms",
                "mad_ms",
                "trimmed_mean_ms",
                "p95_ms",
                "min_ms",
                "max_ms",
                "mean_rss_mb",
                "max_rss_mb",
                "mean_uss_mb",
                "max_uss_mb",
            ]:
                if r.get(k) not in (None, "", "None"):
                    r[k] = float(r[k])
            rows.append(r)
    return rows


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def percent_delta(a, b):
    # delta of a vs b, where smaller is better (time/mem). If b==0, return 0.
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def format_delta(val, best):
    """
    Smarter delta formatting:
      - If best < 1 ms, use absolute delta + multiplier (avoids huge %).
      - Else show percentage.
    """
    if best < 1.0:
        # guard division by tiny numbers; treat as multiplier
        mult = float("inf") if best == 0 else (val / best)
        abs_ms = val - best
        mult_s = "∞" if mult == float("inf") else f"×{mult:.0f}"
        return f"+{abs_ms:.1f} ms ({mult_s})"
    return f"{percent_delta(val, best):+.1f}%"


def fmt_pct(x):
    s = f"{x:+.1f}%"
    # align sign and zero
    return s.replace("+0.0%", "+0.0%").replace("-0.0%", "0.0%")


def md_table(headers, rows):
    out = []
    out.append("| " + " | ".join(headers) + " |")
    out.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def rank_block(rows, metric_key, lion_name):
    # Sort ascending (lower is better)
    rows_sorted = sorted(rows, key=lambda r: (r.get(metric_key, float("inf")), r["framework"]))
    if not rows_sorted:
        return rows_sorted, None, None
    best_val = rows_sorted[0].get(metric_key, float("inf"))
    # Identify #2 for lion advantage callout
    second_val = None
    lion_val = None
    for i, r in enumerate(rows_sorted):
        if i == 1:
            second_val = r.get(metric_key, float("inf"))
        if r["framework"] == lion_name:
            lion_val = r.get(metric_key, float("inf"))
    return rows_sorted, best_val, (lion_val, second_val)


def feature_parity_section():
    # Keep aligned with the harness' apples-to-apples design.
    rows = [
        (
            "lionagi",
            "Session()",
            "N/A",
            "Yes",
            "No",
            "No",
            "Minimal runtime container",
        ),
        (
            "langgraph",
            "StateGraph(...).compile()",
            "Yes",
            "Yes",
            "No",
            "No",
            "One-node identity graph (START→node→END)",
        ),
        (
            "langchain_core",
            "PromptTemplate | RunnableLambda",
            "Yes",
            "Yes",
            "No",
            "No",
            "LCEL chain; no community deps",
        ),
        (
            "llamaindex",
            "SimpleChatEngine(MockLLM)",
            "Yes",
            "Yes",
            "MockLLM",
            "No",
            "Built-in MockLLM; no network",
        ),
        (
            "autogen",
            "ConversableAgent(llm_config=False)",
            "N/A",
            "Yes",
            "No",
            "No",
            "Single agent; LLM disabled",
        ),
    ]
    headers = [
        "Framework",
        "Object",
        "Compiled?",
        "Core-only",
        "LLM?",
        "Network?",
        "Notes",
    ]
    body = [[*r] for r in rows]
    return md_table(headers, body)


DOC_LINKS = {
    # LangGraph graph API and compile semantics
    "langgraph_graphs": "https://langchain-ai.github.io/langgraph/reference/graphs/",
    "langgraph_use_graph_api": "https://docs.langchain.com/oss/python/langgraph/use-graph-api",
    "langgraph_low_level": "https://langchain-ai.github.io/langgraph/concepts/low_level/",
    # LangChain core PromptTemplate / RunnableLambda (LCEL)
    "lc_prompttemplate": "https://python.langchain.com/api_reference/core/prompts/langchain_core.prompts.prompt.PromptTemplate.html",
    "lc_runnable_lambda": "https://python.langchain.com/api_reference/core/runnables/langchain_core.runnables.base.RunnableLambda.html",
    # LlamaIndex SimpleChatEngine + MockLLM
    "llama_simple_engine": "https://docs.llamaindex.ai/en/stable/api_reference/chat_engines/simple/",
    "llama_mockllm_usage": "https://docs.llamaindex.ai/en/stable/understanding/evaluating/cost_analysis/",
    # AutoGen ConversableAgent llm_config=False
    "autogen_conversable": "https://microsoft.github.io/autogen/0.2/docs/reference/agentchat/conversable_agent/",
    # psutil RSS / USS
    "psutil_mem": "https://psutil.readthedocs.io/",
}


def geomean(values):
    vals = [v for v in values if v is not None and v > 0]
    if not vals:
        return float("inf")
    s = 0.0
    for v in vals:
        s += math.log(v)
    return math.exp(s / len(vals))


def build_composite(by_cat_mode, categories, modes):
    """
    Returns a list of dicts:
      {framework, gmean_ms, avg_rss_mb, avg_uss_mb, n}
    across selected (category, mode) pairs.
    """
    # Collect all frameworks present in the selected sections
    frameworks = set()
    for (cat, mode), sect in by_cat_mode.items():
        if cat in categories and mode in modes and (cat, mode) not in EXCLUDE_FROM_COMPOSITE:
            for r in sect:
                frameworks.add(r["framework"])

    results = []
    for fw in sorted(frameworks):
        medians, rss_vals, uss_vals = [], [], []
        n = 0
        for cat in categories:
            for mode in modes:
                if (cat, mode) in EXCLUDE_FROM_COMPOSITE:
                    continue
                sect = by_cat_mode.get((cat, mode), [])
                row = next((x for x in sect if x["framework"] == fw), None)
                if row:
                    medians.append(row.get("median_ms"))
                    rss_vals.append(row.get("mean_rss_mb"))
                    uss_vals.append(row.get("mean_uss_mb"))
                    n += 1
        if n > 0:
            results.append(
                {
                    "framework": fw,
                    "gmean_ms": geomean(medians),
                    "avg_rss_mb": ((sum(rss_vals) / len(rss_vals)) if rss_vals else float("nan")),
                    "avg_uss_mb": ((sum(uss_vals) / len(uss_vals)) if uss_vals else float("nan")),
                    "n": n,
                }
            )
    results.sort(key=lambda x: (x["gmean_ms"], x["framework"]))
    return results


def composite_section(out, by_cat_mode, title, categories, modes, lion_name):
    rows = build_composite(by_cat_mode, categories, modes)
    if not rows:
        return
    out.append(f"## {title}")
    out.append("")
    hdrs = [
        "Rank",
        "Framework",
        "Composite median (gmean, ms)",
        "Avg RSS (MB)",
        "Avg USS (MB)",
        "Pairs",
    ]
    body = []
    lion_g = None
    second_g = None
    for i, r in enumerate(rows, start=1):
        if r["framework"] == lion_name:
            lion_g = r["gmean_ms"]
        if i == 2:
            second_g = r["gmean_ms"]
        body.append(
            [
                str(i),
                r["framework"],
                f"{r['gmean_ms']:.1f}",
                (f"{r['avg_rss_mb']:.1f}" if not math.isnan(r["avg_rss_mb"]) else "—"),
                (f"{r['avg_uss_mb']:.1f}" if not math.isnan(r["avg_uss_mb"]) else "—"),
                str(r["n"]),
            ]
        )
    out.append(md_table(hdrs, body))
    out.append("")
    if lion_g is not None and second_g is not None:
        adv = percent_delta(second_g, lion_g)  # how much slower #2 is vs lion composite
        out.append(f"> **LionAGI composite advantage:** {adv:+.1f}% vs #2.")
        out.append("")


def main():
    ap = argparse.ArgumentParser(description="Generate benchmark report from CSV/JSON outputs")
    ap.add_argument(
        "--summary",
        required=False,
        help="Path or glob to summary CSV (default: benchmark_summary_*.csv)",
    )
    ap.add_argument(
        "--json",
        required=False,
        help="(Optional) Path or glob to JSON bundle for metadata",
    )
    ap.add_argument("--out", default="report.md", help="Output markdown file")
    ap.add_argument("--lion", default="lionagi", help="Canonical name for LionAGI rows")
    args = ap.parse_args()

    summary_path = pick_latest(args.summary or "benchmark_summary_*.csv")
    json_path = pick_latest(args.json or "benchmark_results_*.json")
    if not summary_path:
        print("No summary CSV found. Run the benchmark first.", file=sys.stderr)
        sys.exit(2)

    rows = load_summary(summary_path)
    meta = load_json(json_path) if json_path else None

    # Group by (category, mode)
    by_cat_mode = defaultdict(list)
    for r in rows:
        by_cat_mode[(r["category"], r["mode"])].append(r)

    out = []
    out.append("# Apples-to-Apples Agentic Framework Benchmark Report")
    out.append("")
    out.append(f"- Generated: {datetime.now().isoformat(timespec='seconds')}")
    out.append(f"- Source summary: `{os.path.basename(summary_path)}`")
    if meta:
        md = meta.get("metadata", {})
        out.append(
            f"- Python: {md.get('python_version', '?')}  |  Runs per case: {md.get('runs', '?')}"
        )
        out.append(f"- Harness: {md.get('description', '')}")
    out.append("")

    # For each category and mode, rank by median_ms and compute % deltas
    for cat, mode in sorted(by_cat_mode.keys()):
        sect = by_cat_mode[(cat, mode)]
        ranked, best_val, lion_pair = rank_block(sect, "median_ms", args.lion)
        out.append(f"## {cat} — **{mode}**")
        out.append("")
        hdrs = [
            "Rank",
            "Framework",
            "Median (ms)",
            "p95 (ms)",
            "Range (ms)",
            "RSS μ/max (MB)",
            "USS μ/max (MB)",
            "Δ vs best",
        ]
        body = []
        for i, r in enumerate(ranked, start=1):
            rng = f"{r['min_ms']:.1f}-{r['max_ms']:.1f}"
            delta = format_delta(r["median_ms"], best_val)
            body.append(
                [
                    str(i),
                    r["framework"],
                    f"{r['median_ms']:.1f}",
                    f"{r['p95_ms']:.1f}",
                    rng,
                    f"{r['mean_rss_mb']:.1f}/{r['max_rss_mb']:.1f}",
                    f"{r['mean_uss_mb']:.1f}/{r['max_uss_mb']:.1f}",
                    delta,
                ]
            )
        out.append(md_table(hdrs, body))
        out.append("")

        # LionAGI vs #2 callout if Lion present
        if lion_pair and not math.isinf(best_val):
            lion_val, second_val = lion_pair
            if lion_val is not None and second_val is not None:
                adv = percent_delta(second_val, lion_val)  # how much slower #2 is vs lion
                if adv > -1e-9:  # #2 >= lion
                    out.append(f"> **LionAGI advantage:** median {adv:+.1f}% vs #2 in this table.")
                    out.append("")

        # Flag tiny-baseline sections (e.g., imports ~0.1 ms)
        if best_val is not None and best_val < TINY_BASELINE_THRESHOLD_MS:
            out.append(
                f"> ⚠️ Baseline < {TINY_BASELINE_THRESHOLD_MS:.1f} ms; results dominated by timer granularity and lazy imports. Excluded from headline composites."
            )
            out.append("")

    # Composite sections (headline)
    composite_section(
        out,
        by_cat_mode,
        "Cold Composite (excludes *imports*)",
        HEADLINE_CATEGORIES,
        HEADLINE_MODES,
        args.lion,
    )
    composite_section(
        out,
        by_cat_mode,
        "Cold Composite (excludes *imports* and *data_processing*)",
        HEADLINE_CATEGORIES_NO_DP,
        HEADLINE_MODES,
        args.lion,
    )

    # Summary statistics section
    out.append("## Summary Statistics")
    out.append("")

    # Calculate overall performance summary
    def positions(by_cat_mode, categories=None, modes=None, exclude_pairs=None):
        wins = 0
        total = 0
        pos = []
        for (cat, mode), sect in sorted(by_cat_mode.items()):
            if categories and cat not in categories:
                continue
            if modes and mode not in modes:
                continue
            if exclude_pairs and (cat, mode) in exclude_pairs:
                continue
            ranked, _, _ = rank_block(sect, "median_ms", args.lion)
            for i, r in enumerate(ranked, start=1):
                if r["framework"] == args.lion:
                    pos.append(i)
                    wins += 1 if i == 1 else 0
                    total += 1
                    break
        avg = (sum(pos) / len(pos)) if pos else float("nan")
        return wins, total, avg

    # Headline (cold, no imports)
    hw, ht, ha = positions(
        by_cat_mode,
        categories=HEADLINE_CATEGORIES,
        modes=HEADLINE_MODES,
        exclude_pairs=EXCLUDE_FROM_COMPOSITE,
    )
    if ht > 0:
        out.append(
            f"- **Headline (cold, excl. imports)**: {hw}/{ht} first-place finishes  |  **Avg rank:** {ha:.1f}"
        )

    # All categories (for completeness)
    aw, at, aa = positions(by_cat_mode)
    if at > 0:
        out.append(
            f"- **All categories**: {aw}/{at} first-place finishes  |  **Avg rank:** {aa:.1f}"
        )
    out.append("")

    # Feature parity (fixed)
    out.append("## Feature Parity Matrix")
    out.append("")
    out.append("This matrix demonstrates the equivalence of test cases across frameworks:")
    out.append("")
    out.append(feature_parity_section())
    out.append("")
    out.append("### Documentation Sources")
    out.append("")
    out.append("The benchmark design follows official documentation from each framework:")
    out.append("")
    out.append(
        "- **LangGraph**: [Graph API & compile()](https://langchain-ai.github.io/langgraph/reference/graphs/)"
    )
    out.append(
        "- **LangChain Core**: [PromptTemplate](https://python.langchain.com/api_reference/core/prompts/langchain_core.prompts.prompt.PromptTemplate.html) | [RunnableLambda](https://python.langchain.com/api_reference/core/runnables/langchain_core.runnables.base.RunnableLambda.html)"
    )
    out.append(
        "- **LlamaIndex**: [SimpleChatEngine](https://docs.llamaindex.ai/en/stable/api_reference/chat_engines/simple/) | [MockLLM](https://docs.llamaindex.ai/en/stable/understanding/evaluating/cost_analysis/)"
    )
    out.append(
        "- **AutoGen**: [ConversableAgent](https://microsoft.github.io/autogen/0.2/docs/reference/agentchat/conversable_agent/) (llm_config=False)"
    )
    out.append("- **Memory Metrics**: [psutil RSS/USS](https://psutil.readthedocs.io/)")
    out.append("")

    # Methodology note
    out.append("## Methodology")
    out.append("")
    out.append("### Measurement Approach")
    out.append("- **Cold Mode**: Full import + object construction (serverless scenario)")
    out.append("- **Construct Mode**: Object construction only (post-import, per-request cost)")
    out.append("- **Memory**: RSS (Resident Set Size) and USS (Unique Set Size) deltas")
    out.append("- **Statistics**: Median, P95, MAD, trimmed mean for robustness against outliers")
    out.append("")
    out.append("### Environmental Controls")
    out.append("- CPU pinning for reduced scheduler noise")
    out.append("- Deterministic hashing (PYTHONHASHSEED=0)")
    out.append("- Module cache clearing between runs")
    out.append("- Process isolation per measurement")
    out.append("")

    with open(args.out, "w") as f:
        f.write("\n".join(out))

    print(f"✅ Wrote {args.out}")
    # Also echo a short console preview
    print("\nReport Preview:")
    print("-" * 50)
    print("\n".join(out[:30]), "\n...")
    print("-" * 50)
    print(f"Full report: {args.out}")


if __name__ == "__main__":
    main()
