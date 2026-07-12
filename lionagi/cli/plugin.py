# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li plugin` — inspect, trust, and enable/disable LionAGI plugin bundles."""

from __future__ import annotations

import argparse

from ._logging import log_error


def add_plugin_subparser(subparsers: argparse._SubParsersAction) -> None:
    plugin = subparsers.add_parser(
        "plugin",
        help="Inspect, trust, and enable/disable LionAGI plugin bundles.",
        description=(
            "A plugin is a `.lionagi/plugins/<name>/` directory bundle carrying a "
            "plugin.yaml manifest. A plugin is fully inert (no code import, no "
            "profile/playbook exposure) until `li plugin trust` records an "
            "explicit, content-pinned approval."
        ),
    )
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)

    plugin_sub.add_parser(
        "list",
        help="List discovered plugins and their state.",
        description=(
            "State is one of: active, disabled, untrusted, changed, "
            "incompatible, collision, invalid."
        ),
    )

    info = plugin_sub.add_parser(
        "info",
        help="Show a plugin's manifest and trust state.",
    )
    info.add_argument("name", help="Plugin name (the manifest's `name:` field).")

    trust = plugin_sub.add_parser(
        "trust",
        help="Show everything a plugin declares and record trust (content-pinned).",
        description=(
            "Renders the plugin's full inventory — every tool target, hook "
            "command argv, agent/playbook/pack file, and provider module — "
            "then records a sha256 of each in ~/.lionagi/settings.yaml. Any "
            "later change to any of these reverts the plugin to 'changed' "
            "until re-trusted. Use --yes to skip the confirmation prompt."
        ),
    )
    trust.add_argument("name", help="Plugin name.")
    trust.add_argument(
        "--yes",
        action="store_true",
        help="Record trust without an interactive confirmation prompt.",
    )

    enable = plugin_sub.add_parser(
        "enable",
        help="Enable a plugin (clears `enabled: false` in ~/.lionagi/settings.yaml).",
    )
    enable.add_argument("name", help="Plugin name.")

    disable = plugin_sub.add_parser(
        "disable",
        help="Disable a plugin (sets `enabled: false` in ~/.lionagi/settings.yaml).",
        description="A settings flag, not a file mutation — the bundle stays pristine.",
    )
    disable.add_argument("name", help="Plugin name.")


def _state_row(record) -> str:  # noqa: ANN001 — PluginRecord, imported lazily by callers
    version = record.version or "?"
    return f"{record.name:<24} {version:<10} {record.state.value}"


def _run_list() -> int:
    from lionagi.plugins import PluginRegistry

    PluginRegistry.reset()
    records = PluginRegistry.list_plugins()
    if not records:
        print("(no plugins found)")
        return 0
    print(f"{'NAME':<24} {'VERSION':<10} STATE")
    for record in sorted(records, key=lambda r: r.name):
        print(_state_row(record))
        if record.error:
            print(f"  {record.error}")
    return 0


def _run_info(name: str) -> int:
    from lionagi.plugins import PluginRegistry
    from lionagi.plugins.trust import build_trust_disclosure

    PluginRegistry.reset()
    record = PluginRegistry.get(name)
    if record is None:
        log_error(f"unknown plugin: {name!r}")
        return 1

    print(f"name:    {record.name}")
    print(f"version: {record.version or '?'}")
    print(f"state:   {record.state.value}")
    print(f"bundle:  {record.bundle_dir}")
    if record.error:
        print(f"error:   {record.error}")
    if record.manifest is None:
        return 0

    disclosure = build_trust_disclosure(record)
    print(f"description: {disclosure['description']}")
    print(f"requires lionagi: {disclosure['lionagi']}")
    if disclosure["tools"]:
        print("tools:")
        for t in disclosure["tools"]:
            print(f"  {t['name']} -> {t['target']}")
    if disclosure["hooks_external"]:
        print("hooks_external:")
        for h in disclosure["hooks_external"]:
            print(f"  [{h['event']}] matcher={h['matcher']!r} argv={h['argv']}")
    if disclosure["agents"]:
        print("agents: " + ", ".join(disclosure["agents"]))
    if disclosure["playbooks"]:
        print("playbooks: " + ", ".join(disclosure["playbooks"]))
    if disclosure["providers"]:
        print("providers: " + ", ".join(disclosure["providers"]))
    if disclosure["packs"]:
        print("packs: " + ", ".join(disclosure["packs"]))
    return 0


def _print_disclosure(disclosure: dict) -> None:
    print(f"Plugin: {disclosure['name']} ({disclosure['version']})")
    print(disclosure["description"])
    print(f"requires lionagi: {disclosure['lionagi']}")
    print()
    print("The following will be trusted (content-pinned; any later edit reverts to 'changed'):")
    for t in disclosure["tools"]:
        print(f"  tool    {t['name']} -> {t['target']}")
    for h in disclosure["hooks_external"]:
        print(f"  hook    [{h['event']}] matcher={h['matcher']!r} argv={h['argv']}")
    for a in disclosure["agents"]:
        print(f"  agent   {a}")
    for p in disclosure["playbooks"]:
        print(f"  playbook {p}")
    for p in disclosure["providers"]:
        print(f"  provider {p}")
    for p in disclosure["packs"]:
        print(f"  pack    {p}")


def _run_trust(name: str, *, assume_yes: bool) -> int:
    from lionagi.plugins import PluginRegistry
    from lionagi.plugins.discovery import discover_plugins
    from lionagi.plugins.trust import build_trust_disclosure, trust_plugin

    PluginRegistry.reset()
    record = PluginRegistry.get(name)
    if record is None or record.manifest is None:
        log_error(f"unknown or invalid plugin: {name!r}")
        return 1

    # Trust needs the freshly-discovered bundle (declared_files + manifest),
    # not just the registry's summary record — re-scan and pick the match.
    discovered = next(
        (d for d in discover_plugins() if d.manifest is not None and d.manifest.name == name),
        None,
    )
    if discovered is None:
        log_error(f"plugin {name!r} disappeared during trust (re-run `li plugin list`)")
        return 1

    disclosure = build_trust_disclosure(discovered)
    _print_disclosure(disclosure)

    if not assume_yes:
        answer = input("\nTrust this plugin? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("not trusted.")
            return 1

    try:
        trust_plugin(discovered)
    except FileNotFoundError as exc:
        log_error(str(exc))
        return 1
    PluginRegistry.reset()
    print(f"trusted {name!r}.")
    return 0


def _run_set_enabled(name: str, *, enabled: bool) -> int:
    from lionagi.plugins import PluginRegistry
    from lionagi.plugins._user_settings import read_user_settings, write_user_settings

    PluginRegistry.reset()
    record = PluginRegistry.get(name)
    if record is None:
        log_error(f"unknown plugin: {name!r}")
        return 1

    settings = read_user_settings()
    plugins_block = settings.setdefault("plugins", {})
    if not isinstance(plugins_block, dict):
        plugins_block = {}
        settings["plugins"] = plugins_block
    entry = plugins_block.setdefault(name, {})
    if not isinstance(entry, dict):
        entry = {}
        plugins_block[name] = entry
    entry["enabled"] = enabled
    write_user_settings(settings)
    PluginRegistry.reset()
    print(f"{'enabled' if enabled else 'disabled'} {name!r}.")
    return 0


def run_plugin(args: argparse.Namespace) -> int:
    if args.plugin_command == "list":
        return _run_list()
    if args.plugin_command == "info":
        return _run_info(args.name)
    if args.plugin_command == "trust":
        return _run_trust(args.name, assume_yes=args.yes)
    if args.plugin_command == "enable":
        return _run_set_enabled(args.name, enabled=True)
    if args.plugin_command == "disable":
        return _run_set_enabled(args.name, enabled=False)
    log_error(f"unknown plugin subcommand: {args.plugin_command!r}")
    return 1
