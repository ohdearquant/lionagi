# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A denied tool permission is a local configuration defect and gets its own type.

Before this, a turn that produced nothing because a headless CLI could not be
granted a tool permission arrived as a bare ``ProviderError``, which is the same
thing an unclassified provider failure arrives as. The two demand opposite
responses: one is fixed here in one line, the other tells a caller nothing. A
consumer branching on type could not tell them apart.

No LLM and no network: these call the classifier and read the record.
"""

from __future__ import annotations

import json

import pytest

from lionagi.mcp._terminal_cause import allowed_cause_classes, read_terminal_cause
from lionagi.providers._provider_errors import (
    ProviderAdapterError,
    ProviderError,
    ProviderPermissionError,
    classify_provider_error,
)

# Verbatim from lionagi/providers/google/gemini_code.py, where it is raised.
AUTO_DENY = (
    "agy reported status=SUCCESS with no response content. "
    "A tool call was most likely auto-denied, because headless "
    "print mode cannot prompt for a tool permission. Re-run with "
    "yolo to auto-approve tools, or add an allow-rule under "
    "permissions.allow in the agy settings."
)


class TestTheDeniedPermissionGetsItsOwnType:
    def test_the_auto_deny_message_classifies_as_a_permission_error(self):
        assert type(classify_provider_error(AUTO_DENY)) is ProviderPermissionError

    def test_it_is_not_retryable(self):
        """An unmodified retry reproduces this exactly and forever. Marking it
        retryable would be an instruction to keep paying for the same failure."""
        assert ProviderPermissionError.retryable is False

    def test_an_adapter_status_with_no_named_cause_still_lands_on_the_adapter_class(self):
        """The must-NOT-match arm. A pattern loose enough to take this would be
        relabelling every adapter failure as a permission problem."""
        got = classify_provider_error("agy returned status=ERROR")
        assert type(got) is ProviderAdapterError

    @pytest.mark.parametrize(
        "text",
        [
            "the user denied the request",
            "permissions are insufficient for this resource",
            "access to the file was denied by the operating system",
        ],
    )
    def test_unrelated_denial_prose_is_not_taken(self, text):
        """`denied` and `permission` occur throughout ordinary provider text.
        Matching either word alone would make this class the catch-all it exists
        to be distinguished from."""
        assert type(classify_provider_error(text)) is ProviderError


class TestTheBranchOrderIsWhatMakesItReachable:
    def test_a_message_matching_both_patterns_yields_the_specific_class(self):
        """The arm the pattern tests cannot reach. A permission failure is
        reported BY an adapter, so one string can satisfy both catalogues, and
        then only the ORDER of the branches decides. Every pattern-level
        assertion above still passes with the catch-all moved in front of the
        specific check, so without this arm the ordering is untested and the
        change is inert at the exact site it exists for.
        """
        both = "agy returned status=SUCCESS — a tool call was auto-denied"

        from lionagi.providers import _provider_errors as mod

        assert any(p.search(both) for p in mod._PERMISSION_PATTERNS)
        assert any(p.search(both) for p in mod._ADAPTER_PATTERNS)

        assert type(classify_provider_error(both)) is ProviderPermissionError


class TestNothingThatCaughtItBeforeStopsCatching:
    def test_an_existing_handler_catching_providererror_still_catches_this(self):
        """Inheritance, asserted rather than assumed, because it is the property
        this change could break for every caller at once."""
        with pytest.raises(ProviderError):
            raise classify_provider_error(AUTO_DENY)

    def test_the_record_admits_the_new_class_name(self):
        assert "ProviderPermissionError" in allowed_cause_classes()


class TestTheRecordsWrittenBeforeThisChangeStillRead:
    def test_a_historical_record_saying_providererror_is_still_accepted(self, tmp_path):
        """Every auto-deny already on disk says ProviderError. A reader that only
        admitted the new name would report nothing across the entire historical
        population, silently, and any aggregate spanning the change would be
        computed from one side of it.
        """
        target = tmp_path / "cause.json"
        target.write_text(
            json.dumps({"class": "ProviderError", "retryable": False, "status_source": "absent"})
        )

        got = read_terminal_cause(target)

        assert got is not None
        assert got["class"] == "ProviderError"

    def test_a_record_written_after_this_change_reads_back_as_the_new_class(self, tmp_path):
        target = tmp_path / "cause.json"
        target.write_text(
            json.dumps(
                {
                    "class": "ProviderPermissionError",
                    "retryable": False,
                    "status_source": "absent",
                }
            )
        )

        got = read_terminal_cause(target)

        assert got is not None
        assert got["class"] == "ProviderPermissionError"


class TestTheHierarchyStaysWhereTheRecordCanSeeIt:
    def test_every_provider_error_subclass_lives_in_the_module_the_record_imports(self):
        """``allowed_cause_classes`` walks ``__subclasses__()``, which only sees
        classes already imported into the running interpreter. That is safe here
        for one reason and one only: every subclass is defined in the single
        module the record imports, so importing ``ProviderError`` defines all of
        them. A subclass added in some other module would be admissible in the
        writer's process and unknown in the reader's, depending on import order,
        and nothing would say so.

        Asserting the invariant rather than replacing the derivation: an explicit
        list is what the derivation was written to avoid, since a class added
        without a second edit there would silently read back as 'unknown'.
        """
        import lionagi.providers._provider_errors as owner

        def walk(cls: type) -> set[type]:
            found = {cls}
            for sub in cls.__subclasses__():
                found |= walk(sub)
            return found

        strays = {
            f"{c.__module__}.{c.__name__}"
            for c in walk(ProviderError) - {ProviderError}
            if c.__module__ != owner.__name__
        }
        assert not strays, f"ProviderError subclasses outside {owner.__name__}: {sorted(strays)}"
