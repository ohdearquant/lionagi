"""Seeded-daemon harness for the Lion Studio Playwright e2e suite.

Everything in this package runs the Studio FastAPI daemon against a throwaway
temp directory with a freshly seeded state db -- it never reads or writes the
real ``~/.lionagi``. See ``harness.py`` for the safety invariant and
``fixtures.py`` for the deterministic seed data.
"""

from __future__ import annotations
