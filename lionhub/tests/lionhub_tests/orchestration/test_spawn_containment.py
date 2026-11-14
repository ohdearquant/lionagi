# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Profile frontmatter containment contracts."

from __future__ import annotations

import pytest

from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


@pytest.mark.parametrize(
    ("line", "expected", "warned"),
    [
        ("", False, False),
        ("contain_user_settings: false\n", False, False),
        ("contain_user_settings: true\n", True, False),
        ("contain_user_settings: maybe\n", True, True),
        ("contain_user_settings: 0\n", True, True),
        ("contain_user_settings:\n", True, True),
        ("contain_user_settings: null\n", True, True),
        ("contain_user_settings: True\n", True, False),
        ("contain_user_settings: FALSE\n", False, False),
        ("contain_user_settings: off\n", True, True),
        ("contain_user_settings: no\n", True, True),
        ("contain_user_settings: yes\n", True, True),
        ('contain_user_settings: "false"\n', True, True),
    ],
)
def test_the_profile_flag_reads_true_or_false_and_contains_otherwise(
    caplog, line, expected, warned
):
    import logging

    from lionhub.orchestration.spawn import _parse_profile, profile_config

    text = f"---\nmodel: claude_code/sonnet\n{line}---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.contain_user_settings is expected
    assert profile_config(profile).get("contain_user_settings") is (True if expected else None)
    assert "contain_user_settings" not in profile.extra
    assert ("is not true or false; containing" in caplog.text) is warned


@pytest.mark.parametrize(
    ("frontmatter", "contained"),
    [
        ("model: claude_code/sonnet\nnotes: [unclosed\n", False),
        ("- model: claude_code/sonnet\n- contain_user_settings: true\n", True),
    ],
)
def test_a_profile_whose_frontmatter_does_not_read_warns_that_its_settings_are_ignored(
    caplog, frontmatter, contained
):
    """A frontmatter block that does not read as YAML settings gives the profile none, so
    loading it says so rather than run the role on defaults in silence."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = f"---\n{frontmatter}---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is contained
    assert "does not read as YAML settings" in caplog.text
    assert "every setting in it is ignored" in caplog.text
    assert ("its worker is contained" in caplog.text) is contained


@pytest.mark.parametrize(
    "lines",
    [
        ["contain_user_settings: true"],
        ["contain_user_settings : true"],
        ["contain_user_settings: false"],
        ["contain_user_settings: maybe"],
        ["contain_user_settings: [true"],
        ["  contain_user_settings: false"],
        ["? contain_user_settings", ": false"],
        ['"contain_user_settings": false'],
        ["'contain_user_settings': false"],
        ["contain_user_settings: false", "contain_user_settings: false"],
        ["# contain_user_settings: false"],
    ],
)
def test_a_frontmatter_that_does_not_read_contains_when_it_names_the_flag(caplog, lines):
    """A block that does not read has no value of the flag to trust, so naming it anywhere in
    the block, in any form, contains the worker: a broken line cannot turn containment off."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    flag = "".join(f"{line}\n" for line in lines)
    text = f"---\nmodel: claude_code/sonnet\n{flag}notes: [unclosed\n---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is True
    assert "contain_user_settings" not in profile.extra
    assert "every setting in it is ignored, and its worker is contained" in caplog.text


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "prefix",
    ["\ufeff", "\ufeff\ufeff", "\n\ufeff", " \ufeff", "\ufeff\n", "\u200b", "\u2060\t"],
)
def test_a_profile_that_starts_with_a_byte_order_mark_keeps_its_settings(caplog, newline, prefix):
    """Whitespace and invisible format characters ahead of the opening delimiter, in any
    order, do not hide the frontmatter."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = prefix + newline.join(
        ["---", "model: claude_code/sonnet", "contain_user_settings: true", "---", "", "Work."]
    )
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model == "claude_code/sonnet"
    assert profile.contain_user_settings is True
    assert not {"\ufeff", "\u200b", "\u2060"} & set(profile.raw_body)
    assert "every setting in it is ignored" not in caplog.text


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["contain_user_settings: true", "contain_user_settings: false"], True),
        (["contain_user_settings: false", "contain_user_settings: true"], True),
        (["contain_user_settings: maybe", "contain_user_settings: false"], True),
        (['"contain_user_settings": true', "contain_user_settings: false"], True),
        (["contain_user_settings: true", "? contain_user_settings", ": false"], True),
        (["contain_user_settings: false", "contain_user_settings: null"], True),
        (["contain_user_settings: false", "contain_user_settings: 0"], True),
        (["contain_user_settings: false", "contain_user_settings: off"], True),
        (["contain_user_settings: false", "contain_user_settings: false"], False),
    ],
)
def test_no_key_of_a_frontmatter_that_reads_turns_containment_off_alone(caplog, lines, expected):
    """YAML keeps the last of a repeated key, so the flag is read from every key naming it,
    in any spelling of the key: it is off only when every one reads false."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    flag = "".join(f"{line}\n" for line in lines)
    text = f"---\nmodel: claude_code/sonnet\n{flag}---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model == "claude_code/sonnet"
    assert profile.contain_user_settings is expected
    assert "contain_user_settings is set 2 times" in caplog.text


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("  model: claude_code/sonnet\n  contain_user_settings: true\n", True),
        ("base: &base\n  contain_user_settings: true\n<<: *base\n", True),
        ("base: &base\n  contain_user_settings: off\n<<: *base\n", True),
        ("<<: {contain_user_settings: false}\n", False),
        ("<<: [{contain_user_settings: false}, {contain_user_settings: off}]\n", True),
        ("<<: {contain_user_settings: true}\ncontain_user_settings: false\n", False),
    ],
)
def test_the_flag_is_read_from_every_form_of_a_block_that_reads(caplog, block, expected):
    """An indented block and a merge key give the flag as YAML does, an explicit key winning
    over a merged one; a merged ``off`` reads as neither true nor false, and contains."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", f"---\n{block}---\n\nWork.\n")

    assert profile.contain_user_settings is expected
    assert "every setting in it is ignored" not in caplog.text


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "prefix",
    ["\ufe0f", "\u034f", "\u3164", "\u2800", "\u115f", "\x00", "x", "Notes "],
)
@pytest.mark.parametrize(
    "flag",
    [
        "contain_user_settings: true",
        "contain_user_settings: false",
        "  contain_user_settings: true",
    ],
)
def test_text_ahead_of_a_frontmatter_that_names_the_flag_contains(caplog, newline, prefix, flag):
    """A character ahead of the opening delimiter, on its line, that is neither whitespace nor
    a format character hides the block, and with it every setting; a block that names the flag
    after such text contains, whatever value it gives."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = prefix + newline.join(["---", "model: claude_code/sonnet", flag, "---", "", "Work."])
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is True
    assert "hides the frontmatter after it" in caplog.text
    assert "its worker is contained" in caplog.text


def test_text_ahead_of_a_frontmatter_on_its_line_warns_that_its_settings_are_ignored(caplog):
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = "\u3164---\nmodel: claude_code/sonnet\nyolo: true\n---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.yolo is False
    assert profile.contain_user_settings is False
    assert "text ahead of the --- on line 1 hides the frontmatter after it" in caplog.text
    assert "every setting in it is ignored" in caplog.text
    assert "its worker is contained" not in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "Work.\n\n```yaml\ncontain_user_settings: true\n```\n",
        "Work.\ncontain_user_settings: true\n",
        "# notes\n---\nmodel: claude_code/sonnet\n---\n\nWork.\n",
        "Work.\n\n---\n\nMore work.\n",
        "Scope\n---\nDo not touch contain_user_settings.\n",
        "Work.\n\n---\n\nNever set contain_user_settings here.\n\n---\n\nMore work.\n",
        "Notes ---\nWork on the task.\n",
        "Notes ---\nWork on the task.\n---\n\nMore work.\n",
        "Notes ---\nWork: do this: then that\n",
        "Title\n-----\nmodel: claude_code/sonnet\n---\n\nWork.\n",
        "---\n# contain_user_settings: true\n---\n\nWork.\n",
        "Work.\n\n---\n\n- Never set contain_user_settings here.\n\n---\n\nMore work.\n",
    ],
)
def test_a_profile_that_does_not_open_with_a_block_is_all_prompt(caplog, text):
    """Text on lines of its own ahead of any delimiter makes the profile one with no block, so
    nothing in it is a setting, as before; prose or a list after it is prompt too, even where
    it names the flag, as is a hidden block of prose, closed or not, that names no flag."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is False
    assert "every setting in it is ignored" not in caplog.text
    assert "follows text on lines of its own" not in caplog.text


@pytest.mark.parametrize(
    ("text", "contained", "warns", "refused"),
    [
        (
            "\u3164---\nmodel: claude_code/sonnet\n---\n\ncontain_user_settings: true\n",
            False,
            True,
            False,
        ),
        (
            "# notes\n---\nmodel: claude_code/sonnet\n---\n\ncontain_user_settings: true\n",
            False,
            False,
            False,
        ),
        (
            "# notes\n---\nmodel: claude_code/sonnet\ncontain_user_settings: false\n"
            "notes: [unclosed\n---\n\nWork.\n",
            False,
            False,
            True,
        ),
        (
            "\u3164---\nmodel: claude_code/sonnet\ncontain_user_settings: true\n\nWork.\n",
            True,
            True,
            False,
        ),
        (
            "\u3164---\nmodel: claude_code/sonnet\n# contain_user_settings: true\n---\n",
            False,
            True,
            False,
        ),
    ],
)
def test_a_hidden_block_ends_at_its_closing_delimiter(caplog, text, contained, warns, refused):
    """A hidden block runs to its closing ``---`` line, so a flag in the body after it is
    prompt text; one that does not read runs to the end when it has no closing line."""
    import logging

    from lionagi._errors import ConfigurationError
    from lionhub.orchestration.spawn import _parse_profile

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        if refused:
            with pytest.raises(ConfigurationError, match="contain_user_settings declaration"):
                _parse_profile("role", text)
            return
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is contained
    assert ("hides the frontmatter after it" in caplog.text) is warns
    assert ("its worker is contained" in caplog.text) is contained


@pytest.mark.parametrize(
    "text",
    [
        "Notes ---\n- contain_user_settings: true\n---\n\nWork.\n",
        "Notes ---\ncontain_user_settings\n---\n\nWork.\n",
        "Notes ---\nDo not touch contain_user_settings.\n",
        "ㅤ---\n- model: claude_code/sonnet\n- contain_user_settings: false\n---\n\nWork.\n",
    ],
)
def test_a_hidden_block_that_reads_as_a_list_or_text_contains_when_it_names_the_flag(caplog, text):
    """A hidden block that reads as a list or a line of text holds no settings, so it is read
    as a frontmatter that does not read as settings is: naming the flag anywhere in it
    contains the worker, with a warning, as the same block opening the profile does."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is True
    assert "hides the frontmatter after it" in caplog.text
    assert "its worker is contained" in caplog.text


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("# notes\n---\nmodel: claude_code/sonnet\ncontain_user_settings: true\n---\n\nWork.\n", 2),
        (
            "# notes\n---\nmodel: claude_code/sonnet\ncontain_user_settings: false\n---\n\nWork.\n",
            2,
        ),
        (
            "PROSE work.\n\n---\n\nmodel: claude_code/sonnet\ncontain_user_settings: false\n"
            "\n---\n\nMore.\n",
            3,
        ),
        (
            "# notes\r\n---\r\nmodel: claude_code/sonnet\r\ncontain_user_settings: true\r\n"
            "---\r\n\r\nWork.\r\n",
            2,
        ),
        (
            "# notes\n---\nmodel: claude_code/sonnet\ncontain_user_settings: true\n"
            "notes: [unclosed\n---\n\nWork.\n",
            2,
        ),
        ("# notes\n---\nnotes: [unclosed, contain_user_settings\n", 2),
        ("Title\n-----\nmodel: claude_code/sonnet\ncontain_user_settings: true\n---\n\nWork.\n", 2),
        ("Work.\n\n---\n\n- contain_user_settings: true\n\n---\n\nMore work.\n", 3),
        ("Work.\n---\ncontain_user_settings: true\n", 2),
        ("Work.\r\n---\r\ncontain_user_settings: true\r\n", 2),
        ("Work.\n---\ncontain_user_settings:true\n---\nMore.\n", 2),
        ("Work.\n---\n{contain_user_settings: true}\n---\nMore.\n", 2),
        (
            "Work.\n---\nmodel: claude_code/sonnet\n---\nNotes.\n---\n"
            "contain_user_settings: true\n---\nMore.\n",
            6,
        ),
    ],
)
def test_a_misplaced_block_that_names_the_flag_is_refused(text, line):
    """Text on lines of its own ahead of the first ``---`` line leaves the profile all prompt,
    as before, so a block after it sets nothing, the flag included, whatever value it gives.
    Any block after that text, closed or not and whichever it is, that sets the flag as a key
    or does not read and names it is refused; a block runs from a
    line of three or more dashes to the next such line."""
    from lionagi._errors import ConfigurationError
    from lionhub.orchestration.spawn import _parse_profile

    with pytest.raises(
        ConfigurationError, match=f"block on line {line}.*move the block to the top"
    ):
        _parse_profile("role", text)


@pytest.mark.parametrize(
    ("block", "key"),
    [
        ("worker:\n  contain_user_settings: true\n", "worker.contain_user_settings"),
        ("workers:\n  - contain_user_settings: true\n", "workers[0].contain_user_settings"),
        ("Contain_User_Settings: true\n", "Contain_User_Settings"),
        ("contain-user-settings: true\n", "contain-user-settings"),
    ],
)
def test_a_key_that_reads_as_the_flag_but_is_not_it_warns_without_containing(caplog, block, key):
    """The flag is read only as a top-level key spelled exactly so: a key spelled like it, or
    the flag under another setting, warns that it does not set the flag, and the rest of the
    frontmatter still applies."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = "---\nmodel: claude_code/sonnet\n" + block + "---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model == "claude_code/sonnet"
    assert profile.contain_user_settings is False
    assert f"it is not set by {key!r}, and its worker is not contained" in caplog.text


@pytest.mark.parametrize(
    ("block", "contained"),
    [
        ("contain_user_settings: true\nworker:\n  contain_user_settings: false\n", True),
        ("contain_user_settings: false\n", False),
        ("notes: do not touch contain_user_settings\n", False),
    ],
)
def test_a_frontmatter_that_sets_the_flag_or_names_it_in_a_value_has_no_key_warning(
    caplog, block, contained
):
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = "---\nmodel: claude_code/sonnet\n" + block + "---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.contain_user_settings is contained
    assert "counts only as a top-level key" not in caplog.text


def test_a_false_in_the_body_of_an_unclosed_frontmatter_does_not_turn_containment_off(caplog):
    """With no closing delimiter, where the block ends is not known, so it runs to the end and
    a line in the body cannot undo the flag the block set."""
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    text = (
        "---\nmodel: claude_code/sonnet\ncontain_user_settings: true\n\n"
        "Work.\ncontain_user_settings: false\n"
    )
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.contain_user_settings is True
    assert "every setting in it is ignored, and its worker is contained" in caplog.text


@pytest.mark.parametrize(
    "text",
    [
        "Work.\n",
        "---\n---\n\nWork.\n",
        "---\n# a note only\n---\n\nWork.\n",
        "---\n# a note\n\n# and another\n---\n\nWork.\n",
        "---\nmodel: claude_code/sonnet\n---\n\nWork.\n",
        "\u3164---\n---\n\nWork.\n",
        "\u3164---\n# a note only\n---\n\nWork.\n",
    ],
)
def test_a_profile_with_no_settings_to_lose_does_not_warn(caplog, text):
    import logging

    from lionhub.orchestration.spawn import _parse_profile

    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        _parse_profile("role", text)

    assert "every setting in it is ignored" not in caplog.text
