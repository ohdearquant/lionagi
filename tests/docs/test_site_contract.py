"""High-level contracts for the published documentation site.

These checks deliberately pin discoverability and local build inputs rather
than prose formatting. They should fail when a shipped product surface can no
longer be found or when MkDocs references a file that the site cannot serve.
"""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

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
# Repository READMEs link to the published guides, so the retired-guide guard
# has to read them too; docs/ alone would miss exactly the files this change
# edits.
LINK_GUARD_EXTRA_PAGES = {
    "README.md",
    "apps/studio/README.md",
    "apps/studio/frontend/README.md",
}
# The retired page and the extensionless directory MkDocs published for it.
RETIRED_STUDIO_GUIDE_PATHS = frozenset(
    {(DOCS / "guides/studio.md").resolve(), (DOCS / "guides" / "studio").resolve()}
)
STUDIO_PAGES = {
    "guides/studio-getting-started.md",
    "guides/studio-operator.md",
    "guides/studio-schedules.md",
    "concepts/studio-model.md",
    "reference/studio-surfaces.md",
}


def _nav_targets(node: Node) -> set[str]:
    """Read navigation values, never YAML keys, comments, or unrelated settings."""
    if isinstance(node, ScalarNode):
        return {node.value}
    if isinstance(node, SequenceNode):
        return set().union(*(_nav_targets(child) for child in node.value))
    if isinstance(node, MappingNode):
        return set().union(*(_nav_targets(value) for _, value in node.value))
    return set()


def test_studio_pages_are_in_site_navigation() -> None:
    # Compose nodes rather than constructing tagged MkDocs Python objects.
    config = yaml.compose(MKDOCS.read_text())
    assert isinstance(config, MappingNode)
    nav = next(value for key, value in config.value if key.value == "nav")
    missing = STUDIO_PAGES - _nav_targets(nav)
    assert not missing, f"Studio pages missing from MkDocs navigation: {sorted(missing)}"
    assert all((DOCS / page).is_file() for page in STUDIO_PAGES)


def _link_guard_pages() -> list[Path]:
    """Every page the retired-guide guard reads: published docs plus the
    repository READMEs that link into them."""
    pages = [
        page
        for page in DOCS.rglob("*.md")
        if not {"_archive", "overrides"}.intersection(page.relative_to(DOCS).parts)
        and page.relative_to(DOCS).as_posix() not in UNPUBLISHED_STALE_PAGES
    ]
    pages.append(DOCS / "llms.txt")
    pages.extend(ROOT / name for name in sorted(LINK_GUARD_EXTRA_PAGES))
    return pages


def _retired_studio_guide_refs(page: Path, text: str) -> set[str]:
    """Targets in `text` that reach the retired Studio guide, for `page`.

    ``page`` resolves a relative target, so it must be the file the text came
    from. Inline markdown labels may wrap onto a second line and raw HTML
    anchors are read too, since both publish the same broken link.
    """
    targets = re.findall(r"\[[^\]]*?\]\(\s*<?([^\s)>]+)", text, flags=re.S)
    targets += re.findall(r"(?m)^ {0,3}\[[^\]\n]+\]:\s*<?([^\s>]+)", text)
    targets += re.findall(r"<a\s[^>]*href=[\"']([^\"']+)[\"']", text, flags=re.I)
    found: set[str] = set()
    for target in targets:
        link = urlsplit(target)
        path = unquote(link.path)
        if re.search(r"(?:^|/)guides/studio(?:\.md|/?)$", path):
            found.add(target)
        elif (
            not link.scheme
            and not link.netloc
            and (page.parent / path).resolve() in RETIRED_STUDIO_GUIDE_PATHS
        ):
            found.add(target)
    return found


def test_documentation_links_do_not_target_the_retired_studio_guide() -> None:
    stale: list[str] = []
    for page in _link_guard_pages():
        text = re.sub(r"<!--.*?-->", "", page.read_text(), flags=re.S)
        text = re.sub(r"(?ms)^ {0,3}(?:```|~~~).*?^ {0,3}(?:```|~~~)[^\n]*$", "", text)
        stale.extend(
            f"{page.relative_to(ROOT)}: {target}"
            for target in _retired_studio_guide_refs(page, text)
        )
    assert not stale, f"Links target the retired Studio guide: {stale}"


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
    from lionagi._auto import iter_cli_seeds

    reference = (DOCS / "cli-reference.md").read_text().lower()
    aliases = {
        "orchestrate": ("li orchestrate", "li o "),
    }
    for seed in iter_cli_seeds():
        needles = aliases.get(seed.name, (f"li {seed.name}",))
        assert any(needle in reference for needle in needles), (
            f"cli-reference.md does not make `li {seed.name}` discoverable"
        )

    for hidden_surface in ("li play", "li skill", "li wait"):
        assert hidden_surface in reference, f"Missing hidden CLI surface: {hidden_surface}"


def test_reference_identifies_the_current_release_line() -> None:
    api_index = (DOCS / "api" / "index.md").read_text()
    assert "0.28" in api_index
    assert "lionagi 0.22.6" not in api_index.lower()


def test_link_guard_covers_repository_readmes() -> None:
    scanned = {page.relative_to(ROOT).as_posix() for page in _link_guard_pages()}
    missing = [name for name in LINK_GUARD_EXTRA_PAGES if name not in scanned]
    assert not missing, f"Link guard does not scan: {missing}"


def test_link_guard_recognizes_wrapped_labels_and_html_anchors() -> None:
    """A regression written as a wrapped markdown label or as a raw HTML anchor
    must be reported, not only the single-line markdown form."""
    page = DOCS / "guides" / "studio-operator.md"
    text = (
        "[Studio and\nschedules](studio.md)\n\n"
        '<a href="../guides/studio.md">the Studio guide</a>\n\n'
        '<p>See <a href="studio/">the guide</a>.</p>\n\n'
        "[Operator](studio-operator.md)\n"
    )
    found = _retired_studio_guide_refs(page, text)
    assert sorted(found) == ["../guides/studio.md", "studio.md", "studio/"]


def _studio_config_default_origins() -> list[str]:
    """The default browser origins, read from the daemon that enforces them."""
    import importlib

    config = importlib.import_module("lionagi.studio.config")
    return config.CORS_ORIGINS


def test_studio_docs_list_every_default_browser_origin() -> None:
    """The trust sections must name each default origin; naming only the hosted
    origin would hide the localhost development ports that can also pair."""
    origins = _studio_config_default_origins()
    for name in ("guides/studio-getting-started.md", "concepts/studio-model.md"):
        text = (DOCS / name).read_text()
        missing = [origin for origin in origins if origin not in text]
        assert not missing, f"{name} does not name default origins: {missing}"


def test_operator_guide_names_the_shipped_auto_allow_control() -> None:
    """The composer checkbox ships under one label; the guide must use it."""
    import json

    messages = json.loads((ROOT / "apps/studio/frontend/src/messages/en.json").read_text())
    label = messages["operator"]["composer"]["autoAllow"]
    assert label in (DOCS / "guides/studio-operator.md").read_text()


def test_getting_started_names_the_bind_host_flag() -> None:
    """The launch-mode section must name `--host`: it is the flag that sets the
    bind address in every non-Docker mode, and the trust section depends on it."""
    text = (DOCS / "guides" / "studio-getting-started.md").read_text()
    assert "`--host`" in text
    assert "127.0.0.1" in text
