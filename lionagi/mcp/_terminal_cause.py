"""The typed cause of a run's terminal exception, handed from the CLI to the job record.

A failed job record says a run failed and stops there, so the reason has to be
read out of console prose by whoever asks next. The exception that ended the run
is a typed object inside the CLI process, and it is thrown away at exactly the
moment it becomes useful. This carries the two facts a caller can act on — which
class it was, and whether that class is worth retrying — across the process
boundary, so a caller can branch without parsing anything.

Writer and reader live together deliberately. They are in different processes and
neither imports the other, so the schema is the only thing holding them in
agreement; split across two files it would be two schemas that happen to match
today.

**The exception's message is not carried, and that is a decision rather than an
omission.** Provider messages quote whatever the provider said, which routinely
includes an API key. The credential masker in ``lionagi.state.engine`` closes the
``user:secret@`` shape and documents itself as a backstop, so it would not catch
a bare token. The message is already in the run's console log, which is one
channel with whatever exposure it already has. Copying it into the job record —
a file that gets read, quoted, and passed on by the observer verbs — would open a
second one onto the same secret for no classification value: the class and the
retry hint are the parts a caller branches on.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from lionagi.mcp import config
from lionagi.providers._provider_errors import ProviderError

__all__ = [
    "STATUS_SOURCES",
    "allowed_cause_classes",
    "provider_status",
    "read_terminal_cause",
    "write_terminal_cause",
]

# How the status question was answered. "absent" is a measurement -- the message
# was read and named no status -- and "unreadable" is the admission that it was
# not read at all. Collapsing the two would let a broken extractor report what a
# working one reports for a connection that never completed.
STATUS_SOURCES = frozenset({"received", "absent", "unreadable"})

# A provider status code as adapters spell it in a message, e.g.
# "UNAVAILABLE (code 503)". Only the digits are captured: the surrounding text
# is the provider's own prose and is exactly what must not be stored.
_STATUS_CODE_RE = re.compile(r"\(code\s+(?P<code>\d{3})\)")

# Anything else is reported as this: a terminal exception that was not one of
# our typed provider errors. Deliberately still recorded, because "the cause was
# not a provider error" tells a caller to stop looking for one.
UNKNOWN_CAUSE = "unknown"

# Bounds the read. The file this module writes is two short fields; a larger one
# was written by something else, and parsing it would be answering about a
# different file.
_MAX_CAUSE_BYTES = 4096


def allowed_cause_classes() -> frozenset[str]:
    """Every class name that may be stored, derived from the hierarchy itself.

    Derived rather than listed so a provider error added later is admissible
    without a second edit here, and so a value that is not one of our classes
    can never become one by being written into a list.
    """

    def walk(cls: type) -> set[str]:
        names = {cls.__name__}
        for sub in cls.__subclasses__():
            names |= walk(sub)
        return names

    return frozenset(walk(ProviderError) | {UNKNOWN_CAUSE})


def provider_status(exc: BaseException) -> tuple[str, int | None]:
    """Whether a provider status code reached us, and which one.

    The class alone does not separate the two ways a provider call ends badly.
    One adapter raises the same class with the same message prefix for a service
    that answered ``UNAVAILABLE (code 503)`` and for a DNS lookup that never
    resolved, and those are opposite facts: the first is the provider's own
    answer, the second means no round trip happened and the fault is on this
    side. A status code is what tells them apart, and unlike the message it
    cannot carry a credential.

    Returns the state first because it is the part that must not be inferred
    from the number. ``"received"`` carries an int; ``"absent"`` means the
    message was read and held no status, which is itself the signal that no
    round trip completed; ``"unreadable"`` means this function could not look.
    Absent and unreadable are separate for the same reason a missing status is
    not a zero: an instrument that did not run must never report what a working
    one reports when it finds nothing.
    """
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 — an exception whose str() raises is unreadable, not statusless
        return ("unreadable", None)
    match = _STATUS_CODE_RE.search(text)
    if match is None:
        return ("absent", None)
    try:
        return ("received", int(match.group("code")))
    except (ValueError, IndexError):  # pragma: no cover — the pattern only matches digits
        return ("unreadable", None)


def write_terminal_cause(exc: BaseException) -> None:
    """Record *exc*'s class, retry hint and provider status, if this run has a cause file.

    Called from the CLI's terminal exception path, where raising would replace
    the run's real failure with this one, so every failure here is silent by
    design: no cause file configured, an unwritable directory, and a serialisation
    failure all end the same way, with the record simply not gaining a cause.

    ``provider_status`` is written only when one was received. A caller reads
    ``status_source`` to know which question it is answering, and never has to
    decide what a missing number means.
    """
    target = os.environ.get(config.CAUSE_FILE_ENV_VAR)
    if not target:
        return
    try:
        # Read off the class, never the instance: `retryable` is a ClassVar
        # classification hint, and an instance attribute shadowing it would be
        # some other subsystem's runtime state, not this classification.
        cls = type(exc)
        name = cls.__name__ if issubclass(cls, ProviderError) else UNKNOWN_CAUSE
        retryable = bool(getattr(cls, "retryable", False)) if name != UNKNOWN_CAUSE else False
        source, code = provider_status(exc)
        record: dict[str, Any] = {
            "class": name,
            "retryable": retryable,
            "status_source": source,
        }
        # Present only for "received", so an absent status cannot be read as a
        # code and a code can never be confused with its own absence.
        if code is not None:
            record["provider_status"] = code
        Path(target).write_text(json.dumps(record))
    except Exception:  # noqa: BLE001, S110 — see docstring: this must not raise
        pass


def read_terminal_cause(path: str | os.PathLike[str] | None) -> dict[str, Any] | None:
    """Read a cause file back, or ``None`` if there is nothing trustworthy in it.

    Fail-closed at every step, because the caller is the terminal hook and a
    malformed file must not cost the run its record. The class name is forced
    into :func:`allowed_cause_classes` here, at the boundary that stores it,
    rather than trusted of the writer: the two run in different processes and
    different versions, so an unrecognised value is exactly what this should
    expect to see.
    """
    if not path:
        return None
    try:
        p = Path(path)
        if p.stat().st_size > _MAX_CAUSE_BYTES:
            return None
        loaded = json.loads(p.read_text())
    except Exception:  # noqa: BLE001 — absent, unreadable and malformed are one outcome
        return None
    if not isinstance(loaded, dict):
        return None
    name = loaded.get("class")
    if not isinstance(name, str) or name not in allowed_cause_classes():
        name = UNKNOWN_CAUSE
    # A status_source this reader does not recognise is "unreadable" rather than
    # "absent": an unrecognised value means the two processes disagree about the
    # schema, and that is a failure to read, not a measurement that found none.
    source = loaded.get("status_source")
    if not isinstance(source, str) or source not in STATUS_SOURCES:
        source = "unreadable"
    cause: dict[str, Any] = {
        "class": name,
        "retryable": loaded.get("retryable") is True,
        "status_source": source,
    }
    code = loaded.get("provider_status")
    # A code is carried only alongside the state that claims one, and only as an
    # int. `isinstance(True, int)` is True in Python, so bools are excluded
    # explicitly or a `true` in that field would be read back as status 1.
    if source == "received" and isinstance(code, int) and not isinstance(code, bool):
        cause["provider_status"] = code
    elif source == "received":
        cause["status_source"] = "unreadable"
    return cause
