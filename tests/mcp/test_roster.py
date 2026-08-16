# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The roster verbs: which agent profiles exist here, and what one of them runs.

A caller submits a run by naming a profile, so the value of these verbs is
entirely in whether their answer is the answer the run would get: the same
file wins, the reported configuration is the one the loader produced, and a
name that does not exist comes back naming the ones that do rather than as
an empty result.

Every root here is a temp directory -- nothing in this file may read the
real ``~/.lionagi/agents/``, which is why HOME and the working directory are
redirected before any resolution happens.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lionagi.cli._providers import load_agent_profile, profile_config
from lionagi.mcp import dispatch


def call(**kwargs):
    return asyncio.run(dispatch.request(**kwargs))


def op(name: str, args: dict | None = None) -> dict:
    """Run one roster op and return its entry, which may be a failure."""
    return call(ops=[{"op": name, "args": args or {}}])["ops"][0]


def write_profile(agents_dir: Path, name: str, body: str) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{name}.md"
    path.write_text(body)
    return path


@pytest.fixture
def roots(monkeypatch, tmp_path: Path):
    """A global root and a project root, both under a temp directory.

    HOME is redirected and the working directory moved into the project, because
    the resolver reads both live and would otherwise walk up into whatever real
    ``.lionagi/`` sits above the checkout.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    global_agents = home / ".lionagi" / "agents"
    project_agents = project / ".lionagi" / "agents"
    global_agents.mkdir(parents=True)
    project_agents.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(project)
    # A temp directory is not a git repository, so the git-root probe finds
    # nothing and the walk starts at the working directory.
    return type(
        "Roots",
        (),
        {"home": home, "project": project, "glob": global_agents, "proj": project_agents},
    )()


# ── the list ─────────────────────────────────────────────────────────────────


def test_the_list_covers_both_roots_and_says_which_one_each_came_from(roots):
    write_profile(roots.glob, "archivist", "---\nmodel: openai/gpt-4.1-mini\n---\nbody\n")
    write_profile(roots.proj, "builder", "---\nmodel: anthropic/claude\n---\nbody\n")

    result = op("profile.list")
    assert result["ok"] is True, result
    by_name = {p["name"]: p for p in result["result"]["profiles"]}

    assert sorted(by_name) == ["archivist", "builder"]
    assert by_name["archivist"]["source"]["scope"] == "global"
    assert by_name["builder"]["source"]["scope"] == "project"
    assert by_name["archivist"]["source"]["path"] == str(roots.glob / "archivist.md")
    assert result["result"]["count"] == 2


def test_the_list_names_the_roots_it_searched(roots):
    # Without them, an empty list and a missing directory are the same answer.
    scopes = [r["scope"] for r in op("profile.list")["result"]["roots"]]
    assert scopes == ["project", "global"]


def test_the_list_reports_what_the_loader_produced_not_what_the_file_says(roots):
    # The file declares a timeout the loader rejects; reporting the declared
    # value would tell a caller a run will do something it will not do.
    write_profile(roots.proj, "builder", "---\nmodel: m\ntimeout: -5\n---\nbody\n")
    entry = op("profile.list")["result"]["profiles"][0]
    assert entry["resolved"]["timeout"] is None
    assert entry["resolved"]["timeout"] == load_agent_profile("builder").timeout


def test_the_list_withholds_the_prompt_body(roots):
    write_profile(roots.proj, "builder", "---\nmodel: m\n---\nsecret instructions\n")
    entry = op("profile.list")["result"]["profiles"][0]
    assert "secret instructions" not in str(entry)


# ── precedence ───────────────────────────────────────────────────────────────


def test_a_name_in_both_roots_resolves_to_the_project_one(roots):
    write_profile(roots.glob, "reviewer", "---\nmodel: global-model\neffort: low\n---\nglobal\n")
    write_profile(roots.proj, "reviewer", "---\nmodel: project-model\n---\nproject\n")

    result = op("profile.show", {"name": "reviewer"})
    assert result["ok"] is True, result
    shown = result["result"]

    assert shown["source"]["path"] == str(roots.proj / "reviewer.md")
    assert shown["source"]["scope"] == "project"
    assert shown["resolved"]["model"] == "project-model"
    # Whole-file precedence, not a field merge: the global file's effort is
    # displaced, not inherited.
    assert shown["resolved"]["effort"] is None
    assert shown["shadowed"] == [{"path": str(roots.glob / "reviewer.md"), "scope": "global"}]


def test_the_two_layouts_in_one_root_shadow_each_other_too(roots):
    """A root holds two layouts, so it can hold two declarations of one name.

    The loser here is displaced exactly as surely as a file in a root further
    down, and it is easier to miss: reporting only the winning path per root
    hides it, and the caller reads an empty shadowed list as "nothing else
    declares this".
    """
    directory_layout = roots.proj / "reviewer" / "reviewer.md"
    directory_layout.parent.mkdir(parents=True)
    directory_layout.write_text("---\nmodel: directory-model\n---\ndirectory\n")
    flat_layout = write_profile(roots.proj, "reviewer", "---\nmodel: flat-model\n---\nflat\n")

    shown = op("profile.show", {"name": "reviewer"})["result"]

    assert shown["source"]["path"] == str(directory_layout)
    assert shown["resolved"]["model"] == "directory-model"
    assert shown["shadowed"] == [{"path": str(flat_layout), "scope": "project"}]


def test_a_same_root_loser_is_listed_before_a_further_root(roots):
    # Shadowed is in resolution order, so the file that came closest to winning
    # comes first -- which is the one to delete if the wrong profile ran.
    directory_layout = roots.proj / "reviewer" / "reviewer.md"
    directory_layout.parent.mkdir(parents=True)
    directory_layout.write_text("---\nmodel: directory-model\n---\ndirectory\n")
    flat_layout = write_profile(roots.proj, "reviewer", "---\nmodel: flat-model\n---\nflat\n")
    global_layout = write_profile(roots.glob, "reviewer", "---\nmodel: g\n---\nglobal\n")

    shown = op("profile.show", {"name": "reviewer"})["result"]

    assert [entry["path"] for entry in shown["shadowed"]] == [
        str(flat_layout),
        str(global_layout),
    ]


def test_the_other_separator_spelling_is_a_second_profile_not_a_displaced_one(roots):
    """One directory, both spellings, and each name still selects its own file.

    ``-`` and ``_`` are interchangeable only when the spelling asked for is
    absent, so a directory holding both holds two profiles a caller can run
    separately. Listing either under the other's ``shadowed`` would tell a
    reader a profile cannot be selected when a request for it selects it.
    """
    hyphen = write_profile(roots.proj, "postmortem-lead", "---\nmodel: hyphen-model\n---\nh\n")
    underscore = write_profile(roots.proj, "postmortem_lead", "---\nmodel: under-model\n---\nu\n")

    shown = op("profile.show", {"name": "postmortem-lead"})["result"]
    assert shown["source"]["path"] == str(hyphen)
    assert shown["resolved"]["model"] == "hyphen-model"
    assert shown["shadowed"] == []

    shown = op("profile.show", {"name": "postmortem_lead"})["result"]
    assert shown["source"]["path"] == str(underscore)
    assert shown["resolved"]["model"] == "under-model"
    assert shown["shadowed"] == []


def test_a_displaced_layout_is_still_shadowed_beside_the_other_spelling(roots):
    """Both claims the shadowed list has to keep making, in one directory.

    ``postmortem-lead`` is declared twice, in both layouts, and only the
    directory one is ever read — naming the flat file is the whole point of the
    list. ``postmortem_lead`` is a third file that runs under its own name, so
    it belongs nowhere in that list. Reporting neither would drop the
    diagnostic rather than correct it.
    """
    directory_layout = roots.proj / "postmortem-lead" / "postmortem-lead.md"
    directory_layout.parent.mkdir(parents=True)
    directory_layout.write_text("---\nmodel: directory-model\n---\ndirectory\n")
    flat_layout = write_profile(roots.proj, "postmortem-lead", "---\nmodel: flat-model\n---\nf\n")
    underscore = write_profile(roots.proj, "postmortem_lead", "---\nmodel: under-model\n---\nu\n")

    shown = op("profile.show", {"name": "postmortem-lead"})["result"]

    assert shown["source"]["path"] == str(directory_layout)
    assert shown["shadowed"] == [{"path": str(flat_layout), "scope": "project"}]
    assert op("profile.show", {"name": "postmortem_lead"})["result"]["source"]["path"] == str(
        underscore
    )


def test_the_verb_agrees_with_the_loader_a_run_would_use(roots):
    """Compared on a profile that declares almost nothing, and on every field.

    Both halves are load-bearing. Two fields are not enough — a roster that
    parsed a couple for itself would pass and disagree with the run about the
    rest. And a profile with everything set is not enough either: a reader
    supplying its own default only diverges where the file is silent, so a fully
    populated fixture agrees with any defaulting logic at all.
    """
    write_profile(roots.glob, "reviewer", "---\nmodel: global-model\neffort: high\n---\ng\n")
    write_profile(roots.proj, "reviewer", "---\nmodel: project-model\n---\nproject\n")

    shown = op("profile.show", {"name": "reviewer"})["result"]["resolved"]
    assert shown == profile_config(load_agent_profile("reviewer"))
    assert shown["model"] == "project-model"
    assert [key for key, value in shown.items() if value is None]


def test_an_unshadowed_name_reports_nothing_shadowed(roots):
    write_profile(roots.glob, "archivist", "---\nmodel: m\n---\nbody\n")
    shown = op("profile.show", {"name": "archivist"})["result"]
    assert shown["shadowed"] == []
    assert shown["source"]["scope"] == "global"


# ── a name that is not there ─────────────────────────────────────────────────


def test_an_unknown_name_is_an_error_naming_the_alternatives(roots):
    write_profile(roots.glob, "archivist", "---\nmodel: m\n---\nbody\n")
    write_profile(roots.proj, "builder", "---\nmodel: m\n---\nbody\n")

    result = op("profile.show", {"name": "ghost"})
    assert result["ok"] is False
    assert result["error"]["kind"] == "not_found"
    message = result["error"]["message"]
    assert "ghost" in message
    assert "archivist" in message and "builder" in message


def test_a_name_the_validator_refuses_is_invalid_input_not_a_miss(roots):
    result = op("profile.show", {"name": "../escape"})
    assert result["ok"] is False
    assert result["error"]["kind"] == "invalid_input"


# ── the working directory the answer is about ────────────────────────────────


def test_resolution_follows_the_cwd_a_run_would_be_submitted_with(roots, tmp_path: Path):
    write_profile(roots.proj, "builder", "---\nmodel: here\n---\nbody\n")
    elsewhere = tmp_path / "elsewhere"
    write_profile(elsewhere / ".lionagi" / "agents", "builder", "---\nmodel: there\n---\nb\n")

    assert op("profile.show", {"name": "builder"})["result"]["resolved"]["model"] == "here"
    under = op("profile.show", {"name": "builder", "cwd": str(elsewhere)})["result"]
    assert under["resolved"]["model"] == "there"
    assert under["cwd"] == str(elsewhere)


def test_the_working_directory_is_restored_after_a_cwd_scoped_read(roots, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".lionagi" / "agents").mkdir(parents=True)
    before = Path.cwd()
    op("profile.list", {"cwd": str(elsewhere)})
    assert Path.cwd() == before


def test_a_cwd_that_is_not_a_directory_is_refused_by_name(roots, tmp_path: Path):
    result = op("profile.list", {"cwd": str(tmp_path / "nowhere")})
    assert result["ok"] is False
    assert result["error"]["kind"] == "invalid_input"
    assert "nowhere" in result["error"]["message"]


# ── the shape of the surface ─────────────────────────────────────────────────


def test_the_roster_verbs_are_in_the_catalog_with_their_signature():
    entries = {e["verb"]: e for e in call(help=True)["verbs"]}
    # available is omitted at its default; the key appears only to say False
    assert "available" not in entries["profile.list"]
    assert entries["profile.show"]["required"] == ["name"]


def test_a_roster_read_needs_no_schema_fingerprint():
    # Fingerprints gate the spawn verbs. A read that demanded one would cost a
    # round-trip for nothing, and none of the job reads demand one either.
    assert "schema_fingerprint" not in call(help="profile.show")


def test_an_unknown_parameter_is_refused_by_name(roots):
    result = op("profile.list", {"root": "/tmp"})
    assert result["ok"] is False
    assert "root" in result["error"]["message"]


# ── the spelling fallback, and where it has nothing to say ───────────────────


def test_a_file_reached_by_the_other_separator_says_so(roots):
    """Two files under one name, reached two different ways.

    The project file is spelled the way the name is spelled. The global one is
    reached only because the separators stand in for each other, and it is still
    selectable under its own spelling — so a caller deciding which file to edit
    is looking at a weaker claim there than in the project root. A reply that
    describes both entries identically hides which one is which.
    """
    exact = write_profile(roots.proj, "postmortem-lead", "---\nmodel: m\n---\nbody\n")
    other_spelling = write_profile(roots.glob, "postmortem_lead", "---\nmodel: g\n---\nbody\n")

    entry = op("profile.list", {"names": ["postmortem-lead"], "fields": ["source", "shadowed"]})[
        "result"
    ]["profiles"][0]

    assert entry["source"] == {"path": str(exact), "scope": "project", "match": "exact"}
    assert entry["shadowed"] == [
        {"path": str(other_spelling), "scope": "global", "match": "separator_fallback"}
    ]


def test_two_spellings_in_one_root_are_reported_as_ambiguous_not_as_shadowed(roots):
    """A root the resolver would refuse contributes no ranking to the answer.

    The project file matches the requested spelling and runs. The two global
    files spell it the other two ways, and a request that got as far as that
    root would be refused rather than answered — so neither of them is a file
    this name displaces, and both are still selectable under their own names.
    Listing them as shadowed says the opposite of both.
    """
    exact = write_profile(roots.proj, "post-mortem_lead", "---\nmodel: exact\n---\nbody\n")
    underscores = write_profile(roots.glob, "post_mortem_lead", "---\nmodel: under\n---\nbody\n")
    hyphens = write_profile(roots.glob, "post-mortem-lead", "---\nmodel: hyphen\n---\nbody\n")

    listed = op(
        "profile.list",
        {"names": ["post-mortem_lead"], "fields": ["source", "shadowed", "ambiguous"]},
    )["result"]["profiles"]

    assert listed[0]["source"] == {"path": str(exact), "scope": "project", "match": "exact"}
    assert listed[0]["shadowed"] == []
    assert sorted(entry["path"] for entry in listed[0]["ambiguous"]) == sorted(
        [str(underscores), str(hyphens)]
    )
    # Both really are selectable, which is why neither is displaced.
    assert (
        op("profile.show", {"name": "post_mortem_lead"})["result"]["resolved"]["model"] == "under"
    )
    assert (
        op("profile.show", {"name": "post-mortem-lead"})["result"]["resolved"]["model"] == "hyphen"
    )


def test_placement_names_no_source_where_the_loader_refuses_to_choose(roots):
    """The claim the placement walk must never make, checked against the loader.

    One root, both fallback spellings, and nothing spelled as asked: the loader
    raises rather than ranking them, so there is no file to call the source. The
    loader is called here in the same directory as the control — a placement
    answer is only wrong relative to what a run would actually get.
    """
    from lionagi.cli._providers import AmbiguousProfileNameError, load_agent_profile
    from lionagi.mcp.roster import _placement

    write_profile(roots.proj, "post_mortem_lead", "---\nmodel: under\n---\nbody\n")
    write_profile(roots.proj, "post-mortem-lead", "---\nmodel: hyphen\n---\nbody\n")

    with pytest.raises(AmbiguousProfileNameError):
        load_agent_profile("post-mortem_lead")

    placement = _placement("post-mortem_lead")
    assert placement["source"] is None
    assert placement["shadowed"] == []
    assert len(placement["ambiguous"]) == 2


# ── asking for less than the whole roster ────────────────────────────────────


def test_the_unprojected_list_is_what_it_has_always_been(roots):
    """The whole reply to a call that asked for nothing, against the shape on record.

    The projection is an addition, so a caller that never learned about it has
    to keep reading the same keys — including inside ``source`` and each
    ``shadowed`` entry, which is where a key added to the placement walk would
    otherwise arrive unannounced. Compared whole rather than key by key: a
    subset check passes on a reply carrying anything extra, which is exactly the
    change it is here to catch.
    """
    write_profile(roots.glob, "reviewer", "---\nmodel: global-model\n---\nglobal\n")
    write_profile(roots.proj, "reviewer", "---\nmodel: project-model\n---\nproject\n")

    result = op("profile.list")["result"]
    # Through a JSON round trip, because that is how a caller receives it.
    assert json.loads(json.dumps(result)) == result

    assert set(result) == {"cwd", "roots", "profiles", "count"}
    assert result["count"] == 1
    assert result["profiles"] == [
        {
            "name": "reviewer",
            "source": {"path": str(roots.proj / "reviewer.md"), "scope": "project"},
            "shadowed": [{"path": str(roots.glob / "reviewer.md"), "scope": "global"}],
            "resolved": profile_config(load_agent_profile("reviewer")),
        }
    ]


def test_showing_one_profile_is_what_it_has_always_been_too(roots):
    """``profile.show`` takes no projection at all, so it carries the same record.

    Placement detail reaches a caller of the list only by being named in
    ``fields``; there is nothing to name here, so there is nothing extra to
    return.
    """
    write_profile(roots.glob, "reviewer", "---\nmodel: global-model\n---\nglobal\n")
    write_profile(roots.proj, "reviewer", "---\nmodel: project-model\n---\nproject\n")

    shown = op("profile.show", {"name": "reviewer"})["result"]

    assert set(shown) == {"cwd", "name", "source", "shadowed", "resolved", "declared_extra_keys"}
    assert shown["source"] == {"path": str(roots.proj / "reviewer.md"), "scope": "project"}
    assert shown["shadowed"] == [{"path": str(roots.glob / "reviewer.md"), "scope": "global"}]


def test_names_answers_for_the_profiles_asked_for_and_no_others(roots):
    write_profile(roots.glob, "archivist", "---\nmodel: a\n---\nbody\n")
    write_profile(roots.proj, "builder", "---\nmodel: b\n---\nbody\n")
    write_profile(roots.proj, "critic", "---\nmodel: c\n---\nbody\n")

    result = op("profile.list", {"names": ["builder", "critic"]})["result"]

    assert [p["name"] for p in result["profiles"]] == ["builder", "critic"]
    assert result["count"] == 2
    # Narrowed, not reshaped: a named profile carries what it always carried.
    assert result["profiles"][0]["resolved"]["model"] == "b"


def test_a_name_nothing_declares_is_absent_rather_than_an_error(roots):
    """Which is the answer to 'is there a profile called this'.

    An error would make a caller checking three names unable to ask about all
    three at once, which is the round trip the parameter exists to save.
    """
    write_profile(roots.proj, "builder", "---\nmodel: b\n---\nbody\n")

    result = op("profile.list", {"names": ["builder", "ghost"]})
    assert result["ok"] is True, result
    assert [p["name"] for p in result["result"]["profiles"]] == ["builder"]


def test_fields_returns_only_what_was_asked_for_plus_the_name(roots):
    write_profile(roots.proj, "builder", "---\nmodel: anthropic/claude\n---\nbody\n")

    entries = op("profile.list", {"fields": ["resolved.model"]})["result"]["profiles"]

    assert entries == [{"name": "builder", "resolved": {"model": "anthropic/claude"}}]


def test_fields_can_ask_for_placement_without_the_configuration(roots):
    builder = write_profile(roots.proj, "builder", "---\nmodel: m\n---\nbody\n")

    entry = op("profile.list", {"fields": ["source"]})["result"]["profiles"][0]

    assert entry == {
        "name": "builder",
        "source": {"path": str(builder), "scope": "project", "match": "exact"},
    }


def test_fields_can_still_ask_for_the_whole_resolved_block(roots):
    write_profile(roots.proj, "builder", "---\nmodel: m\n---\nbody\n")

    entry = op("profile.list", {"fields": ["resolved"]})["result"]["profiles"][0]

    assert entry["resolved"] == profile_config(load_agent_profile("builder"))
    assert "source" not in entry


def test_an_unknown_field_is_refused_by_name(roots):
    """Rather than returned empty, which reads as a profile that declares nothing."""
    write_profile(roots.proj, "builder", "---\nmodel: m\n---\nbody\n")

    result = op("profile.list", {"fields": ["resolved.modle"]})

    assert result["ok"] is False
    assert result["error"]["kind"] == "invalid_input"
    assert "resolved.modle" in result["error"]["message"]
    assert "resolved.model" in result["error"]["message"]


def test_the_unprojected_placement_is_narrower_than_the_projected_one(roots):
    """The same key, two shapes, pinned side by side under one roster.

    Both calls describe the same two files. Omitting ``fields`` gives each of
    them as a path and a scope; naming ``source`` and ``shadowed`` adds ``match``
    to both, and ``ambiguous`` exists on the projected reply and nowhere else.
    Which shape a caller gets is decided by whether they asked, so the two are
    pinned together rather than apart — that is the part a caller has to be able
    to predict, and the part the schema text has to describe.
    """
    project = write_profile(roots.proj, "reviewer", "---\nmodel: project-model\n---\nproject\n")
    global_ = write_profile(roots.glob, "reviewer", "---\nmodel: global-model\n---\nglobal\n")

    plain = op("profile.list")["result"]["profiles"][0]
    asked = op("profile.list", {"fields": ["source", "shadowed", "ambiguous"]})["result"][
        "profiles"
    ][0]

    assert plain["source"] == {"path": str(project), "scope": "project"}
    assert plain["shadowed"] == [{"path": str(global_), "scope": "global"}]
    assert "ambiguous" not in plain

    assert asked["source"] == {"path": str(project), "scope": "project", "match": "exact"}
    assert asked["shadowed"] == [
        {"path": str(global_), "scope": "global", "match": "exact"},
    ]
    assert asked["ambiguous"] == []


def test_the_fields_description_says_what_omitting_it_returns(roots):
    """Read from the schema the server serves, against a reply it really produced.

    A caller sizing a call reads this text and nothing else, so it has to name
    the keys an unprojected reply carries and say that naming a placement field
    widens that reply rather than narrowing it. Asserting against the served
    schema rather than against a copy of the string keeps the two from drifting;
    asserting the key names against a real reply keeps the text from going stale
    when the reply gains a key.
    """
    write_profile(roots.proj, "builder", "---\nmodel: m\n---\nbody\n")

    described = call(help="profile.list")["schema"]["properties"]["fields"]["description"]
    entry = op("profile.list")["result"]["profiles"][0]

    for key in entry:
        assert f"'{key}'" in described, key
    # The two ways a projected placement field is wider than an unprojected one.
    assert "'match'" in described
    assert "'ambiguous'" in described
    # Omitting the parameter returns the narrower placement, so no reading of
    # this text may promise that omitting it returns more.
    assert "full record" not in described


def test_names_and_fields_narrow_the_same_reply_together(roots):
    write_profile(roots.glob, "archivist", "---\nmodel: a\n---\nbody\n")
    write_profile(roots.proj, "builder", "---\nmodel: b\n---\nbody\n")

    result = op("profile.list", {"names": ["builder"], "fields": ["resolved.model"]})["result"]

    assert result["profiles"] == [{"name": "builder", "resolved": {"model": "b"}}]
    # The roots are still named: they explain a name that came back missing.
    assert [r["scope"] for r in result["roots"]] == ["project", "global"]
