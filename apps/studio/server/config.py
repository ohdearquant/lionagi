from __future__ import annotations

import os
from pathlib import Path

STUDIO_PORT: int = int(os.environ.get("LIONAGI_STUDIO_PORT", "8765"))
HOST: str = os.environ.get("LIONAGI_STUDIO_HOST", "127.0.0.1")
DATA_ROOT: Path = Path(os.environ.get("LIONAGI_DATA_ROOT", "~/.lionagi")).expanduser()
SHOWS_ROOT: Path = Path(
    os.environ.get("LIONAGI_SHOWS_ROOT", "~/khive-work/shows")
).expanduser()

_raw_origins = os.environ.get("CORS_ORIGINS", "")
CORS_ORIGINS: list[str] = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins
    else ["http://localhost:5173", "http://localhost:3000", "http://localhost:3765"]
)
