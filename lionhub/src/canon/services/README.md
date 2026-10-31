# lionhub.canon.services

Infrastructure services that the governance modules call instead of implementing inline. In this draft that is one service: redaction, which finds personal data in text and removes it or blocks the text. It holds no governance records and no policy, and it is not the place for the Studio application's own scrubbing of secrets and host paths.

Final import path: `lionhub.canon.services`. The folder name in this draft is spelled the same; it sits at `canon/services/` here and moves under the `lionhub` package.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | The package surface: the services this folder exposes. The concept's services package also exposes auth, just-in-time access, metering and vendor services; this draft's surface exposes only redaction. | the governance concepts: the infrastructure services package surface |
| `redact.py` | Detection and redaction of personal data: the categories and entities found, a result type, a model-based pass for context-dependent entities such as names, a pattern-based scan (identity numbers, card numbers, phone, email) used as a gate before text is stored, and the errors raised for blocking findings. | the governance concepts: the redaction service |

## Who uses it

Nothing calls it today: no module under `lionagi/` or `lionhub/` on the development line imports `lionhub.canon`. The Studio application keeps its own helpers: `lionagi/studio/operator/redact.py` (bounds and scrubbing of secrets, tokens and host paths for the operator's read tools) and `lionagi/studio/services/redaction.py` (the demo-mode projection of profile content). They are the nearest existing code. They stay with the Studio application, they are not sources for this module, and they could import from it once it exists.

## Status

New code, written from the governance concepts as its own change with its own tests. It is not a move, so it adds no row to the old-to-new import table, and the core never imports it.
