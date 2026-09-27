"""One markdown file per role; `guidance(name, **fields)` reads one. Edit the file, not a string."""

from .load import HERE, guidance, names

__all__ = ("HERE", "guidance", "names")
