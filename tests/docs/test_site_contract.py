"""High-level contracts for the published documentation site.

These checks deliberately pin discoverability and local build inputs rather
than prose formatting. They should fail when a shipped product surface can no
longer be found or when MkDocs references a file that the site cannot serve.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
MKDOCS = ROOT / "mkdocs.yml"
UNPUBLISHED_STALE_PAGES = {
    "cookbook/reliable-artifact-production.md",
    "cookbook/reliable-multi-leg-runs.md",
    "cookbook/reliable-recurring-runs.md",
    "cookbook/reliable-review-runs.md",
    "reference/migrate-logs.md",
    "reference/outcomes-work.md",
}


def _configured_local_assets() -> list[Path]:
    """Return local files listed in the extra_css/javascript config blocks."""
    assets: list[Path] = []
    section: str | None = None
    for raw_line in MKDOCS.read_text().splitlines():
        if raw_line in {"extra_css:", "extra_javascript:"}:
            section = raw_line.removesuffix(":")
            continue
        if section and raw_line and not raw_line.startswith(" "):
            section = None
        if section and raw_line.startswith("  - "):
            value = raw_line.removeprefix("  - ").strip()
            if not value.startswith(("https://", "http://")):
                assets.append(DOCS / value)
    return assets


def test_archive_is_not_published() -> None:
    config = MKDOCS.read_text()
    assert "exclude_docs:" in config
    assert "_archive/" in config
    assert "overrides/" in config


def test_every_configured_local_asset_exists() -> None:
    missing = [
        str(path.relative_to(ROOT)) for path in _configured_local_assets() if not path.is_file()
    ]
    assert not missing, f"MkDocs references missing local assets: {missing}"


def test_stale_pages_are_excluded_from_the_published_site() -> None:
    config = MKDOCS.read_text()
    nav = config.split("\nnav:\n", maxsplit=1)[1].split("\nextra:\n", maxsplit=1)[0]
    for page in UNPUBLISHED_STALE_PAGES:
        assert page in config
        assert page not in nav


def test_cli_reference_covers_every_shipped_surface() -> None:
    from lionagi.cli.main import _COMMAND_REGISTRY

    reference = (DOCS / "cli-reference.md").read_text().lower()
    aliases = {
        "orchestrate": ("li orchestrate", "li o "),
    }
    for spec in _COMMAND_REGISTRY:
        needles = aliases.get(spec.name, (f"li {spec.name}",))
        assert any(needle in reference for needle in needles), (
            f"cli-reference.md does not make `li {spec.name}` discoverable"
        )

    for hidden_surface in ("li play", "li skill", "li wait"):
        assert hidden_surface in reference, f"Missing hidden CLI surface: {hidden_surface}"


def test_reference_identifies_the_current_release_line() -> None:
    api_index = (DOCS / "api" / "index.md").read_text()
    assert "0.28" in api_index
    assert "lionagi 0.22.6" not in api_index.lower()
