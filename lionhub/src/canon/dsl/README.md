# lionhub.canon.dsl

The declarative language in which a charter's workflows are written, and the compiler that turns that text into a validated phase graph. This package reads the text (tokens, then a syntax tree), checks it (operation names, schema references, phase references, policy references, role actions, and that the phase dependencies form no cycle) and returns the compiled result. It does not run workflows (`lionhub.canon.runtime`), hold the charter record (`lionhub.canon.charter`) or enforce policy (`lionhub.canon.governance`).

Final import path: `lionhub.canon.dsl`. The folder name in this draft is spelled the same; it sits at `canon/dsl/` here and moves under the `lionhub` package.

| Slot | What it holds | Where the code comes from |
|---|---|---|
| `__init__.py` | The language's public entry point. The concept's package surface exports the compile and parse functions, the lexer, parser, resolver, schema catalog, syntax-tree node types and the language's error types. The draft reserves a single slot for the whole language; its modules get their own files when the port is written. | the governance concepts: the charter DSL package surface (lexer, parser, resolver, compiler, catalog) |

The modules of the concept (lexer, parser, syntax tree, resolver, catalog, compiler) get their own files next to `__init__.py` when the port is written; the draft names none of them.

## Who uses it

Nothing calls it today: no module under `lionagi/` or `lionhub/` on the development line imports `lionhub.canon`. In the governance concepts, charter text is compiled here before `lionhub.canon.runtime` walks the resulting phase graph.

## Status

New code, written from the governance concepts as its own change with its own tests. There is no existing lionagi or lionhub code to move, so it adds no row to the old-to-new import table, and the core never imports it.
