# lionhub.integrations.khive

The adapter between LionAGI and khive's durable memory and actor services. It carries recall, composition and write-back policy for a conversation, and the client calls that talk to khive over its existing MCP transport. It is not a database, an MCP server or a policy authority: khive owns storage, identity and authorization.

Final import path: `lionhub.integrations.khive` (ADR-0136 D7). The folder name in this draft is spelled `integerations`; the final package is `integrations`. The new package has no old `lionagi.*` path to keep compatible.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | Package surface: the injection provider and its recall, compose and write-back policies. | `lionagi/tools/khive_injection.py`. |

The concept also has a small hub-side client at `lion1/hub/tools/khive.py` and a Python client at `lion1/src/lion1/khive.py`; neither is moved as-is. The development-line provider is the source for this slot.

## Who uses it

- `lionagi/agent/spec.py`: the `khive_injection` field on an agent specification.
- `lionagi/cli/agent.py` and `lionagi/cli/orchestrate/_orchestration.py`: pass a profile's injection policy into a run and record its telemetry.
- `lionagi/cli/monitor.py`: displays the injection summary recorded on a run.
- The provider itself calls khive's `request` tool through `lionagi/service/connections/mcp_wrapper.py`.

## Status

New package assembled from existing development-line code; no move has landed. It is held with the top-level `integrations` move.
