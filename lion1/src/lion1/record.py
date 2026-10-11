"""The record and the view (ADR-0007).

The record is append-only and has four kinds. Every entry has a monotonic sequence number and a
short name derived from it; names are pointers and live as long as the record. What the model
sees is a view folded from context directives plus the defaults.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, get_args

__all__ = (
    "Kind",
    "Entry",
    "Op",
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
    turn: int = 0
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, Kind):  # a bare "text" too: the kind is the member, never its value
            raise ValueError(f"entry kind {self.kind!r} is not one of {', '.join(str(k) for k in Kind)}")

    @property
    def name(self) -> str:
        """The runtime's name: kind prefix plus sequence, `_in3`, `_sys12`, `_m40`, `_r41`."""
        return f"{_PREFIX[self.kind]}{self.seq}"


Op = Literal["hide", "show", "summarize"]
_OPS = get_args(Op)


@dataclass(frozen=True)
class Directive:
    """One view directive, carried by a context command's RESULT or by a fold event's SYSTEM entry."""

    op: Op
    refs: tuple[str, ...]
    text: str = ""
    at: int = 0  # the seq of the entry that carried it: a command's RESULT, or a fold event's SYSTEM
    seqs: tuple[int, ...] | None = None  # the targets, resolved once when the directive was made; None,
    # from a log row written before pins existed, makes the fold resolve `refs` by name

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"directive op {self.op!r} is not one of {', '.join(_OPS)}")


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

    def append(self, kind: Kind, content: Any, *, alias: str | None = None, turn: int = 0, **meta) -> Entry:
        if alias is not None and alias.startswith("_"):  # the record's namespace, present and future names
            raise ValueError(f"alias {alias!r} starts with _, which the record keeps for its own names")
        if alias is not None and alias in self._aliases:
            raise ValueError(f"alias {alias!r} already names {self.by_seq(self._aliases[alias]).name}")
        if alias is not None and self.get(alias) is not None:  # not an alias, so a runtime name in use
            raise ValueError(f"alias {alias!r} is the name of an entry on the record")
        entry = Entry(seq=len(self._entries), kind=kind, content=content, alias=alias, turn=turn, meta=meta)
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
            if not name.startswith(prefix):
                continue
            # ASCII digits only: `str.isdigit` also takes `²`, which `int` refuses, and `١`, which it
            # reads as 1. No seq has more significant digits than the record's length, so a longer
            # name is nothing and `int` never meets one past its digit limit.
            digits = name[len(prefix) :]
            value = digits.lstrip("0")
            if digits.isascii() and digits.isdigit() and len(value) <= len(str(len(self._entries))):
                seq = int(value or "0")
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
    hidden: bool = False  # a hidden entry: rendered as a placeholder that names it, never its value


def directives(record: Record, consumed: Iterable[Directive] = ()) -> list[Directive]:
    """Directives in the order of their `at`, off the RESULT of a context command or a notification's
    SYSTEM entry: the hides of what a program consumed, each dated at its consumption and so earlier
    than the notification carrying it, then the fold event. A consumption dated `at` happened before
    the entry at that seq landed (a reader settles before its RESULT, a branch or a note write reads
    before the next entry), so among equals the consumed hides come first: a show carried by that
    entry stands over them (ADR-0008/D3); record order settles the rest. `consumed`: hides not on the
    record yet, the ones the notification being written will carry, folded as they will be."""
    out: list[tuple[int, int, Directive]] = [(d.at, 0, d) for d in consumed]
    for e in record:
        if e.kind in (Kind.RESULT, Kind.SYSTEM):
            out += [(d.at, 0, d) for d in e.meta.get("consumed") or () if isinstance(d, Directive)]
            d = e.meta.get("directive")
            if isinstance(d, Directive):
                out.append((d.at, 1, d))
    return [d for _, _, d in sorted(out, key=lambda t: t[:2])]


def fold(
    record: Record, *, protect_input: bool = True, consumed: Iterable[Directive] = ()
) -> list[ViewEntry]:
    """The view: the record after the directives.

    Nothing leaves the view on its own: every entry stays where it landed, so one render differs from
    the last only by what was appended and the provider's prefix cache holds. Directives then apply
    by date (`at`, record order among equal dates), wherever they ride: `hide` removes, `show` brings
    back, `summarize` removes and puts its text in front of where the span began. A hidden entry
    keeps a placeholder (`hidden=True`) so the model knows what it can reopen; one under a summary
    does not, the summary names it. A directive reaches only entries earlier than its date; INPUT is
    never hidden unless `protect_input` is False;
    a name that resolves to nothing is ignored, a directive cannot crash the fold. The runtime's own
    fold event (the view past its budget, ADR-0007/C2) is a `hide` directive on the notification
    that announces it, so it happens once and replays like any other.
    """
    entries = record.entries
    hidden: set[int] = set()
    spans: list[tuple[str, set[int]]] = []  # each summary's text and the entries it still replaces

    for d in directives(record, consumed):
        seqs = d.seqs
        if seqs is None:
            seqs = [e.seq for ref in d.refs if (e := record.get(ref)) is not None]
        # earlier than its date and on the record: a row read back from a log may pin a seq past both
        targets = [s for s in seqs if 0 <= s < min(d.at, len(entries))]
        if protect_input:
            targets = [s for s in targets if entries[s].kind is not Kind.INPUT]
        if d.op == "hide":
            hidden.update(targets)
        elif d.op == "show":
            hidden.difference_update(targets)
            # A shown entry is back as itself (ADR-0007/C3) and leaves every span it was under; the
            # rest of a span stays summarized, so its summary still stands.
            for _, members in spans:
                members.difference_update(targets)
        elif d.op == "summarize" and targets:
            hidden.update(targets)
            spans.append((d.text, set(targets)))

    # A summary stands where what it still replaces begins, in the order the summaries were made.
    summarized: set[int] = set()
    summaries: dict[int, list[str]] = {}
    for text, members in spans:
        if members:
            summarized.update(members)
            summaries.setdefault(min(members), []).append(text)

    view: list[ViewEntry] = []
    for e in entries:
        for text in summaries.get(e.seq, ()):
            view.append(ViewEntry(entry=e, summary=text))
        if e.seq not in hidden:
            view.append(ViewEntry(entry=e))
        elif e.seq not in summarized:
            view.append(ViewEntry(entry=e, hidden=True))
    return view
