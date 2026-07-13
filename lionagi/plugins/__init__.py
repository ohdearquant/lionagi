# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""LionAGI runtime plugin system: directory-bundle manifest with lazy activation.

A plugin is a ``.lionagi/plugins/<name>/`` directory bundle carrying a
``plugin.yaml`` manifest (see ``manifest.py``). Discovery (``discovery.py``)
and the registry (``registry.py``) are data-only until a specific declared
capability is actually used — importing this package does not scan, parse,
or import anything; every module here is inert until a consumer (the ``li
plugin`` CLI, or a capability-resolution miss in another subsystem) calls
into it.

This is unrelated to ``lionagi.studio.services.plugins``, which reads
Claude-Code-format plugin bundles for the Studio marketplace viewer — the two
are separate concepts that happen to share a directory-bundle idiom.
"""

from __future__ import annotations

from .discovery import DiscoveredPlugin, discover_plugins
from .manifest import Capabilities, ManifestError, PluginManifest, parse_manifest
from .registry import (
    PluginActivationError,
    PluginRecord,
    PluginRegistry,
    PluginState,
    PluginToolCollisionError,
    ToolTarget,
)
from .trust import TrustState, build_trust_disclosure, trust_plugin, trust_state

__all__ = (
    "Capabilities",
    "DiscoveredPlugin",
    "ManifestError",
    "PluginActivationError",
    "PluginManifest",
    "PluginRecord",
    "PluginRegistry",
    "PluginState",
    "PluginToolCollisionError",
    "ToolTarget",
    "TrustState",
    "build_trust_disclosure",
    "discover_plugins",
    "parse_manifest",
    "trust_plugin",
    "trust_state",
)
