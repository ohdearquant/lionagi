# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Profile injection declaration parsing."

from lionhub.orchestration.spawn import _parse_profile
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


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
