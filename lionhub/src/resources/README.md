# lionhub.resources

A reserved name for resource code that is not the plugin runtime. Today it holds nothing: the plugin runtime (registry, manifest, trust, discovery) stays in the core as `lionagi.plugins`, because the core's connection registry, action manager and CLI already depend on it. This folder is created with its first module when code other than the runtime needs it.

Final import path: `lionhub.resources`. The draft reserves slots for the plugin runtime that the layout decision (`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md`, Amendment 1) withdrew, so the table says where each one lives now. The draft folder has no `__init__.py`. Nothing moves here from a `lionagi.*` path, so no `DeprecationWarning` shim applies; the core does not import this folder, and any future core access would be lazy, never at import time.

| Slot | What it holds | Source |
| --- | --- | --- |
| `registry.py` | withdrawn: the plugin registry does not move; it stays in the core | `lionagi/plugins/registry.py` (stays) |
| `plugin.py` | withdrawn: the plugin runtime (manifest, trust, discovery) does not move | `lionagi/plugins/manifest.py`, `lionagi/plugins/trust.py`, `lionagi/plugins/discovery.py` (stay) |
| `mod.py` | held name for resource code that is not a plugin; its purpose is not set in the layout decision | new; no existing code |

The layout decision creates slots without code (such as `mod.py`) together with their first code, not as empty modules.

## Who uses it

Nothing imports this folder. The plugin runtime it was meant to hold is used by `lionagi/cli/plugin.py`, `lionagi/service/connections/registry.py`, `lionagi/protocols/action/manager.py` and `lionagi/hooks/external.py`, and those imports of `lionagi.plugins` stay as they are.

## Status

Not moving. Reserved; no code until a non-plugin resource needs it.
