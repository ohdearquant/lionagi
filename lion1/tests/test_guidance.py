"""The guidance a profile is given is a file in the hub, one per role, its fields filled by the
caller; a missing field stays visible in the prompt, a missing file names what exists."""

import pytest

from hub import guidance
from hub.guidance import names


def test_each_guidance_file_is_read_by_name_and_its_fields_filled():
    assert {
        "box",
        "chair",
        "chores",
        "code",
        "code-noshell",
        "console",
        "desk",
        "job",
        "reader",
        "sandbox",
    } <= set(names())
    text = guidance("console", model="m1", tools="read(path)")
    assert text.startswith("You are m1, talking with a person") and "Tools: read(path)." in text
    assert "OUT{}" in text and not text.endswith("\n")
    # results come only in the notice the loop writes, so a turn that issues commands claims none
    assert (
        "A turn that issues commands says what it is doing, never what their results are: results are "
        "read only in the notice the loop writes." in text
    )
    assert "$image" in guidance("box")  # an unfilled field stays visible
    chair = guidance("chair", actor="a:v1", owner="a", senders="a, agent-c", tools="read(path)")
    assert chair.startswith("You are a:v1, the chair of a,") and "Mail from a, agent-c is your work" in chair
    assert "$owner" in guidance("chores", chores="- disk")
    job = guidance("job", model="m1", submitter="a:caller", tools="none")
    # a submitted job has nobody to answer it, and it ends with OUT{} where the console never writes one
    assert job.startswith("You are m1, doing one job on lion1 for a:caller") and "ask nothing" in job
    assert "The job ends when you write OUT{...}" in job and "what a:caller reads back" in job
    with pytest.raises(
        KeyError,
        match="no guidance 'nope'; there is box, chair, chores, code, code-noshell, console, desk, job, "
        "reader, sandbox",
    ):
        guidance("nope")
