from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..services import runs as runs_svc

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/")
async def list_runs(
    page: int = Query(default=1, ge=1, description="1-based page number"),
    per_page: int = Query(default=20, ge=1, le=5000, description="Rows per page"),
    status: list[str] | None = Query(default=None, description="Repeated status filter"),  # noqa: B008
    # ADR-0005: renamed from ?worker= to ?playbook= — "worker" is
    # not in lionagi's Studio vocabulary per ADR-0005.
    playbook: str | None = Query(
        default=None, description="Case-insensitive playbook contains filter"
    ),
    project: str | None = Query(default=None, description="Exact project name filter (ADR-0026)"),
) -> dict[str, Any]:
    runs = await runs_svc.list_runs(playbook=playbook, status=status, project=project)
    return runs_svc.paginate_runs(runs, page=page, per_page=per_page)


@router.get("/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    # get_run reads from StateDB (same source as list_runs); no thread offload needed.
    run = await runs_svc.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


# ADR-0008: removed /api/runs/{id}/events SSE route — it read
# stream/*.buffer.jsonl files forbidden by ADR-0004.  Live monitoring uses
# /api/sessions/{id}/stream instead.
#
# ADR-0008 Write Policy: removed POST /{run_id}/rerun and
# DELETE /{run_id} stub routes.  Run data is explicitly read-only per
# ADR-0008.  Re-running requires switching to the terminal (`li play ...`).
# If re-run support is ever reconsidered, it requires an ADR-0008 amendment
# before the route is added back.
