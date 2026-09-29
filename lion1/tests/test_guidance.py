"""The guidance a profile is given is a file in the hub, one per role, its fields filled by the
caller; a missing field stays visible in the prompt, a missing file names what exists."""

import pytest

from hub import guidance
from hub.guidance import names


def test_each_guidance_file_is_read_by_name_and_its_fields_filled():
    assert {"box", "chair", "chores", "code", "console", "desk", "reader", "sandbox"} <= set(names())
    text = guidance("console", model="m1", tools="read(path)")
    assert text.startswith("You are m1, talking with a person") and "Tools: read(path)." in text
    assert "OUT{}" in text and not text.endswith("\n")
    # results come only in the notice the loop writes, so a turn that issues commands claims none
    assert (
        "A turn that issues commands says what it is doing, never what their results are: results are "
        "read only in the notice the loop writes." in text
    )
    assert "$image" in guidance("box")  # an unfilled field stays visible
    chair = guidance("chair", actor="a:v1", owner="a", senders="a, lambda:owner", tools="read(path)")
    assert (
        chair.startswith("You are a:v1, the chair of a,")
        and "Mail from a, lambda:owner is your work" in chair
    )
    assert "$owner" in guidance("chores", chores="- disk")
    with pytest.raises(
        KeyError,
        match="no guidance 'nope'; there is box, chair, chores, code, console, desk, reader, sandbox",
    ):
        guidance("nope")
