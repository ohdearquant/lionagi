# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Typed failure for an optional feature whose declared dependency is not installed.

Standard library only: importing this module loads no optional dependency, no
adapter package and no provider hub.
"""

from __future__ import annotations

from typing import Any


def _rebuild_missing_feature_error(
    cls: type[MissingFeatureError], fields: dict[str, Any]
) -> MissingFeatureError:
    """Unpickle helper: the constructor is keyword-only, so the fields are replayed by name."""
    return cls(**fields)


class MissingFeatureError(ImportError):
    """An optional feature cannot be activated because a declared dependency is absent.

    ``extra`` is ``None`` when no optional extra repairs the absence, for example a
    bundled provider hub missing from an installation, which is repaired by
    reinstalling ``lionagi``.

    The message is built from the fields alone, so it never carries the text of a
    chained exception, an environment value or a path. ``ImportError.name`` is set to
    ``import_root`` (the missing top-level import name) when one is given, so code
    that reads ``name`` from a caught ``ImportError`` keeps working.

    Attributes:
        feature: Name of the feature that could not be activated.
        extra: Optional extra that supplies the dependency, if there is one.
        missing_distribution: Distribution that is not installed.
        requested_symbol: Public symbol whose access triggered the activation, if any.
        install_hint: Exact guidance text shown to the user.
        provider: Provider the activation was selected for, if any.
        endpoint: Provider endpoint the activation was selected for, if any.
        reason: Short machine-readable cause, if any.
        source: Declaration the requirement came from, if any.
    """

    def __init__(
        self,
        *,
        feature: str,
        missing_distribution: str,
        install_hint: str,
        extra: str | None = None,
        requested_symbol: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        reason: str | None = None,
        source: str | None = None,
        import_root: str | None = None,
    ) -> None:
        if extra is not None:
            message = (
                f"LionAGI feature {feature!r} requires optional extra {extra!r}; "
                f"install it with: {install_hint}"
            )
        else:
            message = (
                f"LionAGI feature {feature!r} requires distribution "
                f"{missing_distribution!r}, which is not installed; "
                f"guidance: {install_hint}"
            )
        super().__init__(message, name=import_root)
        self.feature: str = feature
        self.extra: str | None = extra
        self.missing_distribution: str = missing_distribution
        self.requested_symbol: str | None = requested_symbol
        self.install_hint: str = install_hint
        self.provider: str | None = provider
        self.endpoint: str | None = endpoint
        self.reason: str | None = reason
        self.source: str | None = source

    def __reduce__(self):
        # The default reduction replays ``self.args`` (the formatted message), which the
        # keyword-only constructor does not accept; rebuild from the named fields.
        fields = {
            "feature": self.feature,
            "missing_distribution": self.missing_distribution,
            "install_hint": self.install_hint,
            "extra": self.extra,
            "requested_symbol": self.requested_symbol,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "reason": self.reason,
            "source": self.source,
            "import_root": self.name,
        }
        return (_rebuild_missing_feature_error, (type(self), fields))
