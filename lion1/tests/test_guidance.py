"""The guidance a profile is given is a file in the hub, one per role, its fields filled by the
caller; a missing field stays visible in the prompt, a missing file names what exists."""

import pytest

from hub import guidance
from hub.guidance import names


def test_each_guidance_file_is_read_by_name_and_its_fields_filled():
    assert {"box", "code", "console", "sandbox"} <= set(names())
    text = guidance("console", model="m1", tools="read(path)")
    assert text.startswith("You are m1, talking with a person") and "Tools: read(path)." in text
    assert "OUT{}" in text and not text.endswith("\n")
    assert "$image" in guidance("box")  # an unfilled field stays visible
    with pytest.raises(KeyError, match="no guidance 'nope'; there is box, code, console, sandbox"):
        guidance("nope")
