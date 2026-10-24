"""lionagi type definitions."""

# Lazy type resolution: try fields → models → protocols.types
_lazy_type_cache: dict[str, object] = {}

_SOURCE_MODULES = ("operations.fields", "models", "protocols.types")


def __getattr__(name: str):
    if name in _lazy_type_cache:
        return _lazy_type_cache[name]

    from lionagi.ln import import_module

    for mod_path in _SOURCE_MODULES:
        try:
            mod = import_module(f".{mod_path}", package=__name__.rpartition(".")[0])
            if hasattr(mod, name):
                obj = getattr(mod, name)
                _lazy_type_cache[name] = obj
                return obj
        except (ImportError, AttributeError):
            continue

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
