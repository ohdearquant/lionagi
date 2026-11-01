# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Token budget tracking — context window lookup and usage calculation per branch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lionagi.service.connections.provider_catalog import ProviderCatalog
    from lionagi.session.branch import Branch

_DEFAULT_CONTEXT_WINDOW = 128_000


def lookup_context_window(
    model_name: str,
    provider: str | None = None,
    *,
    provider_catalog: ProviderCatalog | None = None,
) -> int:
    """Return the context window for model_name from a provider catalog.

    The catalog tries the hinted provider's window table first, then every provider's table in
    its declared order; within a table the longest key contained in the lower-cased model name
    wins. A model no table matches gets the 128,000-token estimate. A hint counts only when its
    lower-cased form is a provider key of the catalog; an alias or any other name is no hint.

    ``provider_catalog`` is used as given; when it is None the installed provider hub's catalog
    is loaded. A hub that is missing or cannot be loaded raises the loader's typed error
    (``MissingFeatureError``, ``ProviderContractVersionError`` or ``ProviderActivationError``)
    rather than answering with the estimate.
    """
    if provider_catalog is None:
        from lionagi.service.connections._provider_compat import default_provider_catalog

        provider_catalog = default_provider_catalog()
    # The earlier lookup matched a hint against provider keys only, so an alias or a padded
    # name was no hint; the catalog also resolves aliases, which would read another table.
    provider_keys = {fragment.key for fragment in provider_catalog.providers}
    if provider is not None and provider.lower() not in provider_keys:
        provider = None
    window = provider_catalog.context_window(model_name, provider)
    return _DEFAULT_CONTEXT_WINDOW if window is None else window


@dataclass(frozen=True)
class TokenBudget:
    used: int
    limit: int
    model: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def usage_pct(self) -> float:
        return self.used / self.limit if self.limit > 0 else 0.0

    @property
    def is_warning(self) -> bool:
        return self.usage_pct >= 0.7

    @property
    def is_critical(self) -> bool:
        return self.usage_pct >= 0.9


def get_context_window(branch: Branch) -> int:
    """Resolve context window: endpoint config > provider lookup > default (128k).

    The provider lookup reads the catalog the branch's chat model was built with
    (``provider_catalog``). A model built without one uses the installed provider hub's catalog.
    """
    try:
        endpoint = branch.chat_model.endpoint
        if getattr(endpoint.config, "context_window", None):
            return endpoint.config.context_window

        model_name = ""
        provider = getattr(endpoint.config, "provider", None)
        if hasattr(endpoint.config, "kwargs"):
            model_name = endpoint.config.kwargs.get("model", "")
        if not model_name and hasattr(endpoint.config, "params"):
            model_name = endpoint.config.params.get("model", "")

        if model_name:
            return lookup_context_window(
                model_name,
                provider,
                provider_catalog=getattr(branch.chat_model, "provider_catalog", None),
            )
    except (AttributeError, KeyError):
        pass

    return _DEFAULT_CONTEXT_WINDOW


def get_token_budget(branch: Branch) -> TokenBudget:
    """Calculate current token budget for a branch."""
    from lionagi.service.token_calculator import TokenCalculator

    limit = get_context_window(branch)
    progression = branch.progression
    pile = branch.msgs.messages

    used = 0
    for uid in progression:
        if uid in pile:
            msg = pile[uid]
            c = msg.content if hasattr(msg, "content") else ""
            if c:
                used += TokenCalculator.tokenize(str(c) if not isinstance(c, str) else c)

    model_name = None
    try:
        if hasattr(branch.chat_model.endpoint.config, "kwargs"):
            model_name = branch.chat_model.endpoint.config.kwargs.get("model")
    except AttributeError:
        pass

    return TokenBudget(used=used, limit=limit, model=model_name)
