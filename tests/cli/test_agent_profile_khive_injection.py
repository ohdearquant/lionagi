# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for khive injection configuration in CLI agent profiles."""

from lionagi import Branch
from lionagi.cli._providers import _parse_profile
from lionagi.cli.orchestrate._orchestration import _register_profile_providers


def test_profile_parser_promotes_khive_injection_frontmatter():
    profile = _parse_profile(
        "researcher",
        """---
khive_injection:
  profile_id: researcher-recall-v1
  compose:
    enabled: true
---
Research carefully.
""",
    )

    assert profile.khive_injection == {
        "profile_id": "researcher-recall-v1",
        "compose": {"enabled": True},
    }
    assert "khive_injection" not in profile.extra


def test_verbatim_profile_registers_provider_without_calling_khive():
    profile = _parse_profile(
        "researcher",
        """---
khive_injection: true
---
Research carefully.
""",
    )
    branch = Branch(system=profile.system_prompt)

    _register_profile_providers(branch, "researcher", profile)

    assert branch.providers.names == ["khive_injection:researcher-recall-v1"]
    provider = branch.providers._entries[0].provider
    assert provider.policy.profile_id == "researcher-recall-v1"
