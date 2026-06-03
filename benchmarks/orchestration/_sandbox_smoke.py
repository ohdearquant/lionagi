"""End-to-end smoke: one lionagi coding agent fixing a bug INSIDE a Daytona
sandbox, with its signals streamed live to the host. No SWE-bench yet — a
trivial planted bug — to validate the whole transport before spending on real
instances.

    uv run python benchmarks/orchestration/_sandbox_smoke.py

Requires DAYTONA_API_KEY + DEEPSEEK_API_KEY in env (.env).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lionagi.tools.daytona import DaytonaSandbox, ensure_snapshot  # noqa: E402

SNAPSHOT = "lionagi-bench-py312-v2"
WHEEL = Path(__file__).resolve().parents[2] / "dist" / "lionagi-0.26.14-py3-none-any.whl"
ENTRY = Path(__file__).resolve().parent / "suites" / "swebench" / "_sandbox_entry.py"

BUGGY = "def add(a, b):\n    return a - b  # bug: should be +\n"
TEST = (
    "from buggy import add\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n    assert add(10, 5) == 15\n"
)


def _sig(line: str) -> None:
    try:
        obj = json.loads(line)
    except Exception:
        return
    t = obj.pop("t", "?")
    rest = " ".join(f"{k}={v}" for k, v in obj.items())
    print(f"    ◆ {t:18} {rest}", flush=True)


async def main() -> None:
    assert WHEEL.exists(), f"build the wheel first: {WHEEL}"
    assert os.environ.get("DEEPSEEK_API_KEY"), "DEEPSEEK_API_KEY unset"

    t0 = time.monotonic()
    print("ensuring snapshot (first build is slow — installs the dep tree) …", flush=True)
    await ensure_snapshot(SNAPSHOT, on_logs=lambda m: print("  [snap]", m.rstrip(), flush=True))
    print(f"snapshot ready in {time.monotonic() - t0:.1f}s", flush=True)

    env = {"DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"]}
    async with await DaytonaSandbox.create(snapshot=SNAPSHOT, env=env) as sb:
        home = await sb.home_dir()
        print(f"sandbox {sb.id[:8]} up; home={home}", flush=True)

        # overlay this-branch lionagi (snapshot has the released deps).
        # pip requires the canonical PEP 427 wheel filename — keep WHEEL.name.
        await sb.upload_file(WHEEL, f"{home}/{WHEEL.name}")
        r = await sb.exec(
            f"pip install --no-deps --force-reinstall {home}/{WHEEL.name}", timeout=300
        )
        assert r.ok, f"pip overlay failed:\n{r.stdout}"
        v = await sb.exec("python -c 'import lionagi; print(lionagi.__version__)'")
        print(f"in-sandbox lionagi: {v.stdout.strip()}", flush=True)

        # trivial buggy project under git
        proj = f"{home}/proj"
        await sb.mkdir(proj)
        await sb.write_text(BUGGY, f"{proj}/buggy.py")
        await sb.write_text(TEST, f"{proj}/test_buggy.py")
        setup = await sb.exec(
            "git init -q && git add -A && git -c user.email=a@b.c -c user.name=x "
            "commit -qm base && python -m pytest -q test_buggy.py",
            cwd=proj,
        )
        print(f"baseline tests (expect FAIL): exit={setup.exit_code}", flush=True)

        # upload the in-sandbox driver + its spec
        await sb.upload_file(ENTRY, f"{home}/_sandbox_entry.py")
        spec = {
            "repo_path": proj,
            "model": "deepseek/deepseek-chat",
            "instruction": (
                "The test test_buggy.py fails. Find and fix the bug in buggy.py so "
                "the tests pass. Run pytest to confirm."
            ),
            "max_extensions": 15,
            "result_path": f"{home}/result.json",
            "control_path": f"{home}/control",
            "env": {"DEEPSEEK_API_KEY": os.environ["DEEPSEEK_API_KEY"]},
        }
        await sb.write_text(json.dumps(spec), f"{home}/spec.json")

        print("running in-sandbox agent (live signals) …", flush=True)
        buf = ""

        def on_out(chunk: str) -> None:
            nonlocal buf
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.startswith("@@SIG@@ "):
                    _sig(line[len("@@SIG@@ ") :])
                elif line.strip():
                    print("    │", line, flush=True)

        def on_err(chunk: str) -> None:
            for line in chunk.splitlines():
                if line.strip():
                    print("    ✗", line, flush=True)

        t1 = time.monotonic()
        code = await sb.exec_stream(
            f"python {home}/_sandbox_entry.py {home}/spec.json",
            on_stdout=on_out,
            on_stderr=on_err,
        )
        print(f"agent exited {code} in {time.monotonic() - t1:.1f}s", flush=True)

        result = json.loads(await sb.read_text(f"{home}/result.json"))
        print("\n=== RESULT ===")
        print("status:", result["status"])
        print("usage:", result["usage"])
        print("diff:\n" + (result["diff"] or "(empty)"))

        verify = await sb.exec("python -m pytest -q test_buggy.py", cwd=proj)
        print(f"\npost-fix tests: exit={verify.exit_code}")
        print(verify.stdout.strip()[-400:])

    print(f"\ntotal {time.monotonic() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
