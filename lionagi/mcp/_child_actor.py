# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A write-capable child's own khive actor, minted into its tree at spawn.

khive resolves an actor only from ``<cwd>/.khive/config.toml``, with no ancestor walk. A child run
in the submitting directory therefore signs as the submitter and reads the submitter's inbox, and a
child run in a tree with no config signs as the unattributed pool. Neither is the child. The child
actor is the submitting actor plus the suffix the server is configured with, and its config is the
submitting config copied whole with only the ``[actor]`` table rewritten.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from typing import Any

from lionagi.ln import compute_hash

try:
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml  # type: ignore[no-redef]

__all__ = [
    "ChildActorError",
    "MintedChildActor",
    "SubmittingActorError",
    "child_actor_for",
    "mint_child_actor_config",
]

_CONFIG = os.path.join(".khive", "config.toml")
_ACTOR_HEADER = re.compile(r"^\[actor\]\s*$")
# a table or array-of-tables header ends the [actor] block
_ANY_HEADER = re.compile(r"^\[")


@dataclass(frozen=True)
class MintedChildActor:
    actor: str
    submitting_actor: str
    config_path: str
    source_path: str
    reused: bool


class ChildActorError(ValueError):
    """A write-capable child's actor config cannot be minted, so the run is refused."""


class SubmittingActorError(ChildActorError):
    """The server's own submitting identity or its child suffix cannot yield a child actor.

    A condition of the server's directory and settings rather than of the caller's input, so a
    caller cannot correct it by changing the submission.
    """


def _has_space_or_control(text: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in text)


def child_actor_for(submitting_actor: str, suffix: str) -> str:
    if not isinstance(submitting_actor, str) or not submitting_actor.strip():
        raise SubmittingActorError(
            "the submitting actor is empty, so no child actor can be derived from it"
        )
    if not isinstance(suffix, str) or not suffix:
        raise SubmittingActorError("the child actor suffix is empty")
    if _has_space_or_control(suffix):
        raise SubmittingActorError(
            f"the child actor suffix {suffix!r} holds whitespace or a control character"
        )
    if _has_space_or_control(submitting_actor):
        raise SubmittingActorError(
            f"the submitting actor {submitting_actor!r} holds whitespace or a control character, "
            f"so no child actor is derived from it"
        )
    if submitting_actor == "local":
        raise SubmittingActorError(
            "the submitting actor is 'local', the unattributed pool; a child actor is derived only "
            "from a named identity"
        )
    if submitting_actor.endswith(suffix):
        raise SubmittingActorError(
            f"the submitting actor {submitting_actor!r} is already a child actor; a child does not derive "
            f"another child under its own identity"
        )
    return submitting_actor + suffix


def _without_actor(parsed: dict[str, Any]) -> dict[str, Any]:
    rest = dict(parsed)
    rest.pop("actor", None)
    return rest


def _read_regular_file(path: str) -> bytes:
    """The bytes of ``path``, refused with ``OSError`` unless it is a regular file.

    The open does not block, so a FIFO at ``path`` is refused instead of waiting for a writer.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError(f"{path!r} is not a regular file")
        return f.read()


def mint_child_actor_config(submitting_dir: str, child_dir: str, suffix: str) -> MintedChildActor:
    """Write ``<child_dir>/.khive/config.toml`` naming the derived child actor, or refuse.

    Refuses (``ChildActorError``) rather than falling through to the submitting identity or to the
    unattributed pool. Nothing is written unless the new text parses back to the child actor block
    plus exactly the submitting config's other tables.
    """
    source = os.path.join(submitting_dir, _CONFIG)
    target = os.path.join(child_dir, _CONFIG)
    where = f"(submitting config {source!r}, child config {target!r})"

    if not os.path.isfile(source):
        raise SubmittingActorError(
            "the submitting directory holds no .khive/config.toml, so no submitting actor exists "
            f"to derive a child actor from {where}"
        )
    try:
        # Checked as a file above, but read without blocking and refused unless it still is one:
        # another process can replace it in between.
        source_bytes = _read_regular_file(source)
        source_text = source_bytes.decode("utf-8")
        source_parsed = _toml.loads(source_text)
    except Exception as exc:
        raise SubmittingActorError(
            f"the submitting config cannot be read or parsed: {exc} {where}"
        ) from exc
    actor_table = source_parsed.get("actor")
    submitting_actor = actor_table.get("id") if isinstance(actor_table, dict) else None
    if not isinstance(submitting_actor, str) or not submitting_actor:
        raise SubmittingActorError(f"the submitting config names no [actor].id {where}")
    try:
        child_actor = child_actor_for(submitting_actor, suffix)
    except SubmittingActorError as exc:
        raise SubmittingActorError(f"{exc} {where}") from exc

    if not os.path.isdir(child_dir):
        raise ChildActorError(
            f"the child directory {child_dir!r} is not an existing directory {where}"
        )
    if os.path.realpath(child_dir) == os.path.realpath(submitting_dir):
        raise ChildActorError(
            f"the child's tree is the submitting directory; a write-capable run needs its own tree "
            f"{where}"
        )

    remaining_tables = _without_actor(source_parsed)
    expected_actor = {
        "id": child_actor,
        "display_name": child_actor,
        "visible_namespaces": [submitting_actor],
    }
    if os.path.lexists(target):
        try:
            existing = _toml.loads(_read_regular_file(target).decode("utf-8"))
        except Exception as exc:
            raise ChildActorError(
                f"the child directory already holds a config that cannot be read or parsed: {exc} "
                f"{where}"
            ) from exc
        existing_actor = existing.get("actor")
        found = existing_actor.get("id") if isinstance(existing_actor, dict) else None
        if found != child_actor:
            raise ChildActorError(
                f"the child directory already holds a config naming actor {found!r}, not the "
                f"minted actor {child_actor!r}; refusing to overwrite another actor's config {where}"
            )
        if existing_actor != expected_actor or _without_actor(existing) != remaining_tables:
            raise ChildActorError(
                f"the child directory already holds a config naming {child_actor!r} whose "
                f"[actor] table or other tables differ from the ones this submission would mint, "
                f"as when the submitting config changed after it was minted; refusing to "
                f"overwrite it, remove it to have it minted again {where}"
            )
        return MintedChildActor(child_actor, submitting_actor, target, source, reused=True)

    lines = source_text.split("\n")
    headers = [i for i, line in enumerate(lines) if _ACTOR_HEADER.match(line)]
    if len(headers) != 1:
        raise SubmittingActorError(
            f"the submitting config carries {len(headers)} '[actor]' header lines; exactly one is "
            f"to rewrite it {where}"
        )
    start = headers[0]
    end = next((i for i in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
    # Blank and comment lines directly before the next header introduce THAT table (the submitting
    # config headlines each block with a comment banner), so they stay; only the actor keys go.
    while end > start + 1 and (
        not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
    ):
        end -= 1
    block = [
        "[actor]",
        f"id = {json.dumps(child_actor, ensure_ascii=False)}",
        f"display_name = {json.dumps(child_actor, ensure_ascii=False)}",
        f"visible_namespaces = [{json.dumps(submitting_actor, ensure_ascii=False)}]",
    ]
    tail = lines[end:]
    if tail and tail[0].strip():
        tail = ["", *tail]
    sha8 = compute_hash(source_bytes, max_bytes=None)[:8]
    header = (
        f"# CHILD ACTOR CONFIG — {child_actor}, minted by lionagi at spawn from {source} "
        f"(source sha256 prefix {sha8}); only [actor] differs."
    )
    new_text = "\n".join([header, *lines[:start], *block, *tail])

    # The text is derived only from the submitting config and the server's suffix, so a text that
    # does not verify is a fault in the server's own settings, not in the caller's cwd.
    try:
        minted = _toml.loads(new_text)
    except Exception as exc:
        raise SubmittingActorError(f"the minted config does not parse: {exc} {where}") from exc
    if minted.get("actor") != expected_actor or _without_actor(minted) != remaining_tables:
        raise SubmittingActorError(
            f"the minted config does not verify: [actor] must be exactly {expected_actor!r} and "
            f"every other table equal to the submitting config's; nothing was written {where}"
        )

    khive_dir = os.path.dirname(target)
    tmp = None
    try:
        os.makedirs(khive_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=khive_dir, prefix=".config.toml.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
        os.replace(tmp, target)
    except OSError as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise ChildActorError(f"the child config could not be written: {exc} {where}") from exc
    return MintedChildActor(child_actor, submitting_actor, target, source, reused=False)
