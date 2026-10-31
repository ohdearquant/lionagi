# lionhub.integrations.ruvector

The optional adapter for RuVector, a native approximate-nearest-neighbour index. It provides an in-memory cosine store with bounded payloads and a tool that exposes similarity search to a model. It is not a persistence layer and not an access-control system; callers supply vectors and must use separate stores for separate authorization scopes.

Final import path: `lionhub.integrations.ruvector` (ADR-0136 D7). The folder name in this draft is spelled `integerations`; the final package is `integrations`. The moved `lionagi.integrations.ruvector.*` paths keep working through 1.x as the same module objects, with a `DeprecationWarning` naming the new path. Installing the optional `ruvector` extra provides the native dependency; without it, importing the adapter raises a clear error.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | Package surface and optional-dependency guard. | `lionagi/integrations/ruvector/__init__.py`. |
| `store.py` | The bounded in-memory vector store and its search tool. | `lionagi/integrations/ruvector/store.py`. |
| `memory.py` | The memory envelope and conversion between stored records and memory items. | `lionagi/integrations/ruvector/memory.py`. |

## Who uses it

- Applications that install the `ruvector` extra and construct a store for retrieval.
- The search tool returned by the store, which a model can call through the core action layer.
- No module under `lionagi/` or `lionhub/` imports the adapter at module import time; it is opt-in.

## Status

Already present on the development line at `lionagi/integrations/ruvector/`; its move is held with the top-level `integrations` move.
