"""Pluggable provider dispatch: claude_code, codex, gemini, one API model (ADR-0088)."""

from __future__ import annotations

from lionagi import iModel
from lionagi.cli._providers import build_imodel_from_spec

# CLI-subprocess families resolve through the CLI provider table
# (build_imodel_from_spec -> endpoint="query_cli"); "api" is a plain iModel
# with the default endpoint="chat" — a real API call, not a subprocess.
_CLI_SPECS = {
    "claude_code": "claude-code/sonnet",
    "codex": "codex/gpt-5.4-mini",
    "gemini": "gemini-code/gemini-3.5-flash",
}
_API_SPECS = {
    "api": "openai/gpt-4.1-mini",
}

PROVIDER_KEYS = tuple(_CLI_SPECS) + tuple(_API_SPECS)


def build_imodel(provider_key: str) -> iModel:
    """Build the iModel for one provider family, by its harness-facing key."""
    if provider_key in _CLI_SPECS:
        return build_imodel_from_spec(_CLI_SPECS[provider_key], yolo=True)
    if provider_key in _API_SPECS:
        return iModel(model=_API_SPECS[provider_key])
    raise ValueError(f"unknown provider {provider_key!r}; known: {PROVIDER_KEYS}")
