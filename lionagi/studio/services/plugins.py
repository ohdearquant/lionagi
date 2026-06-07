from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ._path_safety import public_path, safe_path_join

# ---------------------------------------------------------------------------
# Root directories
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
# plugins.py is at lionagi/studio/services/plugins.py
# parents: [0]=services, [1]=studio, [2]=lionagi, [3]=repo root
_REPO_ROOT = _THIS.parents[3]
MARKETPLACE_DIR = _REPO_ROOT / "marketplace"

# Fallback: if the server is running from a pip-install, try LIONAGI_HOME.parent
if not MARKETPLACE_DIR.exists():
    try:
        from lionagi.cli._runs import LIONAGI_HOME  # type: ignore[import-untyped]

        _fallback = LIONAGI_HOME.parent / "marketplace"
        if _fallback.exists():
            MARKETPLACE_DIR = _fallback
    except Exception:  # noqa: S110
        pass

MARKETPLACE_MANIFEST = _REPO_ROOT / ".claude-plugin" / "marketplace.json"
if not MARKETPLACE_MANIFEST.exists():
    _mf_fallback = MARKETPLACE_DIR.parent / ".claude-plugin" / "marketplace.json"
    if _mf_fallback.exists():
        MARKETPLACE_MANIFEST = _mf_fallback
THIRDPARTY_DIR = Path.home() / ".claude" / "plugins" / "cache"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter + markdown body. Returns (frontmatter_dict, body_text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm if isinstance(fm, dict) else {}, parts[2].strip()


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON file, returning None on any error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _scan_skills(plugin_dir: Path) -> list[dict[str, Any]]:
    """Return list of {name, description} for each skill in plugin_dir/skills/."""
    skills_dir = plugin_dir / "skills"
    if not skills_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Look for SKILL.md first, then {dir_name}.md, then any .md
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            alt = entry / f"{entry.name}.md"
            skill_md = alt if alt.exists() else next(iter(entry.glob("*.md")), None)  # type: ignore[assignment]
        if skill_md is None or not skill_md.exists():
            continue
        try:
            text = skill_md.read_text()
        except OSError:
            continue
        fm, _ = _parse_frontmatter(text)
        out.append(
            {
                "name": str(fm.get("name") or entry.name),
                "description": str(fm.get("description") or "").strip(),
            }
        )
    return out


def _scan_agents(plugin_dir: Path) -> list[dict[str, Any]]:
    """Return list of {name, description} for each *.md in plugin_dir/agents/."""
    agents_dir = plugin_dir / "agents"
    if not agents_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(agents_dir.glob("*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        fm, _ = _parse_frontmatter(text)
        out.append(
            {
                "name": path.stem,
                "description": str(fm.get("description") or "").strip(),
            }
        )
    return out


def _plugin_summary(
    plugin_dir: Path,
    name: str,
    description: str,
    source: str,
) -> dict[str, Any]:
    """Build the summary dict for list_plugins() from a plugin directory."""
    plugin_json = _read_json(plugin_dir / ".claude-plugin" / "plugin.json") or {}
    skills = _scan_skills(plugin_dir)
    agents = _scan_agents(plugin_dir)
    has_hooks = (plugin_dir / "hooks" / "hooks.json").exists()
    has_mcp = (plugin_dir / ".mcp.json").exists()

    # Also check for mcpServers in plugin.json
    if not has_mcp and plugin_json.get("mcpServers"):
        has_mcp = True

    return {
        "name": str(plugin_json.get("name") or name),
        "description": str(plugin_json.get("description") or description).strip(),
        "version": str(plugin_json.get("version") or "0.0.0"),
        "source": source,
        "skill_count": len(skills),
        "agent_count": len(agents),
        "has_hooks": has_hooks,
        "has_mcp": has_mcp,
        "path": public_path(plugin_dir),
    }


def _plugin_detail(
    plugin_dir: Path,
    name: str,
    description: str,
    source: str,
) -> dict[str, Any]:
    """Build the full detail dict for get_plugin() from a plugin directory."""
    summary = _plugin_summary(plugin_dir, name, description, source)
    skills = _scan_skills(plugin_dir)
    agents = _scan_agents(plugin_dir)

    hooks_path = plugin_dir / "hooks" / "hooks.json"
    hooks = _read_json(hooks_path) if hooks_path.exists() else None

    mcp_path = plugin_dir / ".mcp.json"
    if mcp_path.exists():
        mcp = _read_json(mcp_path)
    else:
        # Inline mcpServers from plugin.json
        plugin_json = _read_json(plugin_dir / ".claude-plugin" / "plugin.json") or {}
        mcp = plugin_json.get("mcpServers") or None

    readme_path = plugin_dir / "README.md"
    readme: str | None = None
    if readme_path.exists():
        try:
            readme = readme_path.read_text()
        except OSError:
            pass

    return {
        **summary,
        "skills": skills,
        "agents": agents,
        "hooks": hooks,
        "mcp": mcp,
        "readme": readme,
    }


# ---------------------------------------------------------------------------
# Marketplace plugin discovery
# ---------------------------------------------------------------------------


def _resolve_marketplace_source(source_rel: str) -> Path | None:
    """Resolve a marketplace manifest source path, rejecting escape attempts.

    Rejects empty, absolute, parent-traversal, and symlink-escaped paths so
    a malicious marketplace.json cannot reference files outside the repo.
    """
    if not source_rel:
        return None
    source_path = Path(source_rel)
    if source_path.is_absolute() or ".." in source_path.parts:
        return None
    try:
        plugin_dir = (_REPO_ROOT / source_path).resolve()
        plugin_dir.relative_to(_REPO_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return plugin_dir


def _iter_marketplace_plugins() -> list[tuple[Path, str, str]]:
    """Yield (plugin_dir, name, description) for each marketplace plugin."""
    if not MARKETPLACE_MANIFEST.exists():
        return []

    manifest = _read_json(MARKETPLACE_MANIFEST)
    if not manifest:
        return []

    results: list[tuple[Path, str, str]] = []
    for entry in manifest.get("plugins", []):
        name = str(entry.get("name") or "")
        source_rel = str(entry.get("source") or "")
        desc = str(entry.get("description") or "")
        if not name:
            continue
        plugin_dir = _resolve_marketplace_source(source_rel)
        if plugin_dir is not None and plugin_dir.exists():
            results.append((plugin_dir, name, desc))

    return results


def _find_plugin_dir_for(name: str) -> Path | None:
    """Return the actual filesystem Path for a named plugin without going through the API response."""
    for plugin_dir, pname, _ in _iter_marketplace_plugins():
        if pname == name:
            return plugin_dir
    for plugin_dir, pname, _, _ in _iter_thirdparty_plugins():
        if pname == name:
            return plugin_dir
    return None


# ---------------------------------------------------------------------------
# Third-party plugin discovery
# ---------------------------------------------------------------------------


def _iter_thirdparty_plugins() -> list[tuple[Path, str, str, str]]:
    """Yield (plugin_dir, name, description, marketplace_name) for each installed third-party plugin.

    Layout: ~/.claude/plugins/cache/{marketplace}/{plugin_name}/{version}/
    We take the lexicographically latest version directory for each plugin.
    """
    if not THIRDPARTY_DIR.exists():
        return []

    results: list[tuple[Path, str, str, str]] = []
    for marketplace_dir in sorted(THIRDPARTY_DIR.iterdir()):
        if not marketplace_dir.is_dir():
            continue
        mp_name = marketplace_dir.name
        for plugin_name_dir in sorted(marketplace_dir.iterdir()):
            if not plugin_name_dir.is_dir():
                continue
            version_dirs = sorted([d for d in plugin_name_dir.iterdir() if d.is_dir()])
            if not version_dirs:
                continue
            plugin_dir = version_dirs[-1]

            plugin_json = _read_json(plugin_dir / ".claude-plugin" / "plugin.json") or {}
            name = str(plugin_json.get("name") or plugin_name_dir.name)
            desc = str(plugin_json.get("description") or "").strip()
            results.append((plugin_dir, name, desc, mp_name))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_plugins() -> list[dict[str, Any]]:
    """Scan marketplace/ and ~/.claude/plugins/cache/ for plugins.

    For each plugin returns:
    - name, description, version (from plugin.json or fallback)
    - source: "marketplace" | "third-party"
    - skill_count, agent_count
    - has_hooks (bool), has_mcp (bool)
    - path (absolute path to plugin dir)
    """
    out: list[dict[str, Any]] = []

    for plugin_dir, name, desc in _iter_marketplace_plugins():
        out.append(_plugin_summary(plugin_dir, name, desc, "marketplace"))

    for plugin_dir, name, desc, mp_name in _iter_thirdparty_plugins():
        out.append(_plugin_summary(plugin_dir, name, desc, mp_name))

    return out


def get_plugin(name: str) -> dict[str, Any] | None:
    """Full plugin detail including skills list, agents list, hooks, mcp, readme."""
    # Search marketplace first
    for plugin_dir, pname, desc in _iter_marketplace_plugins():
        if pname == name:
            return _plugin_detail(plugin_dir, pname, desc, "marketplace")

    # Then third-party
    for plugin_dir, pname, desc, mp_name in _iter_thirdparty_plugins():
        if pname == name:
            return _plugin_detail(plugin_dir, pname, desc, mp_name)

    return None


def get_plugin_skill(plugin_name: str, skill_name: str) -> dict[str, Any] | None:
    """Get a specific skill's full content from a plugin.

    Returns name, description, path, content, allowed_tools — same shape as
    skills.get_skill().
    """
    plugin_dir = _find_plugin_dir_for(plugin_name)
    if plugin_dir is None:
        return None
    # Validate skill_name is safe before joining to filesystem path
    safe_path_join(plugin_dir / "skills", skill_name)

    skills_dir = plugin_dir / "skills"
    skill_dir = skills_dir / skill_name
    if not skill_dir.exists() or not skill_dir.is_dir():
        return None

    # Find the skill's markdown file using the same logic as skills.py
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        alt = skill_dir / f"{skill_name}.md"
        skill_md = alt if alt.exists() else next(iter(skill_dir.glob("*.md")), None)  # type: ignore[assignment]
    if skill_md is None or not skill_md.exists():
        return None

    try:
        text = skill_md.read_text()
    except OSError:
        return None

    fm, body = _parse_frontmatter(text)
    allowed_tools = fm.get("allowed-tools")
    if not isinstance(allowed_tools, list):
        allowed_tools = [allowed_tools] if allowed_tools else []

    return {
        "name": str(fm.get("name") or skill_name),
        "description": str(fm.get("description") or "").strip(),
        "path": public_path(skill_md),
        "allowed_tools": allowed_tools,
        "content": body,
    }


def get_plugin_agent(plugin_name: str, agent_name: str) -> dict[str, Any] | None:
    """Get a specific agent's full content from a plugin.

    Returns name, description, path, content.
    """
    plugin_dir = _find_plugin_dir_for(plugin_name)
    if plugin_dir is None:
        return None
    # Validate agent_name is safe before joining to filesystem path
    safe_path_join(plugin_dir / "agents", agent_name)

    agents_dir = plugin_dir / "agents"
    # Support both "name" and "name.md"
    stem = agent_name.removesuffix(".md")
    agent_path = agents_dir / f"{stem}.md"
    if not agent_path.exists():
        return None

    try:
        text = agent_path.read_text()
    except OSError:
        return None

    fm, body = _parse_frontmatter(text)

    return {
        "name": stem,
        "description": str(fm.get("description") or "").strip(),
        "path": public_path(agent_path),
        "content": body,
    }
