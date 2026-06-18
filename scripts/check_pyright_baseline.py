"""Compare the current pyright error count against the committed baseline.

Exits 0 when current errors <= baseline, 1 when the count has grown.
Usage: uv run python scripts/check_pyright_baseline.py <pyright-json-file>
"""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: check_pyright_baseline.py <pyright-output.json>", file=sys.stderr)
        sys.exit(2)

    output_path = pathlib.Path(sys.argv[1])
    baseline_path = pathlib.Path(__file__).parent.parent / ".github" / "pyright-baseline.txt"

    data = json.loads(output_path.read_text())
    current = int(data["summary"]["errorCount"])
    baseline = int(baseline_path.read_text().strip())

    print(f"pyright errors: current={current}  baseline={baseline}")

    if current > baseline:
        print(
            f"FAIL: error count increased by {current - baseline}. "
            "Fix new type errors or update the baseline with scripts/update_pyright_baseline.sh",
            file=sys.stderr,
        )
        sys.exit(1)

    if current < baseline:
        print(
            f"NOTE: error count decreased by {baseline - current}. Consider updating the baseline."
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
