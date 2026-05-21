#!/usr/bin/env python3
"""Validate .claude-plugin/marketplace.json — checks required fields and SKILL.md presence."""

import json
import sys
from pathlib import Path

PLUGIN_REQUIRED = ["name", "source", "description"]
TOP_REQUIRED = ["name", "version", "description"]


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

    for plugin in plugins:
        name = plugin.get("name", "<unnamed>")
        plugin_ok = True

        for field in PLUGIN_REQUIRED:
            if field not in plugin:
                print(f"FAIL [{name}]: missing required field '{field}'")
                plugin_ok = False
                failures += 1

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

        if plugin_ok:
            print(f"PASS [{name}]")

    if failures == 0:
        print(f"\nAll {len(plugins)} plugin(s) passed.")
        return 0
    else:
        print(f"\n{failures} failure(s) found.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
