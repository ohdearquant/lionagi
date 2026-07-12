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

    def test_rejected_batch_mutates_no_branch(self):
        """A rejected member anywhere in the batch leaves EVERY member
        untouched: no partial claim of earlier batch members."""
        session_a = Session()
        session_b = Session()
        owned = Branch(name="owned")
        session_a.include_branches(owned)
        fresh1 = Branch(name="fresh1")
        fresh2 = Branch(name="fresh2")

        with pytest.raises(ValueError, match="already owned by session"):
            session_b.include_branches([fresh1, owned, fresh2])

        for b in (fresh1, fresh2):
            assert b._owning_session_id is None
            assert b.user is None
            assert b not in session_b.branches
        assert owned._owning_session_id == session_a.id

    def test_session_constructor_rejects_owned_branch(self):
        from lionagi.protocols.types import Pile

        session_a = Session()
        shared = Branch(name="shared")
        session_a.include_branches(shared)

        fresh = Branch(name="fresh")
        pile = Pile(collections=[fresh, shared], item_type={Branch}, strict_type=False)
        with pytest.raises(ValueError, match="already owned by session"):
            Session(branches=pile)

        # the fresh sibling stays claimable after the failed construction
        assert fresh._owning_session_id is None
        Session().include_branches(fresh)

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


class TestSessionHookAttachment:
    """Hook-bus attachment must not depend on whether `include_branches()` or
    the first `Session.hooks` access happens first."""

    async def test_hook_delivery_is_construction_order_invariant(self):
        from lionagi.hooks import HookPoint

        async def deliver(bus, members):
            received = []

            async def capture(*, member, **_):
                received.append(member)

            bus.on(HookPoint.TOOL_POST, capture)
            for label, branch in members:
                assert branch._hooks is bus
                await branch._hooks.emit(HookPoint.TOOL_POST, member=label)
            return received

        hooks_then_include = Session()
        bus_a = hooks_then_include.hooks
        extra_a = Branch(name="extra")
        hooks_then_include.include_branches(extra_a)

        include_then_hooks = Session()
        extra_b = Branch(name="extra")
        include_then_hooks.include_branches(extra_b)
        bus_b = include_then_hooks.hooks

        delivered_a = await deliver(
            bus_a,
            [
                ("default", hooks_then_include.default_branch),
                ("extra", extra_a),
            ],
        )
        delivered_b = await deliver(
            bus_b,
            [
                ("default", include_then_hooks.default_branch),
                ("extra", extra_b),
            ],
        )

        assert delivered_a == ["default", "extra"]
        assert delivered_b == delivered_a

    def test_include_then_hooks_attaches_default_and_explicit_branch(self):
        """Branches present before the first `session.hooks` access (the
        constructor's default branch, and one explicitly included early)
        must both receive the bus once it is created."""
        session = Session()
        explicit = Branch(name="explicit")
        session.include_branches(explicit)

        bus = session.hooks

        assert session.default_branch._hooks is bus
        assert explicit._hooks is bus

    def test_hooks_then_include_still_attaches_new_branch(self):
        """The reverse order keeps working: a branch included after the bus
        already exists gets the same bus (unchanged existing behavior)."""
        session = Session()
        bus = session.hooks
        branch = Branch(name="late")

        session.include_branches(branch)

        assert branch._hooks is bus
        assert session.default_branch._hooks is bus

    async def test_remove_detaches_branch_from_functional_hook_delivery(self):
        """After removal, the branch's own hook-emission guard must no
        longer deliver — proving detachment beyond the `_hooks is None`
        identity check already covered by TestRemoveBranchTeardown."""
        from lionagi.hooks import HookPoint

        session = Session()
        bus = session.hooks
        branch = Branch(name="b")
        session.include_branches(branch)
        assert branch._hooks is bus

        received = []

        async def capture(**kw):
            received.append(kw["branch_id"])

        bus.on(HookPoint.MESSAGE_ADD, capture)

        await branch._persist_via_bus({"role": "user"})
        assert received == [str(branch.id)]

        session.remove_branch(branch)
        assert branch._hooks is None

        await branch._persist_via_bus({"role": "user"})
        assert received == [str(branch.id)]  # unchanged: removed branch is a no-op


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
