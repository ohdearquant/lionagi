# lionhub.integrations

Optional adapters between LionAGI and external systems. An integration owns the translation between a product's data or protocol and the core's protocols and declares its optional dependencies. It is not a provider: providers talk to model APIs, while these packages connect to storage, memory and collaboration services. Nothing here is imported by the core at module import time.

Final import path: `lionhub.integrations` (ADR-0136 D7). The folder name in this draft is spelled `integerations`; the final package is `integrations`, and the moved `lionagi.integrations.*` paths keep working through 1.x as the same module objects, with a `DeprecationWarning` naming the new path. The top-level `integrations` move is held; its subpackages have separate status below.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | Package surface for optional integrations and their shared conventions. | `lionagi/integrations/__init__.py`. |
| `khive/` | Adapter for khive's durable memory and actor services. | new; no existing package. See [khive/README.md](khive/README.md). |
| `ruvector/` | Adapter for the RuVector native vector index. | `lionagi/integrations/ruvector/`; see [ruvector/README.md](ruvector/README.md). |

## Who uses it

- `lionagi/agent/spec.py` and `lionagi/cli/agent.py`: read the `khive_injection` setting that selects the khive adapter.
- `lionagi/tools/khive_injection.py`: the existing khive context provider and policy types, which the khive integration builds on.
- The `lionagi[ruvector]` extra installs the RuVector dependency for the adapter.
- `li` and Studio select integrations through configuration; neither imports this package eagerly.

## Status

The `ruvector` subpackage already exists on the development line at `lionagi/integrations/ruvector/`, and its move is held. The khive subpackage is new code. The top-level package move remains held pending the integrations decision.
