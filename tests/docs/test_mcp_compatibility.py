"""The retired MCP surface is listed consistently in docs and dispatch tests."""

import re
from pathlib import Path

from tests.mcp._removed_surface import REMOVED_VERB_ALIASES

ROOT = Path(__file__).resolve().parents[2]


def test_retired_mcp_verb_documentation_matches_the_dispatch_witness():
    reference = (ROOT / "docs/reference/mcp-server.md").read_text(encoding="utf-8")
    compatibility = reference.partition("\n## Compatibility\n")[2].partition("\n## ")[0]
    rows = re.findall(
        r"^\|[ \t]*`([^`\n]+)`[ \t]*\|[ \t]*`([^`\n]+)`[ \t]*\|[ \t]*$",
        compatibility,
        flags=re.MULTILINE,
    )
    assert len(rows) == len(REMOVED_VERB_ALIASES)
    assert dict(rows) == REMOVED_VERB_ALIASES

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release = re.search(r"^## \[0\.37\.0\] - (\d{4}-\d{2}-\d{2})$", changelog, re.MULTILINE)
    assert release is not None
    removed = changelog[release.end() :].partition("\n## ")[0].partition("\n### Removed\n")[2]
    aliases = re.search(r"one-tool-per-operation surface \(([^)]+)\) inside `ops`", removed)
    assert aliases is not None
    names = re.findall(r"`([^`]+)`", aliases[1])
    assert len(names) == len(REMOVED_VERB_ALIASES)
    assert set(names) == set(REMOVED_VERB_ALIASES)

    adr = (ROOT / "docs/adr/ADR-0066-li-mcp-v2-verb-surface.md").read_text(encoding="utf-8")
    status = adr.partition("\n## Implementation status\n")[2].partition("\n## ")[0]
    assert f"**{release[1]} (0.37.0).**" in status
