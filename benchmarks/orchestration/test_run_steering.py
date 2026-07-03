"""Unit tests pinning run_steering.py's provider-dispatch defaults (ADR-0088)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_steering  # noqa: E402


def test_default_providers_is_claude_code_only():
    """No --providers flag must dispatch claude_code only, never codex/gemini/api."""
    args = run_steering._build_parser().parse_args([])
    assert args.providers == ["claude_code"]


def test_explicit_providers_flag_overrides_default():
    args = run_steering._build_parser().parse_args(["--providers", "codex", "gemini"])
    assert args.providers == ["codex", "gemini"]


def test_smoke_flag_still_available_alongside_default_providers():
    args = run_steering._build_parser().parse_args(["--smoke"])
    assert args.smoke is True
    assert args.providers == ["claude_code"]
