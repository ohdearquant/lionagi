"""lionbench v0 campaign config assembly (DESIGN.md §2.2 campaign.json, §8
item 1). Builds a fingerprinted ``Campaign`` record from explicit inputs —
no hidden env magic — plus small git/price-table introspection helpers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from v0_schema import Campaign, hash_json


def hash_price_table(price_table: dict[str, Any]) -> str:
    """Content hash of a resolved price table (model -> price tuple/list), so
    ``campaign.json`` changes whenever the *effective* prices change even if
    the override source (env var vs. default table) does not."""
    return hash_json(price_table)


def git_sha_and_dirty(repo_root: str | Path) -> tuple[str, bool]:
    """Best-effort git SHA + dirty-worktree state for provenance. Returns
    ``("", False)`` if the path is not a git checkout or git is unavailable —
    campaign assembly never fails because of provenance introspection."""
    root = Path(repo_root)
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — git resolved on PATH by design
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607 — git resolved on PATH by design
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return sha, bool(status.strip())
    except (OSError, subprocess.CalledProcessError):
        return "", False


def assemble_campaign(
    campaign_id: str,
    *,
    model: str,
    model_revision: str,
    profile_hash: str,
    price_table: dict[str, Any],
    image_digest: str,
    isolation_requested: str,
    isolation_effective: str,
    lionagi_version: str,
    khive_version: str,
    cli_version: str,
    created_at: str,
    repo_root: str | Path | None = None,
    dataset_hashes: dict[str, str] | None = None,
    seed: int = 0,
    parameters: dict[str, Any] | None = None,
) -> Campaign:
    """Build the frozen ``Campaign`` record a campaign directory is stamped
    with. ``fingerprint`` (computed on ``Campaign``) changes whenever model,
    profile hash, price table, sandbox image, or isolation setting changes —
    the acceptance bar for DESIGN.md §8 item 1."""
    git_sha, git_dirty = git_sha_and_dirty(repo_root) if repo_root is not None else ("", False)
    return Campaign(
        campaign_id=campaign_id,
        git_sha=git_sha,
        git_dirty=git_dirty,
        lionagi_version=lionagi_version,
        khive_version=khive_version,
        cli_version=cli_version,
        image_digest=image_digest,
        model=model,
        model_revision=model_revision,
        profile_hash=profile_hash,
        price_table_hash=hash_price_table(price_table),
        isolation_requested=isolation_requested,
        isolation_effective=isolation_effective,
        seed=seed,
        dataset_hashes=dict(dataset_hashes or {}),
        parameters=dict(parameters or {}),
        created_at=created_at,
    )
