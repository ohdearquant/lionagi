# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resolution of a codex config profile named as a model.

Lives at the provider layer rather than under ``cli/`` because both entry
points need it. A codex request built through the library --
``Branch(chat_model="codex/deepseek-flash")`` -- reaches the same spawn as one
built through ``li agent``, and while this resolution sat in the CLI package
only the CLI path got it: the library path sent the profile NAME to codex as a
model id, and codex answered with an unsupported-model error naming a model
nobody had asked for.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from lionagi.libs.path_safety import validate_bare_name

__all__ = ("resolve_codex_config_profile",)

logger = logging.getLogger(__name__)


def _unreadable_symlink_target(path: Path) -> str | None:
    """Return a broken/non-file symlink's declared target, if applicable."""
    if not path.is_symlink() or path.is_file():
        return None
    try:
        return str(path.readlink())
    except OSError:
        return "<unreadable>"


def resolve_codex_config_profile(model: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve a codex model part that names a codex config profile.

    codex reaches models from other providers through a config profile:
    ``-p <name>`` layers ``$CODEX_HOME/<name>.config.toml`` over the base
    config, and such a file names a ``model`` and the ``model_provider`` that
    serves it. So ``codex/<name>`` should mean "run that profile", not "run a
    model literally called ``<name>``".

    lionagi cannot forward this by passing ``-p``. codex accepts exactly one
    profile per invocation and lionagi already spends that slot on a generated
    profile carrying MCP server secrets, which is why supplying both is
    refused outright. The file is therefore read here and its settings applied
    directly: the profile's ``model`` becomes the model, and its remaining
    scalars become ``-c`` overrides, which outrank config either way.

    Two deliberate limits, both narrowing rather than widening:

    * Only a bare name is looked up, so an ordinary vendor model id such as
      ``deepseek/deepseek-v4-flash-0731`` is never treated as a path. Bare
      excludes dots as well as separators, so a profile must be named like
      ``deepseek-flash`` and one named ``gpt-5.6-sol`` is not resolved at all.
      That cuts the other way usefully: model ids carry dots and version
      numbers, so a profile name cannot collide with a real one and quietly
      stand in for it.
    * Table-valued keys (notably ``mcp_servers``) are not applied, and are
      logged. lionagi decides a leg's MCP server set explicitly, and quietly
      re-introducing servers from a config file would go around that.

    Returns ``None`` when no such profile file exists, leaving the name to be
    treated as a model id exactly as before.
    """
    try:
        validate_bare_name(model, label="codex config profile name")
    except ValueError:
        return None

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    profile_path = codex_home / f"{model}.config.toml"
    if not profile_path.is_file():
        # A symlink whose target is unreadable is not the same as no profile.
        # `is_file()` follows the link and answers False for both, so falling
        # through here would send the name to codex as a model id — the exact
        # silent substitution this function exists to prevent, arriving through
        # a file the operator can see sitting right there.
        broken_target = _unreadable_symlink_target(profile_path)
        if broken_target is not None:
            raise ValueError(
                f"codex config profile {str(profile_path)!r} is a symlink whose "
                f"target {broken_target!r} is unreadable. Repair or remove the "
                f"link; running without it would send {model!r} to codex as a "
                f"model id and silently run something else."
            )
        return None

    import toml

    try:
        data = toml.loads(profile_path.read_text())
    except (OSError, toml.TomlDecodeError) as exc:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} could not be read "
            f"({type(exc).__name__}: {exc}). Fix or remove the file; running "
            f"without it would send {model!r} as a model id instead."
        ) from exc

    resolved = data.get("model")
    if not isinstance(resolved, str) or not resolved:
        raise ValueError(
            f"codex config profile {str(profile_path)!r} declares no 'model'. "
            f"Add one, or use a model id instead of the profile name — "
            f"without it {model!r} would be sent to codex as a model id and "
            f"silently run something other than the profile."
        )

    overrides: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in data.items():
        if key == "model":
            continue
        if isinstance(value, (str, int, float, bool)):
            overrides[key] = value
        else:
            skipped.append(key)
    if skipped:
        logger.warning(
            "codex config profile %r: ignoring %s — lionagi applies a profile's "
            "model and scalar settings, and sets a leg's MCP servers itself",
            model,
            ", ".join(sorted(skipped)),
        )

    # Say which model is actually being run. A profile whose name collides with
    # a real model id would otherwise substitute without a word, which is the
    # quiet half of the same failure this function fixes: the caller asks for
    # one thing, a different thing runs, and the leg looks healthy either way.
    logger.info("codex profile %r resolves to model %r", model, resolved)
    return resolved, overrides
