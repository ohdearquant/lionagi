"""Regression tests for system_template deprecation warnings."""

import warnings

import pytest

from lionagi.session.branch import Branch


class TestSystemTemplateDeprecation:
    """Branch(system_template=...) must raise DeprecationWarning."""

    def test_system_template_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Branch(system_template="Hello {{ name }}")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "system_template" in str(deprecations[0].message)
        assert "deprecated" in str(deprecations[0].message).lower()

    def test_system_template_context_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Branch(system_template_context={"name": "world"})

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "system_template_context" in str(deprecations[0].message)
        assert "deprecated" in str(deprecations[0].message).lower()

    def test_both_template_params_warn_twice(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Branch(
                system_template="Hello {{ name }}",
                system_template_context={"name": "world"},
            )

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 2
        messages = {str(d.message) for d in deprecations}
        assert any("system_template " in m for m in messages)
        assert any("system_template_context" in m for m in messages)

    def test_no_warning_without_template_params(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            Branch(system="You are helpful.")

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 0

    def test_branch_still_functional_with_template_param(self):
        """Passing system_template warns but does not create an Instruction message."""
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            branch = Branch(system_template="ignored {{ template }}")

        # Branch is usable and the deprecated param caused no message to be added
        assert branch is not None
        assert len(branch.messages) == 0
