# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
import toml

from lionagi.providers.openai.codex import CodexCodeRequest


def _config_values(args: list[str], key: str) -> list[object]:
    end = args.index("--") if "--" in args else len(args)
    values: list[object] = []
    for index, token in enumerate(args[:end]):
        if token != "-c" or index + 1 >= end:
            continue
        pair = args[index + 1]
        pair_key, separator, raw = pair.partition("=")
        if separator and pair_key == key:
            values.append(toml.loads(f"value = {raw}")["value"])
    return values


@pytest.mark.parametrize("mode", ["untrusted", "on-request", "never"])
def test_approval_mode_uses_the_equivalent_config_key(mode: str) -> None:
    args = CodexCodeRequest(prompt="review this", ask_for_approval=mode).as_cmd_args()

    assert _config_values(args, "approval_policy") == [mode]
    assert "-a" not in args
    assert "--ask-for-approval" not in args


def test_approval_mode_keeps_the_requested_sandbox() -> None:
    args = CodexCodeRequest(
        prompt="review this",
        ask_for_approval="on-request",
        sandbox="read-only",
    ).as_cmd_args()

    sandbox_index = args.index("-s")
    assert args[sandbox_index + 1] == "read-only"
    assert _config_values(args, "approval_policy") == ["on-request"]


@pytest.mark.parametrize(
    "override",
    [
        {"approval_policy": "never"},
        {"approval_policy.granular.rules": False},
    ],
)
def test_approval_mode_rejects_a_second_config_source(override: dict[str, object]) -> None:
    with pytest.raises(
        ValueError,
        match=(
            "ask_for_approval cannot be combined with config_overrides entries for approval_policy"
        ),
    ):
        CodexCodeRequest(
            prompt="review this",
            ask_for_approval="on-request",
            config_overrides=override,
        )


def test_bypass_still_takes_precedence_over_approval_mode() -> None:
    with pytest.warns(UserWarning, match="skips ALL approval prompts"):
        args = CodexCodeRequest(
            prompt="review this",
            ask_for_approval="on-request",
            sandbox="read-only",
            bypass_approvals=True,
        ).as_cmd_args()

    assert "--dangerously-bypass-approvals-and-sandbox" in args
    assert _config_values(args, "approval_policy") == []
    assert "-s" not in args


def test_full_auto_still_takes_precedence_over_approval_mode() -> None:
    args = CodexCodeRequest(
        prompt="review this",
        ask_for_approval="on-request",
        sandbox="read-only",
        full_auto=True,
    ).as_cmd_args()

    assert _config_values(args, "approval_policy") == []
    assert "-s" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
