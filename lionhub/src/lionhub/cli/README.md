# lionhub.cli

The command catalog discovers built-in commands lazily. The Studio and schedule
adapters live here so selecting a command imports only its own adapter.

| Module | Holds |
| --- | --- |
| `catalog` | Fixed command seeds, selected-command loading, and parser composition |
| `studio` | The `li studio` parser and launcher |
| `schedule` | The `li schedule` parser and schedule client |

Through 1.x, `lionagi.studio.cli` re-exports the adapter names as the same objects.
Patch callers through `lionhub.cli.studio` or `lionhub.cli.schedule`, since changing
an attribute on the compatibility module does not change the adapter's globals.
