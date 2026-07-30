#!/usr/bin/env python3
"""Validate marketplace manifests — checks required fields, SKILL.md presence,
per-plugin plugin.json files, duplicate names/sources, and stub mcpServer entries."""

import json
import sys
from pathlib import Path

PLUGIN_REQUIRED = ["name", "source", "description"]
TOP_REQUIRED = ["name", "version", "description"]
PER_PLUGIN_REQUIRED = ["name", "version", "description"]
PER_PLUGIN_STRING_FIELDS = ["name", "version", "description"]
PER_PLUGIN_OPTIONAL_STRINGS = ["repository", "license", "homepage"]


def _has_frontmatter_description(path: Path) -> bool:
    """True when the file opens with a frontmatter block carrying a non-empty description."""
    try:
        text = path.read_text()
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    _, _, rest = text.partition("\n")
    block, sep, _ = rest.partition("\n---")
    if not sep:
        return False
    return any(
        line.startswith("description:") and line[len("description:") :].strip()
        for line in block.splitlines()
    )


def main() -> int:
    repo_root = Path(__file__).parent.parent.parent
    manifest_path = repo_root / ".claude-plugin" / "marketplace.json"

    if not manifest_path.exists():
        print(f"FAIL: manifest not found at {manifest_path}")
        return 1

    with manifest_path.open() as f:
        manifest = json.load(f)

    failures = 0

    # Top-level required fields
    for field in TOP_REQUIRED:
        if field not in manifest:
            print(f"FAIL [manifest]: missing top-level field '{field}'")
            failures += 1

    plugins = manifest.get("plugins", [])
    if not isinstance(plugins, list):
        print("FAIL [manifest]: 'plugins' must be an array")
        return 1

    print(f"Checking {len(plugins)} plugin(s) in {manifest_path.relative_to(repo_root)}")

    seen_names: dict[str, int] = {}
    seen_sources: dict[str, int] = {}

    for idx, plugin in enumerate(plugins):
        name = plugin.get("name", "<unnamed>")
        plugin_ok = True

        for field in PLUGIN_REQUIRED:
            if field not in plugin:
                print(f"FAIL [{name}]: missing required field '{field}'")
                plugin_ok = False
                failures += 1

        # Duplicate name/source detection
        if name in seen_names:
            print(f"FAIL [{name}]: duplicate plugin name (also at index {seen_names[name]})")
            plugin_ok = False
            failures += 1
        else:
            seen_names[name] = idx

        source = plugin.get("source", "")
        if source:
            if source in seen_sources:
                print(
                    f"FAIL [{name}]: duplicate source '{source}' (also used by index {seen_sources[source]})"
                )
                plugin_ok = False
                failures += 1
            else:
                seen_sources[source] = idx

        if "source" in plugin:
            source_rel = plugin["source"].lstrip("./")
            source_dir = repo_root / source_rel
            if not source_dir.is_dir():
                print(f"FAIL [{name}]: source directory not found: {plugin['source']}")
                plugin_ok = False
                failures += 1
            else:
                skill_files = sorted(source_dir.rglob("SKILL.md"))
                if not skill_files:
                    print(f"FAIL [{name}]: no SKILL.md found under {plugin['source']}")
                    plugin_ok = False
                    failures += 1
                else:
                    for sf in skill_files:
                        rel = sf.relative_to(repo_root)
                        if not sf.is_file():
                            print(f"FAIL [{name}]: SKILL.md not a file: {rel}")
                            plugin_ok = False
                            failures += 1

                # Agent markdown must be a direct child of agents/ and carry a
                # description. Plugin hosts walk nested agents/<subdir>/*.md as
                # agents too, so support docs kept there surface as malformed
                # agents; the description is what makes an agent selectable by
                # intent rather than merely installed.
                agents_dir = source_dir / "agents"
                if agents_dir.is_dir():
                    for md in sorted(agents_dir.rglob("*.md")):
                        rel_md = md.relative_to(repo_root)
                        if md.parent != agents_dir:
                            print(
                                f"FAIL [{name}]: agent markdown must be a direct child of "
                                f"agents/: {rel_md}"
                            )
                            plugin_ok = False
                            failures += 1
                        elif not _has_frontmatter_description(md):
                            print(
                                f"FAIL [{name}]: agent missing frontmatter 'description': {rel_md}"
                            )
                            plugin_ok = False
                            failures += 1

                # Per-plugin plugin.json validation
                per_plugin_json = source_dir / ".claude-plugin" / "plugin.json"
                if not per_plugin_json.exists():
                    print(
                        f"FAIL [{name}]: Listed source '{plugin['source']}' has no .claude-plugin/plugin.json"
                    )
                    plugin_ok = False
                    failures += 1
                else:
                    with per_plugin_json.open() as pf:
                        per_plugin = json.load(pf)
                    for field in PER_PLUGIN_REQUIRED:
                        if field not in per_plugin:
                            print(f"FAIL [{name}]: plugin.json missing required field '{field}'")
                            plugin_ok = False
                            failures += 1
                    for field in PER_PLUGIN_STRING_FIELDS:
                        val = per_plugin.get(field)
                        if val is not None and not isinstance(val, str):
                            print(
                                f"FAIL [{name}]: plugin.json '{field}' must be a string, got {type(val).__name__}"
                            )
                            plugin_ok = False
                            failures += 1
                    for field in PER_PLUGIN_OPTIONAL_STRINGS:
                        val = per_plugin.get(field)
                        if val is not None and not isinstance(val, str):
                            print(
                                f"FAIL [{name}]: plugin.json '{field}' must be a string, got {type(val).__name__}"
                            )
                            plugin_ok = False
                            failures += 1
                    author = per_plugin.get("author")
                    if author is not None and not isinstance(author, dict):
                        print(
                            f"FAIL [{name}]: plugin.json 'author' must be an object, got {type(author).__name__}"
                        )
                        plugin_ok = False
                        failures += 1
                    mcp = per_plugin.get("mcpServers")
                    if mcp is not None and not isinstance(mcp, dict):
                        print(
                            f"FAIL [{name}]: plugin.json 'mcpServers' must be an object, got {type(mcp).__name__}"
                        )
                        plugin_ok = False
                        failures += 1
                    # Each server entry must be an object before it is inspected: a
                    # string or list value raises on .get and kills the run with a
                    # traceback instead of reporting a failure line.
                    for server_name, server_cfg in per_plugin.get("mcpServers", {}).items():
                        if not isinstance(server_cfg, dict):
                            print(
                                f"FAIL [{name}]: plugin.json mcpServers['{server_name}'] must be "
                                f"an object, got {type(server_cfg).__name__}"
                            )
                            plugin_ok = False
                            failures += 1
                        elif server_cfg.get("type") == "stub":
                            print(f"FAIL [{name}]: plugin.json contains stub mcpServers entry")
                            plugin_ok = False
                            failures += 1

        if plugin_ok:
            print(f"PASS [{name}]")

    # Also scan marketplace subdirs for plugin.json files not referenced by manifest
    marketplace_dir = repo_root / "marketplace"
    for pjson in sorted(marketplace_dir.glob("*/.claude-plugin/plugin.json")):
        with pjson.open() as f:
            pdata = json.load(f)
        pname = pdata.get("name", "<unnamed>")
        pversion = pdata.get("version")
        plugin_dir = pjson.parent.parent.name
        if pname not in seen_names:
            # Standalone plugin.json not in marketplace.json — validate it anyway
            ok = True
            for field in PER_PLUGIN_REQUIRED:
                if field not in pdata:
                    print(f"FAIL [standalone:{plugin_dir}]: plugin.json missing '{field}'")
                    failures += 1
                    ok = False
            for server_name, server_cfg in pdata.get("mcpServers", {}).items():
                if not isinstance(server_cfg, dict):
                    print(
                        f"FAIL [standalone:{plugin_dir}]: plugin.json "
                        f"mcpServers['{server_name}'] must be an object, got "
                        f"{type(server_cfg).__name__}"
                    )
                    failures += 1
                    ok = False
                elif server_cfg.get("type") == "stub":
                    print(
                        f"FAIL [standalone:{plugin_dir}]: plugin.json contains stub mcpServers entry"
                    )
                    failures += 1
                    ok = False
            if ok:
                print(f"PASS [standalone:{plugin_dir}] (version={pversion})")

    if failures == 0:
        print(f"\nAll {len(plugins)} plugin(s) passed.")
        return 0
    else:
        print(f"\n{failures} failure(s) found.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
