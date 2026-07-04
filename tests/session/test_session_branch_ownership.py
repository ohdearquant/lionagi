"""Session/Branch ownership semantics: include claims, remove tears down."""

import pytest

from lionagi.session.branch import Branch
from lionagi.session.session import Session


class TestCrossSessionInclude:
    def test_second_session_include_raises(self):
        session_a = Session()
        session_b = Session()
        shared = Branch(name="shared")
        session_a.include_branches(shared)

        with pytest.raises(ValueError, match="already owned by session"):
            session_b.include_branches(shared)

    def test_first_sessions_wiring_survives_rejected_steal(self):
        session_a = Session()
        session_b = Session()
        _ = session_a.hooks
        shared = Branch(name="shared")
        session_a.include_branches(shared)
        observer_a = shared._observer
        hooks_a = shared._hooks

        with pytest.raises(ValueError):
            session_b.include_branches(shared)

        assert shared._observer is observer_a
        assert shared._hooks is hooks_a
        assert shared._operation_manager is session_a._operation_manager
        assert shared._owning_session_id == session_a.id
        assert shared not in session_b.branches

    def test_session_constructor_rejects_owned_branch(self):
        from lionagi.protocols.types import Pile

        session_a = Session()
        shared = Branch(name="shared")
        session_a.include_branches(shared)

        pile = Pile(collections=[shared], item_type={Branch}, strict_type=False)
        with pytest.raises(ValueError, match="already owned by session"):
            Session(branches=pile)

    def test_same_session_reinclude_is_idempotent(self):
        session = Session()
        branch = Branch(name="b")
        session.include_branches(branch)
        observer = branch._observer
        manager = branch._operation_manager

        session.include_branches(branch)

        assert branch._observer is observer
        assert branch._operation_manager is manager
        assert branch._owning_session_id == session.id
        assert len(session.branches) == 2  # default branch + b


class TestRemoveBranchTeardown:
    def test_remove_clears_routing_keeps_data(self):
        session = Session()
        _ = session.hooks
        branch = Branch(name="b")
        session.include_branches(branch)
        store = branch.memory

        session.remove_branch(branch)

        assert branch._owning_session_id is None
        assert branch._observer is None
        assert branch._hooks is None
        assert branch.user is None
        # fresh standalone manager, not the session's
        assert branch._operation_manager is not None
        assert branch._operation_manager is not session._operation_manager
        # data survives removal
        assert branch._memory is store

    def test_remove_then_include_reparents_cleanly(self):
        session_a = Session()
        session_b = Session()
        _ = session_a.hooks
        _ = session_b.hooks
        branch = Branch(name="b")
        session_a.include_branches(branch)

        session_a.remove_branch(branch)
        session_b.include_branches(branch)

        assert branch._owning_session_id == session_b.id
        assert branch._observer is session_b.observer
        assert branch._hooks is session_b._hooks
        assert branch._operation_manager is session_b._operation_manager
        assert branch.user == session_b.id
        assert branch not in session_a.branches

    def test_remove_preserves_foreign_user(self):
        session = Session()
        branch = Branch(name="b")
        session.include_branches(branch)
        branch.user = "someone-else"

        session.remove_branch(branch)

        assert branch.user == "someone-else"


class TestSplitUnaffected:
    def test_split_clone_is_owned_by_same_session(self):
        session = Session()
        branch = Branch(name="b")
        session.include_branches(branch)

        clone = session.split(branch)

        assert clone._owning_session_id == session.id
        assert clone in session.branches
