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
    # An HTML anchor may leave its href unquoted, and a bare autolink publishes
    # the same target; both are read so neither form can hide a stale link.
    for match in re.finditer(r"<a\s[^>]*href=(?:\"([^\"]+)\"|'([^']+)'|([^\s>\"']+))", text, re.I):
        targets.append(next(group for group in match.groups() if group is not None))
    targets += re.findall(r"<((?:https?://|mailto:)[^>\s]+)>", text)
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
    """The guard must read the repository READMEs by name; a change to
    LINK_GUARD_EXTRA_PAGES that drops one has to fail here, so the expected
    paths are spelled out rather than compared to the set under test."""
    scanned = {page.relative_to(ROOT).as_posix() for page in _link_guard_pages()}
    expected = {"README.md", "apps/studio/README.md", "apps/studio/frontend/README.md"}
    missing = sorted(expected - scanned)
    assert not missing, f"Link guard does not scan: {missing}"


def test_link_guard_recognizes_wrapped_labels_and_html_anchors() -> None:
    """A regression written as a wrapped markdown label, a raw HTML anchor
    (quoted or unquoted), or an autolink must be reported, not only the
    single-line markdown form."""
    page = DOCS / "guides" / "studio-operator.md"
    text = (
        "[Studio and\nschedules](studio.md)\n\n"
        '<a href="../guides/studio.md">the Studio guide</a>\n\n'
        '<p>See <a href="studio/">the guide</a>.</p>\n\n'
        "<a href=../guides/studio/>unquoted anchor</a>\n\n"
        "<https://example.invalid/docs/guides/studio.md>\n\n"
        "[Operator](studio-operator.md)\n"
    )
    found = _retired_studio_guide_refs(page, text)
    assert sorted(found) == [
        "../guides/studio.md",
        "../guides/studio/",
        "https://example.invalid/docs/guides/studio.md",
        "studio.md",
        "studio/",
    ]


def _studio_config_default_origins(monkeypatch) -> list[str]:
    """The default browser origins, read from the daemon that enforces them.

    ``CORS_ORIGINS`` is built at import time from the environment variable of
    the same name, so it is cleared first: the docs describe the defaults, and
    an exported override must not turn this contract red.
    """
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    import importlib

    config = importlib.import_module("lionagi.studio.config")
    return config.CORS_ORIGINS


def _named_origins(text: str) -> set[str]:
    """The ``http(s)://host[:port]`` origins a trust section names."""
    return set(re.findall(r"https?://[^\s`'\"<>,;)\]]+", text))


def test_studio_docs_list_every_default_browser_origin(monkeypatch) -> None:
    """The trust sections must name each default origin; naming only the hosted
    origin would hide the localhost development ports that can also pair."""
    origins = _studio_config_default_origins(monkeypatch)
    for name in ("guides/studio-getting-started.md", "concepts/studio-model.md"):
        text = (DOCS / name).read_text()
        missing = [origin for origin in origins if origin not in text]
        assert not missing, f"{name} does not name default origins: {missing}"


def test_studio_docs_name_only_origins_the_daemon_allows(monkeypatch) -> None:
    """Every origin the trust sections say can pair must be one the daemon
    admits; a documented origin the config no longer lists is a broken promise."""
    allowed = set(_studio_config_default_origins(monkeypatch))
    for name in ("guides/studio-getting-started.md", "concepts/studio-model.md"):
        text = (DOCS / name).read_text()
        paragraphs = [p for p in text.split("\n\n") if "CORS_ORIGINS" in p]
        named: set[str] = set()
        for paragraph in paragraphs:
            named |= _named_origins(paragraph)
        unexpected = sorted(named - allowed)
        assert not unexpected, f"{name} names origins the daemon does not allow: {unexpected}"


def test_operator_guide_names_the_shipped_auto_allow_control() -> None:
    """The composer checkbox ships under one label; the guide must use it."""
    import json

    messages = json.loads((ROOT / "apps/studio/frontend/src/messages/en.json").read_text())
    label = messages["operator"]["composer"]["autoAllow"]
    assert label in (DOCS / "guides/studio-operator.md").read_text()


def test_getting_started_names_the_bind_host_flag() -> None:
    """The launch-mode section must name `--host` and the modes it applies to.

    `127.0.0.1` and `` `--host` `` occur elsewhere on the page, so the
    assertion is on the paragraph that carries the claim: rewording that
    sentence to drop the default or a mode has to fail here."""
    text = (DOCS / "guides" / "studio-getting-started.md").read_text()
    paragraphs = [p for p in text.split("\n\n") if "`--host`" in p]
    assert len(paragraphs) == 1, "`--host` must be documented in exactly one paragraph"
    paragraph = paragraphs[0]
    assert "defaults to `127.0.0.1`" in paragraph
    assert "`--no-frontend`" in paragraph
    assert "`--dev`" in paragraph
    assert "Docker" in paragraph


def test_surfaces_availability_note_matches_the_frontend_kind_set() -> None:
    """The availability callout is the single owner of the Workflow/Engine
    availability fact, so it must list exactly the kinds the frontend hides.

    A change to `UNFINISHED_KINDS` that leaves the callout stale has to fail
    here, the way the Auto-allow label is pinned against `en.json`."""
    source = (ROOT / "apps/studio/frontend/src/routes/library.tsx").read_text()
    match = re.search(r"UNFINISHED_KINDS\s*=\s*new Set<[^>]*>\(\[([^\]]*)\]\)", source)
    assert match, "UNFINISHED_KINDS not found in library.tsx"
    kinds = set(re.findall(r"\"([^\"]+)\"", match.group(1)))
    assert kinds, "UNFINISHED_KINDS is empty; remove the availability callout"
    callout = (DOCS / "reference/studio-surfaces.md").read_text()
    for kind in sorted(kinds):
        assert f"`{kind}`" in callout, f"availability callout does not name `{kind}`"
