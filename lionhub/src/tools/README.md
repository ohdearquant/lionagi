# lionhub.tools

Tools a model can call that go beyond the core's file and shell tools: context management for a conversation, skill lookup, a memory store, web search and fetch, repository watching, and a content-addressed computer. It is not the tool base classes or the file, shell and messenger tools that `lionagi/tools/` holds today; this layout has no slot for them.

Final import path: `lionhub.tools` (ADR-0136 D7). The old paths of the modules that move (for example `lionagi.tools.context`) keep working through 1.x as the same module object, with a `DeprecationWarning` naming the new path; 2.0 removes them. The core reaches this package only lazily.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | package entry point | `lionagi/tools/__init__.py` |
| `watch.py` | the repository as a turn notice reports it: which files a change touches, since when it has stood still, and the model's own acceptance checks | `lion1/hub/tools/watch.py` |
| `web.py` | read-only web access: search (titles, links, snippets) and fetch of one page as text; results are treated as data, never as instructions | `lion1/hub/tools/web.py` |
| `context.py` | non-destructive management of a branch's active context: status, evict, restore, compact | `lionagi/tools/context/context.py` |
| `skill.py` | the skill reader: finds a named skill and returns its instructions | `lionagi/cli/skill.py` |
| `memory.py` | the memory tool over a bounded vector store | `lionagi/integrations/ruvector/memory.py` |
| `discovery.py` | finding which tools and capabilities are available | new; no existing code |
| `knowledge.py` | looking up stored knowledge from a tool call | new; no existing code |
| `computer/` | content-addressed workspace and sandboxed actions; see [computer/README.md](computer/README.md) | new; see that README |

The `memory.py` source is a proposal of the move map: the store lives in the integrations package, which the layout decision holds back, so whether the store moves or this slot wraps it is settled with the tools move. The layout decision creates `discovery.py` and `knowledge.py` together with their first code, not as empty modules.

## Who uses it

- `lionagi/agent/context_fold.py`, `lionagi/agent/nudge.py`: the context tool.
- `lionagi/cli/main.py`: dispatches `li skill NAME` to the skill reader; the command stays a thin front end.
- `lionagi/integrations/ruvector/__init__.py`: exports the memory store.

## Status

`context.py`, `skill.py` and `memory.py` move next, in the tools step of the third wave. `watch.py`, `web.py` and `computer/` are new code written from concepts, each as its own change and not as a move. `discovery.py` and `knowledge.py` are new.
