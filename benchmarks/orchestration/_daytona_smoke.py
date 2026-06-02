"""One-shot Daytona connectivity check. Run once, delete the sandbox, report.

uv run python benchmarks/orchestration/_daytona_smoke.py
"""

from __future__ import annotations

import asyncio
import time

from dotenv import load_dotenv

load_dotenv()

from daytona import AsyncDaytona  # noqa: E402


async def main() -> None:
    t0 = time.monotonic()
    async with AsyncDaytona() as dt:
        print("client up; creating sandbox …", flush=True)
        sb = await dt.create()
        print(f"created {sb.id} in {time.monotonic() - t0:.1f}s", flush=True)
        try:
            r = await sb.process.exec("python3 --version && uname -m && nproc")
            print("exec exit:", r.exit_code)
            print(r.result.strip())

            root = await sb.get_user_root_dir()
            print("root dir:", root)

            t1 = time.monotonic()
            await sb.git.clone(
                "https://github.com/pallets/click.git",
                f"{root}/click",
                commit_id=None,
                branch="main",
            )
            print(f"git clone click in {time.monotonic() - t1:.1f}s", flush=True)
            st = await sb.git.status(f"{root}/click")
            print("cloned branch:", st.current_branch)

            ls = await sb.process.exec("ls", cwd=f"{root}/click")
            print("repo top-level entries:", len(ls.result.strip().splitlines()))

            data = await sb.fs.download_file(f"{root}/click/README.md")
            print("downloaded README bytes:", len(data) if data else 0)
        finally:
            await sb.delete()
            print(f"deleted sandbox; total {time.monotonic() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
