# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Agent profile MCP containment declaration."""

from __future__ import annotations

import sys
from datetime import date

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.spawn import AgentProfile, _parse_profile, profile_config
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_profile_mcp_none_is_a_recognized_runtime_setting():
    profile = _parse_profile("reviewer", "---\nmcp: none\n---\nReview.")

    assert profile.mcp == "none"
    assert "mcp" not in profile.extra
    assert profile_config(profile)["mcp"] == "none"


def test_profile_without_mcp_keeps_legacy_defaults():
    profile = _parse_profile("reviewer", "---\nmodel: codex/gpt-5.5\n---\nReview.")

    assert profile.mcp is None
    assert AgentProfile(name="manual").mcp is None
    assert "mcp" not in profile_config(profile)


@pytest.mark.parametrize("value", ["other", "null", "true", "false", "0", "[]", "{}", "'NONE'"])
def test_profile_mcp_invalid_value_is_refused_without_echoing_it(value):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\nmcp: {value}\n---\nReview.")

    message = str(raised.value)
    assert "reviewer" in message
    assert "mcp" in message
    assert "must be 'none'" in message
    if value == "other":
        assert value not in message


@pytest.mark.parametrize(
    "frontmatter",
    [
        "mcp: none\nmcp: none\n",
        "mcp: invalid\nmcp: none\n",
        "\"mcp\": none\n'mcp': none\n",
        "defaults: &defaults {mcp: none}\n<<: *defaults\nmcp: none\n",
        "mcp: [unterminated\n",
        "mcp: none\nmodel: [unterminated\n",
    ],
)
def test_duplicate_or_unreadable_mcp_frontmatter_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


def test_unclosed_mcp_frontmatter_is_refused():
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", "---\nmcp: none\n")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


def test_unclosed_unreadable_backslash_explains_and_both_delimiter_repairs_work():
    settings = "model: codex/gpt-5.5\n"
    body = "Review \\local\n"
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", "---\n" + settings + body)
    message = str(raised.value)
    assert "no closing ---" in message
    assert "add" in message and "closing ---" in message
    assert "remove" in message and "opening ---" in message
    assert _parse_profile("reviewer", "---\n" + settings + "---\n" + body).mcp is None
    assert _parse_profile("reviewer", settings + body).mcp is None


@pytest.mark.parametrize("indent", ["\t", "  \t", "\t  "])
@pytest.mark.parametrize("prefix", ["name: reviewer\n", "model: [unterminated\n"])
def test_tab_indented_mcp_in_unreadable_frontmatter_is_refused(indent, prefix):
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", f"---\n{prefix}{indent}mcp: none\n---\nReview.")


@pytest.mark.parametrize(
    "block",
    [
        "mcp: none\n",
        "'mcp': none\n",
        "defaults: &d {mcp: none}\n<<: *d\n",
        "mcp: none\nmodel: [unterminated\n",
        "\t'mcp': none\nmodel: [unterminated\n",
    ],
)
@pytest.mark.parametrize("closing", ["---\nReview.", ""])
def test_mcp_after_preface_is_refused_with_repair(block, closing):
    text = f"# Reviewer\n---\n{block}{closing}"
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", text)
    message = str(raised.value)
    assert "reviewer" in message and "mcp" in message
    assert "line 2" in message and "top" in message


def test_mcp_after_same_line_preface_is_refused():
    with pytest.raises(ConfigurationError, match=r"line 1.*top"):
        _parse_profile("reviewer", "Reviewer ---\nmcp: none\n---\nReview.")


@pytest.fixture(params=[1000, 3000], ids=["default-limit", "raised-limit"])
def recursion_limit(request):
    # A library can raise the limit for the whole process (jedi sets 3000 on import),
    # so a depth refusal is checked at the default and at a raised limit.
    saved = sys.getrecursionlimit()
    sys.setrecursionlimit(request.param)
    yield request.param
    sys.setrecursionlimit(saved)


def _nested_flow(depth: int) -> str:
    return "x: " + "[" * depth + "a" + "]" * depth + "\n"


@pytest.mark.parametrize("position", ["before", "after"])
def test_deep_closed_mcp_frontmatter_is_refused(position, recursion_limit):
    nested = _nested_flow(500)
    block = "mcp: none\n" + nested if position == "before" else nested + "mcp: none\n"
    with pytest.raises(ConfigurationError, match="deeper than 64 levels"):
        _parse_profile("reviewer", "---\n" + block + "---\nReview.")


def test_deep_closed_frontmatter_without_mcp_is_refused(recursion_limit):
    with pytest.raises(ConfigurationError, match="deeper than 64 levels"):
        _parse_profile("reviewer", "---\n" + _nested_flow(65) + "---\nReview.")


def _compose_out_of_stack(monkeypatch):
    import yaml

    def out_of_stack(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(yaml, "compose", out_of_stack)


@pytest.mark.parametrize("mcp", ["", "mcp: none\n"])
def test_stack_exhausted_by_the_caller_is_not_reported_as_deep_frontmatter(monkeypatch, mcp):
    # A caller already near the recursion limit can exhaust the stack on a shallow block;
    # that is the caller's error, not a nesting refusal naming the profile.
    _compose_out_of_stack(monkeypatch)
    with pytest.raises(RecursionError):
        _parse_profile("reviewer", "---\n" + mcp + "model: example\n---\nReview.")


def test_stack_exhausted_on_deep_frontmatter_is_the_nesting_refusal(monkeypatch):
    _compose_out_of_stack(monkeypatch)
    with pytest.raises(ConfigurationError, match="deeper than 64 levels"):
        _parse_profile("reviewer", "---\n" + _nested_flow(65) + "---\nReview.")


def _aliased_stages(stages: int = 40, width: int = 30) -> str:
    # Each stage is written inside the one before it and anchored; a later top-level key
    # aliases it, so the shortest path to every stage is two levels however deep it is written.
    inner = "a"
    for k in reversed(range(stages + 1)):
        inner = f"&A{k} " + "[" * width + inner + "]" * width
    keys = [f"b{k}: *A{k}" for k in range(1, stages)]
    return "\n".join(["a: " + inner, *keys, "mcp: none"]) + "\n"


def test_deep_closed_frontmatter_reached_through_aliases_is_refused(recursion_limit):
    with pytest.raises(ConfigurationError, match="deeper than 64 levels"):
        _parse_profile("reviewer", "---\n" + _aliased_stages() + "---\nReview.")


def test_deep_unclosed_frontmatter_reached_through_aliases_is_refused(recursion_limit):
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", "---\n" + _aliased_stages())
    lenient = _aliased_stages().replace("mcp: none", "other: none")
    assert _parse_profile("reviewer", "---\n" + lenient).mcp is None


def test_closed_frontmatter_at_the_depth_bound_is_read(recursion_limit):
    # The top mapping is one level, so 63 lists under it reach the bound of 64.
    flow = _parse_profile("reviewer", "---\nmcp: none\n" + _nested_flow(63) + "---\nReview.")
    assert flow.mcp == "none" and "x" in flow.extra
    lines = "".join("  " * i + f"k{i}:\n" for i in range(63)) + "  " * 63 + "v: 1\n"
    block = _parse_profile("reviewer", "---\n" + lines + "---\nReview.")
    assert "k0" in block.extra
    with pytest.raises(ConfigurationError, match="deeper than 64 levels"):
        _parse_profile(
            "reviewer",
            "---\n" + lines.replace("v: 1", "k63:\n" + "  " * 64 + "v: 1") + "---\nReview.",
        )


@pytest.mark.parametrize(
    "text",
    [
        "# Reviewer\n```yaml\n---\nmcp: none\n---\n```",
        "# Reviewer\n~~~yaml\n---\nmcp: none\nmodel: [broken\n---\n~~~",
        "# Reviewer\n```yaml\n---\n'mcp': none\nmodel: [broken\n---\n```",
        "# Reviewer\n---\n# mcp: none\nmodel: example\n---\nReview.",
        "# Reviewer\n---\nprompt: |\n  mcp: none\n---\nReview.",
    ],
)
def test_mcp_examples_after_preface_do_not_warn(monkeypatch, text):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    profile = _parse_profile("reviewer", text)
    assert profile.raw_body == text
    assert profile.mcp is None
    assert not any("mcp" in warning for warning in warnings)


def test_unrelated_unreadable_frontmatter_keeps_existing_leniency():
    profile = _parse_profile("reviewer", "---\nmodel: [unterminated\n---\nReview.")
    assert profile.mcp is None


def test_profile_loads_with_an_unreadable_date(monkeypatch):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    profile = _parse_profile("p", "---\nDeadline: 2026-02-30\n---\nBody.\n")
    assert profile.raw_body == "Body."
    assert "Deadline" not in profile.extra
    assert any("frontmatter" in warning and "ignored" in warning for warning in warnings)


@pytest.mark.parametrize(
    ("block", "ending"),
    [
        ("disallowed_tools: [Example]\nDeadline: 2026-02-30\n", "---\nBody.\n"),
        ("'allowed_tools': [Example]\nDeadline: 2026-02-30\n", "---\nBody.\n"),
        ('"tools": [Example]\nDeadline: 2026-02-30\n', "---\nBody.\n"),
        ("disallowed_tools: [Example]\ninvalid: [\n", "---\nBody.\n"),
        ("disallowed_tools: [Example]\n", ""),
        ("description: |\n  notes\ntools: [Example]\ninvalid: [\n", "---\nBody.\n"),
        ("{disallowed_tools: [Example], bad: [}\n", "---\nBody.\n"),
        ("? disallowed_tools\n: [Example]\nbad: [\n", "---\nBody.\n"),
        ("&example !!str disallowed_tools: [Example]\nbad: [\n", "---\nBody.\n"),
        (
            'description: "Example settings:\n  {disallowed_tools: [Example]}"\nbad: [\n',
            "---\nBody.\n",
        ),
    ],
    ids=[
        "deny-list-date",
        "allow-list-date",
        "tools-date",
        "deny-list-syntax",
        "unclosed",
        "key-after-scalar",
        "flow-mapping",
        "explicit-key",
        "anchored-tagged-key",
        "multiline-double-quoted-scalar",
    ],
)
def test_unreadable_profile_tool_settings_are_refused(block, ending):
    with pytest.raises(ConfigurationError, match="agent profile 'reviewer'.*tool settings"):
        _parse_profile("reviewer", f"---\n{block}{ending}")


def test_readable_profile_keeps_tool_settings():
    profile = _parse_profile(
        "reviewer",
        "---\ntools: [First, Second]\nallowed_tools: [First]\ndisallowed_tools: [Second]\n---\n",
    )
    assert profile.extra == {
        "tools": ["First", "Second"],
        "allowed_tools": ["First"],
        "disallowed_tools": ["Second"],
    }


@pytest.mark.parametrize(
    "mention",
    [
        "description: 'allowed_tools: [Example]'\n",
        "# Notes about tools\n",
        "description: |-\n  tools: [Example]\n",
        "description: >2 # Example\n  disallowed_tools: [Example]\n",
        "description:\n  - |-\n    allowed_tools: [Example]\n",
    ],
)
def test_tool_names_in_an_unreadable_profile_value_or_comment_are_refused(mention):
    with pytest.raises(ConfigurationError, match="agent profile 'reviewer'.*tool settings"):
        _parse_profile("reviewer", f"---\n{mention}Deadline: 2026-02-30\n---\nBody.\n")


@pytest.mark.parametrize(
    "text",
    [
        "You are a reviewer.\n\n---\n\n**Rules**\n* Use the tools: read: carefully\n\n---\n\nEnd.\n",
        "You are a reviewer.\n\n---\n\n**Rules**\n* Use the tools: read: carefully\n",
        "Intro ---\n* Use the tools: read: carefully\n---\nEnd.\n",
    ],
    ids=["closed-rule", "single-rule", "hidden-block"],
)
def test_markdown_after_prompt_text_that_mentions_tools_loads(text):
    profile = _parse_profile("reviewer", text)
    assert "tools" in profile.raw_body
    assert profile.extra == {}


@pytest.mark.parametrize(
    "declaration",
    [
        "disallowed_tools: [Example]\n",
        "'allowed_tools': [Example]\n",
        "- tools: [Example]\n",
        "&example !!str disallowed_tools: [Example]\n",
        "? disallowed_tools\n: [Example]\n",
        "{bad: x, disallowed_tools: [Example]}\n",
    ],
    ids=["key", "quoted-key", "list-item", "anchored-tagged-key", "explicit-key", "flow-mapping"],
)
@pytest.mark.parametrize(
    ("before", "after"),
    [("Prompt.\n\n---\n", "bad: [\n---\nEnd.\n"), ("Intro ---\n", "bad: [\n---\nEnd.\n")],
    ids=["after-prose", "hidden-block"],
)
def test_a_tool_key_line_in_an_unreadable_block_after_prompt_text_is_refused(
    declaration, before, after
):
    with pytest.raises(ConfigurationError, match="agent profile 'reviewer'.*tool settings"):
        _parse_profile("reviewer", f"{before}{declaration}{after}")


@pytest.mark.parametrize("key", ("tools", "allowed_tools", "disallowed_tools"))
@pytest.mark.parametrize(
    "declaration",
    ("{key}: [Example]", "- {key}: [Example]", "{{{key}: [Example]}}"),
    ids=("mapping", "list-item", "flow-mapping"),
)
def test_readable_tool_setting_after_prompt_text_is_refused(key, declaration):
    text = f"Prompt.\n\n---\n{declaration.format(key=key)}\n---\nEnd."
    with pytest.raises(
        ConfigurationError,
        match=rf"--- block on line 3.*{key} declaration on line 4.*move the setting",
    ):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    ("prefix", "line"),
    [
        ("\n\n---\nmodel: example\n---\n\nReview.\n---\n", 10),
        ("---\n{}\n---\nReview.\n---\n", 7),
        ("\nReview ---\n\n", 5),
    ],
    ids=["after-leading-settings", "after-empty-mapping", "prose-opener"],
)
@pytest.mark.parametrize(
    "declaration",
    [
        "allowed_tools: [Example]",
        r'"allowed\u005ftools": [Example]',
        "defaults: &defaults {allowed_tools: [Example]}\n<<: *defaults",
    ],
    ids=["plain-key", "escaped-key", "merged-key"],
)
def test_misplaced_readable_tool_setting_names_its_source_line(prefix, line, declaration):
    text = f"{prefix}# A comment before the setting.\n{declaration}\n---\nEnd."
    with pytest.raises(ConfigurationError, match=rf"allowed_tools declaration on line {line}\b"):
        _parse_profile("reviewer", text)


def test_tools_prose_after_a_rule_names_its_source_line():
    text = "Review.\n---\n\n# A comment before the prose.\ntools: Use Read to open files.\n---"
    with pytest.raises(
        ConfigurationError, match=r"--- block on line 2.*tools declaration on line 5"
    ):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    ("prefix", "line"),
    [
        ("\n---\n", 4),
        ("---\nmodel: example\n---\nReview.\n---\n", 7),
        ("\nReview ---\n\n", 5),
        ("Review.\n---\n", 4),
    ],
    ids=["leading", "after-leading-settings", "prose-opener", "after-prompt"],
)
@pytest.mark.parametrize(
    ("spelling", "supported"),
    [("allowed-tools", "allowed_tools"), ("disallowed-tools", "disallowed_tools")],
)
def test_hyphenated_tool_setting_names_the_supported_spelling(prefix, line, spelling, supported):
    text = f"{prefix}# A comment before the setting.\n{spelling}: [Example]\n---\nEnd."
    with pytest.raises(
        ConfigurationError, match=rf"'{spelling}' on line {line}\b.*use '{supported}'"
    ):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    "body",
    [
        "A plain example:\nallowed_tools: [Example]",
        "A fenced example:\n```yaml\n---\nallowed_tools: [Example]\n---\n```\nEnd.",
        "An unclosed fence:\n```yaml\nallowed_tools: [Example]\nEnd.",
        "A mapping value:\n---\ndescription:\n  - - allowed_tools: [Example]\n---\nEnd.",
        "A scalar example:\n---\ndescription: |\n  allowed_tools: [Example]\n---\nEnd.",
    ],
    ids=["plain", "fenced", "unclosed-fence", "mapping-value", "scalar"],
)
def test_tool_examples_leave_valid_leading_settings_and_prompt_intact(body, monkeypatch):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    text = f"---\nallowed_tools: [Read]\n---\n{body}"
    profile = _parse_profile("reviewer", text)
    assert profile.extra == {"allowed_tools": ["Read"]}
    assert profile.raw_body == body
    assert warnings == []


@pytest.mark.parametrize(
    ("leading", "prompt", "line", "settings"),
    [
        ("", "Review.\n", 4, {}),
        ("\n---\nallowed_tools: [Read]\n---\n", "Review.\n", 8, {"allowed_tools": ["Read"]}),
        ("", "Review ---\n---\nPrompt.\n", 6, {}),
    ],
    ids=["prompt-only", "after-leading-settings", "prose-opener"],
)
@pytest.mark.parametrize(
    ("opener", "incomplete_closer"),
    [("```", ""), ("~~~", "```"), ("````", "```")],
    ids=["unclosed", "other-fence", "short-closer"],
)
def test_final_unclosed_fence_with_tool_settings_warns_once(
    monkeypatch, leading, prompt, line, settings, opener, incomplete_closer
):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    body = (
        f"{prompt}{opener}yaml\n---\nallowed_tools: [Example]\n---\n"
        f"{incomplete_closer}\n---\ndisallowed_tools: [Other]\n---\nEnd."
    )

    profile = _parse_profile("reviewer", leading + body)

    assert profile.raw_body == body
    assert profile.extra == settings
    assert len(warnings) == 1
    [warning] = warnings
    assert f"tool settings on line {line} " in warning
    assert "unclosed code fence" in warning
    assert "leading frontmatter" in warning
    assert "Example" not in warning and "Other" not in warning


@pytest.mark.parametrize(
    "body",
    [
        "Example:\n~~~yaml\n---\ntools: [Example]\n---\n~~~",
        "Example:\n```yaml\n---\ndescription: |\n  tools: [Example]\n---",
        "Example:\n```yaml\n---\ntools: Use Read to open files.\n---",
        "Example:\n```yaml\n---\ntools: [Example]",
    ],
    ids=["closed-tilde", "scalar-example", "prose", "unpaired-rule"],
)
def test_fence_without_a_paired_tool_declaration_stays_silent(body, monkeypatch):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    assert _parse_profile("reviewer", body).raw_body == body
    assert warnings == []


@pytest.mark.parametrize(
    ("prefix", "body", "line"),
    [
        ("Review ---\n", "~~~yaml\n---\ntools: [Example]\n---\n~~~", None),
        ("Review ---\n", "~~~yaml\n---\ntools: [Example]\n---", 4),
        ("Review ---\n", "~~~yaml\ntools: [Example]", None),
        ("", "~~~yaml ---\ntools: [Example]\n~~~", None),
    ],
    ids=["closed", "unclosed-paired", "unclosed-unpaired", "fence-info"],
)
def test_first_line_prose_rule_leaves_fenced_examples_in_prompt(prefix, body, line, monkeypatch):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    text = prefix + body

    profile = _parse_profile("reviewer", text)

    assert profile.raw_body == text
    assert profile.extra == {}
    if line is None:
        assert warnings == []
    else:
        [warning] = warnings
        assert f"tool settings on line {line} are inside an unclosed code fence" in warning


@pytest.mark.parametrize(
    ("before_close", "line"),
    [("~~~yaml\n---\ntools: [Example]\n---\n~~~\n", 8), ("", 3)],
    ids=["fenced-example", "no-fence"],
)
def test_first_line_prose_rule_refuses_tools_after_first_unfenced_close(before_close, line):
    text = f"Review ---\n{before_close}---\ntools: [Read]\n---"

    with pytest.raises(
        ConfigurationError,
        match=rf"--- block on line {line - 1}\b.*tools declaration on line {line}\b",
    ):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    ("declaration", "diagnostic"),
    [
        ("mcp: none", "follows prompt text and its mcp declaration"),
        ("workload: light", "follows text on lines of its own, so its workload declaration"),
        (
            "contain_user_settings: true",
            "follows text on lines of its own, so its contain_user_settings declaration",
        ),
        (
            "contain_account_integrations: true",
            "follows text on lines of its own, so its contain_account_integrations declaration",
        ),
    ],
    ids=["mcp", "light-workload", "user-settings", "account-integrations"],
)
def test_first_line_prose_rule_refuses_later_misplaced_settings(declaration, diagnostic):
    text = f"Review ---\n---\n{declaration}\n---"

    with pytest.raises(
        ConfigurationError, match=rf"--- block on line 2 {diagnostic} would be ignored"
    ):
        _parse_profile("reviewer", text)


def test_first_line_prose_rule_leaves_fenced_tools_after_first_unfenced_close_in_prompt(
    monkeypatch,
):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    text = (
        "Review ---\n---\n~~~yaml\n---\n"
        "tools: [Example]\nmcp: none\nworkload: light\n"
        "contain_user_settings: true\ncontain_account_integrations: true\n"
        "---\n~~~"
    )

    profile = _parse_profile("reviewer", text)

    assert profile.raw_body == text
    assert profile.extra == {}
    assert profile.mcp is None
    assert profile.workload == AgentProfile(name="reviewer").workload
    assert profile.contain_user_settings is False
    assert profile.contain_account_integrations is None
    assert warnings == []


@pytest.mark.parametrize(
    ("after_merge", "line"),
    [
        ("", 5),
        ("  tools: [Local]\n", 7),
        ("  tools: explanatory prose\n", None),
        ("  tools: explanatory prose\n- tools: [Other]\n", 8),
    ],
    ids=["merged", "local-list", "local-scalar", "independent-mapping"],
)
def test_unclosed_fence_reports_the_effective_tool_list_line(after_merge, line, monkeypatch):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    text = (
        "Review.\n~~~yaml\n---\n"
        "- example: &defaults\n    tools: [Template]\n  <<: *defaults\n"
        f"{after_merge}---"
    )

    profile = _parse_profile("reviewer", text)

    assert profile.raw_body == text
    assert profile.extra == {}
    if line is None:
        assert warnings == []
    else:
        [warning] = warnings
        assert f"tool settings on line {line} are inside an unclosed code fence" in warning


@pytest.mark.parametrize(
    ("prefix", "first_content_line"),
    [
        ("Review.\n---\n", 3),
        ("\n---\nallowed_tools: [Read]\n---\nReview.\n---\n", 7),
        ("\nReview ---\n", 3),
    ],
    ids=["prompt-only", "after-leading-settings", "prose-opener"],
)
@pytest.mark.parametrize(
    ("declaration", "key", "offset"),
    [
        ("- - allowed_tools: [Example]", "allowed_tools", 0),
        ("- - - disallowed_tools: [Example]", "disallowed_tools", 0),
        ("- &restrictions\n  - tools: [Example]\n- *restrictions", "tools", 1),
    ],
    ids=["two-levels", "three-levels", "shared-sequence"],
)
def test_misplaced_tool_settings_in_nested_sequences_are_refused(
    prefix, first_content_line, declaration, key, offset
):
    text = f"{prefix}{declaration}\n---\nEnd."
    with pytest.raises(
        ConfigurationError, match=rf"{key} declaration on line {first_content_line + offset}\b"
    ):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    ("prefix", "first_body_line"),
    [
        ("\f\u2028Review.\n", 4),
        ("---\nmodel: example\n---\n\f\u2028\nReview.\n", 8),
        ("\f\u2028---\r\nmodel: example\r\n---\r\nReview.\r\n", 7),
    ],
    ids=["stripped-prefix", "after-frontmatter", "crlf-frontmatter"],
)
@pytest.mark.parametrize("fenced", [True, False], ids=["warning", "nested-refusal"])
def test_tool_diagnostics_count_all_profile_line_breaks(
    monkeypatch, prefix, first_body_line, fenced
):
    if fenced:
        warnings = []
        monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
        _parse_profile("reviewer", f"{prefix}```yaml\n---\nallowed_tools: [Example]\n---")
        assert len(warnings) == 1
        assert f"tool settings on line {first_body_line + 2} " in warnings[0]
    else:
        with pytest.raises(
            ConfigurationError,
            match=rf"--- block on line {first_body_line}.*tools declaration on line {first_body_line + 1}\b",
        ):
            _parse_profile("reviewer", f"{prefix}---\n- - tools: [Example]\n---")


def test_tool_example_as_a_scalar_after_a_prose_opener_loads():
    text = "Review ---\ndescription: |\n  allowed_tools: [Example]\n---\nEnd."
    profile = _parse_profile("reviewer", text)
    assert profile.extra == {}
    assert profile.raw_body == text


@pytest.mark.parametrize(
    "prompt",
    (
        "Prompt.\n\n---\ndescription: |\n  allowed_tools: [Example]\n---\nEnd.",
        "Prompt.\n\n```yaml\n---\nallowed_tools: [Example]\n---\n```\nEnd.",
    ),
    ids=("scalar-value", "fenced-example"),
)
def test_readable_tool_mentions_after_prompt_text_stay_in_the_prompt(prompt):
    profile = _parse_profile("reviewer", prompt)
    assert profile.raw_body == prompt
    assert profile.extra == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-02-28", date(2026, 2, 28)), ('"2026-02-30"', "2026-02-30")],
    ids=["valid-date", "quoted-date"],
)
def test_profile_preserves_valid_date_values(value, expected):
    profile = _parse_profile("p", f"---\nDeadline: {value}\n---\nBody.\n")
    assert profile.raw_body == "Body."
    assert profile.extra["Deadline"] == expected


@pytest.mark.parametrize(
    ("ending", "message"),
    [("---\nBody.\n", "invalid YAML"), ("", "no closing ---")],
    ids=["closed", "unclosed"],
)
def test_profile_refuses_mcp_mentioned_with_an_unreadable_date(ending, message):
    text = f"---\nDeadline: 2026-02-30\n# mcp\n{ending}"
    with pytest.raises(ConfigurationError, match=message):
        _parse_profile("p", text)


@pytest.mark.parametrize(
    "declaration",
    [
        '? "m\\\n  cp"\n: none\n',
        '{? "m\\\n  cp": none}\n',
        '[? "m\\\n  cp": none]\n',
    ],
)
@pytest.mark.parametrize("ending", ["---\nbody\n", ""])
def test_unreadable_frontmatter_with_multiline_escaped_key_is_refused(declaration, ending):
    text = f"---\n\tbad: x\n{declaration}{ending}"
    with pytest.raises(ConfigurationError) as caught:
        _parse_profile("reviewer", text)
    if ending:
        assert "invalid YAML" in str(caught.value)
    else:
        assert "no closing ---" in str(caught.value)
    assert "backslash" in str(caught.value)


@pytest.mark.parametrize("content", ["ordinary \\\\ path", "mixed-case McP text"])
def test_unreadable_frontmatter_with_any_backslash_or_mcp_text_is_refused(content):
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", f"---\ninvalid: @value\ndescription: '{content}'\n---\nbody")


def test_unreadable_frontmatter_without_mcp_or_backslash_warns_and_keeps_body(monkeypatch):
    warnings = []
    monkeypatch.setattr("lionhub.orchestration.console.warn", warnings.append)
    profile = _parse_profile("reviewer", "---\ninvalid: @value\n---\nbody")
    assert profile.mcp is None
    assert "body" in profile.raw_body
    assert any("frontmatter" in warning and "ignored" in warning for warning in warnings)


def test_readable_frontmatter_allows_mcp_and_backslash_in_description():
    profile = _parse_profile("reviewer", "---\ndescription: 'MCP docs at \\local'\n---\nbody")
    assert profile.mcp is None
    assert "body" in profile.raw_body


def test_unrelated_duplicate_key_keeps_existing_yaml_behavior():
    profile = _parse_profile("reviewer", "---\nmodel: first\nmodel: second\n---\nReview.")
    assert profile.model == "second"
    assert profile.mcp is None


def test_single_merged_mcp_key_is_valid():
    profile = _parse_profile("reviewer", "---\ndefaults: &d {mcp: none}\n<<: *d\n---\nReview.")
    assert profile.mcp == "none"


def test_mcp_example_in_body_is_not_frontmatter():
    profile = _parse_profile("reviewer", "Review.\n\nExample:\n```yaml\nmcp: none\n```")
    assert profile.mcp is None


def test_mcp_mention_in_comment_refuses_unreadable_frontmatter():
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", "---\n# mcp: none\nmodel: [\n---\nReview.")


@pytest.mark.parametrize(
    "frontmatter",
    [
        "- mcp: none\n",
        "- mcp: invalid\n",
        "[{mcp: none}]\n",
        "- nested:\n    mcp: none\n",
    ],
)
def test_non_mapping_frontmatter_with_mcp_key_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize("frontmatter", ["- model: example\n", "'mcp: none'\n"])
def test_non_mapping_without_mcp_key_keeps_existing_behavior(frontmatter):
    profile = _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")
    assert profile.mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        '"m\\u0063p": none\nmodel: [unterminated\n',
        'model: [unterminated\n"m\\u0063p": none\n',
        '[{"m\\u0063p": none, broken: [}]\n',
    ],
)
def test_escaped_mcp_key_in_unreadable_frontmatter_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


def test_escaped_mcp_key_in_unclosed_frontmatter_is_refused():
    with pytest.raises(ConfigurationError):
        _parse_profile("reviewer", '---\n"m\\u0063p": none\n')


@pytest.mark.parametrize(
    "frontmatter",
    [
        "key: &policy_key mcp\n? *policy_key\n: none\nmodel: [\n",
        "model: [unterminated\nkey: &policy_key mcp\n? *policy_key\n: none\n",
    ],
)
def test_alias_mcp_key_in_unreadable_frontmatter_is_refused(frontmatter):
    with pytest.raises(ConfigurationError):
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")


def test_escaped_mcp_mention_in_comment_refuses_unreadable_frontmatter():
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", '---\n# "m\\u0063p": none\nmodel: [\n---\nReview.')


def test_escaped_mcp_example_in_unreadable_block_scalar_is_refused():
    text = '---\nprompt: |\n  "m\\u0063p": none\nmodel: [\n---\nReview.'
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)


def test_mcp_example_in_unreadable_block_scalar_is_refused():
    text = "---\nprompt: |\n  mcp: none\nmodel: [\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)


def test_mcp_example_in_readable_block_scalar_remains_lenient():
    text = "---\nprompt: |\n  mcp: none\n---\nReview."
    assert _parse_profile("reviewer", text).mcp is None


@pytest.mark.parametrize("ending", ["---\nReview.", ""])
def test_misindented_mcp_after_block_scalar_in_unreadable_frontmatter_is_refused(ending):
    text = f"---\nbad: @x\ndesc: |\n    content\n  mcp: none\n{ending}"
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", text)
    assert "mcp" in str(raised.value)
    if ending:
        assert "could not be parsed" in str(raised.value)
    else:
        assert "no closing ---" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid, mcp: none\n",
        'model: @invalid, "m\\u0063p": none\n',
    ],
)
def test_mcp_key_after_same_line_syntax_error_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid, other: value\n",
    ],
)
def test_unrelated_unreadable_same_line_text_remains_lenient(frontmatter):
    profile = _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")
    assert profile.mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid # comment, mcp: none\n",
        'model: @invalid, note: "example, mcp: none"\n',
    ],
)
def test_unreadable_same_line_mcp_in_comment_or_value_is_refused(frontmatter):
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")


@pytest.mark.parametrize("content", ["mcp: none", "example {mcp: none}"])
def test_mcp_key_shaped_scalar_text_in_unreadable_frontmatter_is_refused(content):
    text = f"---\nmodel: @invalid\nprompt: |\n  {content}\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid {mcp: none}\n",
        "model: @invalid [mcp: none]\n",
        'model: @invalid {"m\\u0063p": none}\n',
        'model: @invalid ["m\\u0063p": none]\n',
    ],
)
def test_mcp_key_after_same_line_flow_opener_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        'model: @invalid, note: "example {mcp: none}"\n',
        'model: @invalid, note: "example [mcp: none]"\n',
        "model: @invalid, note: 'example {mcp: none}'\n",
        'model: @invalid {other: "mcp: none"}\n',
        "model: @invalid # example {mcp: none}\n",
    ],
)
def test_unreadable_flow_mcp_text_in_quotes_or_comments_is_refused(frontmatter):
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")


def test_unreadable_flow_without_mcp_or_backslash_stays_lenient():
    text = "---\nmodel: @invalid {other: value}\n---\nReview."
    assert _parse_profile("reviewer", text).mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid, note: can't, mcp: none\n",
        'model: @invalid, note: can"t, mcp: none\n',
        "model: @invalid, note: can't, {mcp: none}\n",
        'model: @invalid, note: can"t, ["m\\u0063p": none]\n',
    ],
)
def test_plain_scalar_quote_does_not_hide_later_mcp_key(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        'model: @invalid, note: "can\'t, mcp: none"\n',
        "model: @invalid, note: 'can''t, mcp: none'\n",
        "model: @invalid, note: 'can\"t, {mcp: none}'\n",
        'model: @invalid, note: !!str "example, mcp: none"\n',
        'model: @invalid, note: &note "example, mcp: none"\n',
        'model: @invalid, {"note":"example, mcp: none"}\n',
    ],
)
def test_real_quoted_values_with_mcp_text_in_unreadable_block_are_refused(frontmatter):
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")


@pytest.mark.parametrize("key", ["mcp: none", '"m\\u0063p": none'])
def test_long_unreadable_flow_uses_no_yaml_scan(key, monkeypatch):
    import yaml

    real_scan = yaml.scan
    scanned_chars = 0

    def counted_scan(source, *args, **kwargs):
        nonlocal scanned_chars
        scanned_chars += len(source)
        return real_scan(source, *args, **kwargs)

    monkeypatch.setattr(yaml, "scan", counted_scan)
    text = "---\nmodel: @invalid," + "a," * 4_000 + key + "\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)
    assert scanned_chars == 0


def test_long_unreadable_flow_without_signals_uses_no_recovery_scans(monkeypatch):
    import yaml

    scans = 0
    real_scan = yaml.scan

    def counted_scan(source, *args, **kwargs):
        nonlocal scans
        scans += 1
        return real_scan(source, *args, **kwargs)

    monkeypatch.setattr(yaml, "scan", counted_scan)
    text = "---\nmodel: @invalid," + "a," * 4_000 + "other: value\n---\nReview."
    assert _parse_profile("reviewer", text).mcp is None
    assert scans == 0


def test_long_unreadable_flow_with_mcp_only_in_quoted_value_is_refused():
    text = "---\nmodel: @invalid," + "a," * 4_000 + 'note: "mcp: none"\n---\nReview.'
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid, [, note: cant? mcp\n",
        "model: @invalid, { !tagkey: &policy mcp, *policy: none, \n",
        "model: @invalid, [*policy: none, : none, key: &policy mcp\n",
    ],
)
def test_explicit_or_aliased_keys_after_malformed_flow_are_refused(frontmatter):
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")


def test_explicit_key_and_anchor_text_inside_unreadable_block_is_refused():
    text = '---\nmodel: @invalid, note: "example ? mcp, &policy mcp"\n---\nReview.'
    with pytest.raises(ConfigurationError, match="invalid YAML"):
        _parse_profile("reviewer", text)


def test_unclosed_valid_yaml_does_not_rescan_each_line(monkeypatch):
    import yaml

    real_scan = yaml.scan
    scans = 0

    def counted_scan(*args, **kwargs):
        nonlocal scans
        scans += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(yaml, "scan", counted_scan)
    body = "note: 'mcp: none'\n" + "description: ordinary\n" * 200
    assert _parse_profile("reviewer", "---\n" + body).mcp is None
    assert scans <= 1


def test_deep_unclosed_unrelated_frontmatter_keeps_leniency(recursion_limit):
    text = "---\nx: " + "[" * 500 + "a" + "]" * 500
    assert _parse_profile("reviewer", text).mcp is None


@pytest.mark.parametrize("position", ["before", "after"])
def test_deep_unclosed_mcp_frontmatter_is_refused(position, recursion_limit):
    nested = "x: " + "[" * 500 + "a" + "]" * 500 + "\n"
    block = "mcp: none\n" + nested if position == "before" else nested + "mcp: none\n"
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", "---\n" + block)


@pytest.mark.parametrize("header", ["prompt: |", "prompt: >", "prompt: |-", "key:\n  sub: |"])
@pytest.mark.parametrize(
    "tail",
    ["{lead}mcp: none\nmodel: [unterminated", "{indent}text\n{lead}mcp: none", "{lead}mcp: none"],
)
def test_tab_led_mcp_after_block_scalar_is_refused(header, tail):
    indent = "    " if header.startswith("key:") else "  "
    lead = "\t  " if header.startswith("key:") else "\t"
    text = f"---\n{header}\n{tail.format(indent=indent, lead=lead)}\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize("header", ["prompt: |", "prompt: >", "prompt: |-", "key:\n  sub: |"])
def test_space_indented_mcp_in_unreadable_block_scalar_is_refused(header):
    indent = "    " if header.startswith("key:") else "  "
    text = f"---\n{header}\n{indent}mcp: none\nmodel: [unterminated\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)


@pytest.mark.parametrize(
    "header,content",
    [
        ("prompt: |", " \tmcp: none"),
        ("key:\n  sub: |", "    \tmcp: none"),
    ],
)
def test_tabs_after_scalar_indentation_in_unreadable_block_are_refused(header, content):
    text = f"---\n{header}\n{content}\nmodel: [unterminated\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)


def test_tab_at_nested_scalar_indentation_is_refused():
    text = "---\nkey:\n  sub: |\n  \tmcp: none\nmodel: [unterminated\n---\nReview."
    with pytest.raises(ConfigurationError, match="mcp"):
        _parse_profile("reviewer", text)
