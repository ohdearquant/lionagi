"""The record and the view (ADR-0007).

The record is append-only and has four kinds. Every entry has a monotonic sequence number and a
short name derived from it; names are pointers and live as long as the record. What the model
sees is a view folded from context directives plus the defaults.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = (
    "Kind",
    "Entry",
    "Directive",
    "Record",
    "ViewEntry",
    "directives",
    "fold",
)


class Kind(Enum):
    INPUT = "input"
    SYSTEM = "system"
    TEXT = "text"
    RESULT = "result"


# A leading underscore: the record's names and the model's aliases never collide (an alias starting
# with `_` is refused), so `_r6` is entry 6 whatever the model called its sixth command.
_PREFIX = {Kind.INPUT: "_in", Kind.SYSTEM: "_sys", Kind.TEXT: "_m", Kind.RESULT: "_r"}


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: Kind
    content: Any
    alias: str | None = None  # the model's own name for it, when it gave one
    round: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        """The runtime's name: kind prefix plus sequence, `_in3`, `_sys12`, `_m40`, `_r41`."""
        return f"{_PREFIX[self.kind]}{self.seq}"


@dataclass(frozen=True)
class Directive:
    """One context command's effect on the view, read off its RESULT entry at fold time."""

    op: str  # hide | show | summarize
    refs: tuple[str, ...]
    text: str = ""
    at: int = 0  # the seq of the entry that carried it: a command's RESULT, or a fold event's SYSTEM
    seqs: tuple[int, ...] | None = None  # the targets, resolved once when the directive was made; a fold
    # never resolves a name again


class Record:
    """Append-only. `get(name)` resolves a runtime name (`_r41`) or a model alias to its entry."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._entries: list[Entry] = []
        self._aliases: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries)

    @property
    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries)

    @property
    def seq(self) -> int:
        """The sequence the next append will take; a checkpoint is this number."""
        return len(self._entries)

    def append(self, kind: Kind, content: Any, *, alias: str | None = None, round: int = 0, **meta) -> Entry:
        if alias is not None and alias.startswith("_"):  # the record's namespace, present and future names
            raise ValueError(f"alias {alias!r} starts with _, which the record keeps for its own names")
        if alias is not None and alias in self._aliases:
            raise ValueError(f"alias {alias!r} already names {self.by_seq(self._aliases[alias]).name}")
        if alias is not None and self.get(alias) is not None:  # not an alias, so a runtime name in use
            raise ValueError(f"alias {alias!r} is the name of an entry on the record")
        entry = Entry(seq=len(self._entries), kind=kind, content=content, alias=alias, round=round, meta=meta)
        self._entries.append(entry)
        if alias is not None:
            self._aliases[alias] = entry.seq
        return entry

    def by_seq(self, seq: int) -> Entry:
        return self._entries[seq]

    def get(self, name: str) -> Entry | None:
        """A runtime name (`_r41`) or a model alias. None when nothing is called that."""
        if name in self._aliases:
            return self._entries[self._aliases[name]]
        for kind, prefix in _PREFIX.items():
            if name.startswith(prefix) and name[len(prefix) :].isdigit():
                seq = int(name[len(prefix) :])
                if seq < len(self._entries) and self._entries[seq].kind is kind:
                    return self._entries[seq]
        return None

    def has_alias(self, alias: str) -> bool:
        return alias in self._aliases

    def latest(self, kind: Kind) -> Entry | None:
        for e in reversed(self._entries):
            if e.kind is kind:
                return e
        return None

    def of_kind(self, kind: Kind) -> list[Entry]:
        return [e for e in self._entries if e.kind is kind]


@dataclass(frozen=True)
class ViewEntry:
    entry: Entry
    summary: str | None = None  # when this entry stands in front of a hidden span
    hidden: bool = False  # a hidden RESULT: rendered as a placeholder that names it, never its value


def directives(record: Record) -> list[Directive]:
    """Context directives in record order: RESULT entries of the context commands carry them."""
    out = []
    for e in record:
        if e.kind in (Kind.RESULT, Kind.SYSTEM) and isinstance(e.meta.get("directive"), Directive):
            out.append(e.meta["directive"])
    return out


def fold(record: Record, *, protect_input: bool = True) -> list[ViewEntry]:
    """The view: the record after the directives.

    Nothing leaves the view on its own: every entry stays where it landed, so one render differs from
    the last only by what was appended and the provider's prefix cache holds. Directives then apply
    in record order: `hide` removes, `show` brings back, `summarize` removes and puts its text in
    front of where the span began. A hidden entry keeps a placeholder (`hidden=True`) so the model
    knows what it can reopen; one under a summary does not, the summary names it. A directive
    reaches only entries earlier than itself; INPUT is never hidden unless `protect_input` is False;
    a name that resolves to nothing is ignored, a directive cannot crash the fold. The runtime's own
    fold event (the view past its budget, ADR-0007/C2) is a `hide` directive on the notification
    that announces it, so it happens once and replays like any other.
    """
    entries = record.entries
    hidden: set[int] = set()
    summarized: set[int] = set()
    summaries: dict[int, list[str]] = {}

    def resolve(refs: Iterable[str]) -> list[int]:
        seqs = []
        for r in refs:
            e = record.get(r)
            if e is not None:
                seqs.append(e.seq)
        return seqs

    for d in directives(record):
        targets = [s for s in (d.seqs if d.seqs is not None else resolve(d.refs)) if s < d.at]
        if protect_input:
            targets = [s for s in targets if entries[s].kind is not Kind.INPUT]
        if d.op == "hide":
            hidden.update(targets)
        elif d.op == "show":
            hidden.difference_update(targets)
            summarized.difference_update(targets)
            # A shown entry is back as itself (ADR-0007/C3); without this the stale text in
            # `summaries` would still render at this seq, and the view would carry the entry and its
            # summary.
            for s in targets:
                summaries.pop(s, None)
        elif d.op == "summarize" and targets:
            hidden.update(targets)
            summarized.update(targets)
            summaries.setdefault(min(targets), []).append(d.text)

    view: list[ViewEntry] = []
    for e in entries:
        for text in summaries.get(e.seq, ()):
            view.append(ViewEntry(entry=e, summary=text))
        if e.seq not in hidden:
            view.append(ViewEntry(entry=e))
        elif e.seq not in summarized:
            view.append(ViewEntry(entry=e, hidden=True))
    return view
