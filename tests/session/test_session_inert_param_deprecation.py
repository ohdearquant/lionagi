"""Session parameters that were accepted and never read warn when passed."""

import warnings

import pytest

from lionagi.protocols.types import Instruction
from lionagi.session.branch import Branch
from lionagi.session.session import Session


def _session_with_one_message() -> tuple[Session, Branch]:
    session = Session()
    branch = Branch(name="worker")
    branch.messages.include(
        Instruction(content={"instruction": "hello"}, sender="user", recipient=branch.id)
    )
    session.include_branches(branch)
    return session, branch


def _deprecations(caught: list[warnings.WarningMessage]) -> list[warnings.WarningMessage]:
    return [
        w
        for w in caught
        if issubclass(w.category, DeprecationWarning) and str(w.message).startswith("Session.")
    ]


def test_remove_branch_delete_warns_at_the_caller_and_still_removes():
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session.remove_branch(branch.id, delete=True)

    found = _deprecations(caught)
    assert len(found) == 1
    assert "remove_branch(delete=...)" in str(found[0].message)
    assert found[0].filename == __file__
    assert branch not in session.branches
    assert branch._owning_session_id is None


@pytest.mark.parametrize("call", ["keyword_false", "positional_true"])
def test_remove_branch_warns_for_any_passed_delete(call):
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if call == "keyword_false":
            session.remove_branch(branch.id, delete=False)
        else:
            session.remove_branch(branch.id, True)

    found = _deprecations(caught)
    assert len(found) == 1
    assert found[0].filename == __file__
    assert branch not in session.branches


def test_remove_branch_without_delete_does_not_warn():
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session.remove_branch(branch.id)

    assert _deprecations(caught) == []
    assert branch not in session.branches


@pytest.mark.parametrize("name", ["exclude_clone", "exclude_load"])
@pytest.mark.parametrize("value", [True, False])
def test_concat_messages_inert_flags_warn_once_and_change_nothing(name, value):
    session, branch = _session_with_one_message()
    expected = [m.id for m in session.concat_messages([branch.id])]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        got = session.concat_messages([branch.id], **{name: value})

    found = _deprecations(caught)
    assert len(found) == 1
    assert f"concat_messages({name}=...)" in str(found[0].message)
    assert found[0].filename == __file__
    assert [m.id for m in got] == expected


@pytest.mark.parametrize("name", ["exclude_clone", "exclude_load"])
def test_to_df_inert_flags_warn_once_for_to_df_only(name):
    pytest.importorskip("pandas")
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        df = session.to_df([branch.id], **{name: True})

    found = _deprecations(caught)
    assert len(found) == 1
    assert f"to_df({name}=...)" in str(found[0].message)
    assert found[0].filename == __file__
    assert len(df) == 1


def test_to_df_with_both_flags_warns_once_per_flag():
    pytest.importorskip("pandas")
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session.to_df([branch.id], exclude_clone=False, exclude_load=True)

    messages = sorted(
        str(w.message).split("(", 1)[1].split("=", 1)[0] for w in _deprecations(caught)
    )
    assert messages == ["exclude_clone", "exclude_load"]


def test_concat_messages_without_flags_or_with_none_does_not_warn():
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session.concat_messages([branch.id])
        session.concat_messages([branch.id], exclude_clone=None, exclude_load=None)
        session.concat_messages([branch.id], None)
        session.remove_branch(branch.id, delete=None)

    assert _deprecations(caught) == []


def test_to_df_without_flags_or_with_none_does_not_warn():
    pytest.importorskip("pandas")
    session, branch = _session_with_one_message()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        session.to_df([branch.id])
        session.to_df([branch.id], exclude_clone=None, exclude_load=None)

    assert _deprecations(caught) == []
