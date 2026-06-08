"""Tests for lionagi/ln/types/spec.py"""

import dataclasses

import pytest

from lionagi.ln.types import CommonMeta, Meta, Spec


class TestCommonMeta:
    def test_allowed_returns_all_values(self):
        allowed = CommonMeta.allowed()
        assert "name" in allowed
        assert "nullable" in allowed
        assert "listable" in allowed
        assert "validator" in allowed
        assert "default" in allowed
        assert "default_factory" in allowed
        assert len(allowed) == 6

    def test_validate_rejects_both_default_and_factory(self):
        with pytest.raises(ValueError, match="both 'default' and 'default_factory'"):
            CommonMeta._validate_common_metas(default="value", default_factory=lambda: "value")

    def test_validate_rejects_non_callable_factory(self):
        with pytest.raises(ValueError, match="must be callable"):
            CommonMeta._validate_common_metas(default_factory="not_a_function")

    def test_validate_rejects_non_callable_validator(self):
        with pytest.raises(ValueError, match="must be a list of functions"):
            CommonMeta._validate_common_metas(validator="not_callable")

    def test_prepare_detects_duplicate_in_metadata(self):
        meta1 = Meta("name", "field1")
        meta2 = Meta("name", "field2")
        with pytest.raises(ValueError, match="Duplicate metadata key: name"):
            CommonMeta.prepare(metadata=(meta1, meta2))

    def test_prepare_detects_duplicate_in_args(self):
        meta1 = Meta("name", "field1")
        meta2 = Meta("name", "field2")
        with pytest.raises(ValueError, match="Duplicate metadata key: name"):
            CommonMeta.prepare(meta1, meta2)

    def test_prepare_detects_duplicate_in_kwargs(self):
        meta1 = Meta("name", "field1")
        with pytest.raises(ValueError, match="Duplicate metadata key: name"):
            CommonMeta.prepare(meta1, name="field2")

    def test_prepare_success(self):
        result = CommonMeta.prepare(name="field", nullable=True)
        assert len(result) == 2
        meta_dict = {m.key: m.value for m in result}
        assert meta_dict["name"] == "field"
        assert meta_dict["nullable"] is True


class TestSpec:
    def test_basic_creation(self):
        spec = Spec(str, name="username")
        assert spec.base_type == str
        assert spec.name == "username"

    def test_nullable_and_listable(self):
        spec = Spec(int, name="age", nullable=True, listable=False)
        assert spec.is_nullable is True
        assert spec.is_listable is False

    def test_default_value(self):
        spec = Spec(str, name="field", default="default_value")
        assert spec.default == "default_value"
        assert spec.create_default_value() == "default_value"

    def test_default_factory(self):
        spec = Spec(list, name="field", default_factory=list)
        assert spec.has_default_factory is True
        result = spec.create_default_value()
        assert isinstance(result, list)

    def test_async_default_factory_warning(self):

        async def async_factory():
            return "value"

        with pytest.warns(UserWarning, match="Async default factories"):
            spec = Spec(str, name="field", default_factory=async_factory)
        assert spec.has_async_default_factory is True

    def test_as_nullable(self):
        spec = Spec(str, name="field")
        nullable_spec = spec.as_nullable()
        assert nullable_spec.is_nullable is True
        assert nullable_spec.name == "field"

    def test_as_listable(self):
        spec = Spec(int, name="field")
        listable_spec = spec.as_listable()
        assert listable_spec.is_listable is True
        assert listable_spec.name == "field"

    def test_with_default(self):
        spec = Spec(str, name="field")
        spec_with_default = spec.with_default("value")
        assert spec_with_default.default == "value"

    def test_with_default_factory(self):
        spec = Spec(list, name="field")
        spec_with_factory = spec.with_default(list)
        assert spec_with_factory.has_default_factory is True

    def test_with_validator(self):

        def validator(v):
            return len(v) > 0

        spec = Spec(str, name="field")
        spec_with_validator = spec.with_validator(validator)
        assert spec_with_validator.get("validator") == validator

    def test_annotation_basic(self):
        spec = Spec(str, name="field")
        assert spec.annotation == str

    def test_annotation_nullable(self):
        spec = Spec(str, name="field", nullable=True)
        assert spec.annotation == str | None

    def test_annotation_listable(self):
        spec = Spec(int, name="field", listable=True)
        assert spec.annotation == list[int]

    def test_annotation_nullable_listable(self):
        spec = Spec(int, name="field", nullable=True, listable=True)
        assert spec.annotation == list[int] | None

    def test_getitem(self):
        spec = Spec(str, name="field", custom="value")
        assert spec["name"] == "field"
        assert spec["custom"] == "value"

    def test_getitem_missing_raises(self):
        spec = Spec(str, name="field")
        with pytest.raises(KeyError, match="Metadata key 'missing'"):
            _ = spec["missing"]

    def test_get_with_default(self):
        spec = Spec(str, name="field")
        assert spec.get("missing", "default") == "default"
        assert spec.get("name") == "field"

    def test_metadict(self):
        spec = Spec(str, name="field", nullable=True, custom="value")
        metadict = spec.metadict()
        assert metadict["name"] == "field"
        assert metadict["nullable"] is True
        assert metadict["custom"] == "value"

    def test_metadict_exclude(self):
        spec = Spec(str, name="field", nullable=True, custom="value")
        metadict = spec.metadict(exclude={"name"})
        assert "name" not in metadict
        assert metadict["nullable"] is True

    def test_metadict_exclude_common(self):
        spec = Spec(str, name="field", nullable=True, custom="value")
        metadict = spec.metadict(exclude_common=True)
        assert "name" not in metadict
        assert "nullable" not in metadict
        assert metadict["custom"] == "value"

    def test_invalid_base_type_raises(self):
        with pytest.raises(ValueError, match="must be a type"):
            Spec("not_a_type")

    def test_create_default_without_default_raises(self):
        spec = Spec(str, name="field")
        with pytest.raises(ValueError, match="No default value"):
            spec.create_default_value()

    def test_create_default_with_async_factory_raises(self):

        async def async_factory():
            return "value"

        with pytest.warns(UserWarning):
            spec = Spec(str, name="field", default_factory=async_factory)

        with pytest.raises(ValueError, match="asynchronous"):
            spec.create_default_value()

    def test_annotated_caching(self):
        spec = Spec(str, name="field")
        annotated1 = spec.annotated()
        annotated2 = spec.annotated()
        # Should return same object from cache
        assert annotated1 is annotated2

    def test_with_updates(self):
        spec = Spec(str, name="field", nullable=False)
        updated = spec.with_updates(nullable=True, custom="value")
        assert updated.is_nullable is True
        assert updated.get("custom") == "value"
        assert updated.name == "field"

    def test_immutability(self):
        spec = Spec(str, name="field")
        with pytest.raises(dataclasses.FrozenInstanceError):  # frozen dataclass (not pydantic)
            spec.base_type = int


class TestCommonMetaFalsyValueHandling:
    def test_default_zero_and_factory_conflict_raises(self):
        """default=0 (falsy) alongside default_factory must still be detected as a conflict.

        Previously, truthiness on kw.get('default') treated 0 as absent, silently
        accepting an invalid combination that would produce undefined behaviour at
        instantiation time.
        """
        with pytest.raises(ValueError, match="both 'default' and 'default_factory'"):
            CommonMeta._validate_common_metas(default=0, default_factory=list)

    def test_default_false_and_factory_conflict_raises(self):
        with pytest.raises(ValueError, match="both 'default' and 'default_factory'"):
            CommonMeta._validate_common_metas(default=False, default_factory=list)

    def test_default_factory_zero_non_callable_raises(self):
        """default_factory=0 is not callable and must be rejected.

        Previously the walrus assignment ``if _df := kw.get('default_factory')``
        skipped the callable check for falsy non-callables, silently producing a
        broken Spec that would fail at runtime when the factory was invoked.
        """
        with pytest.raises(ValueError, match="must be callable"):
            CommonMeta._validate_common_metas(default_factory=0)

    def test_validator_zero_non_callable_raises(self):
        """validator=0 is not callable and must be rejected.

        Previously the walrus assignment ``if _val := kw.get('validator')`` skipped
        validator-callability checks when the value was falsy, allowing invalid
        validators to be stored without error.
        """
        with pytest.raises(ValueError, match="must be a list of functions or a function"):
            CommonMeta._validate_common_metas(validator=0)

    def test_default_zero_alone_is_valid(self):
        CommonMeta._validate_common_metas(default=0)  # must not raise

    def test_default_false_alone_is_valid(self):
        CommonMeta._validate_common_metas(default=False)  # must not raise

    def test_spec_default_zero_is_stored(self):
        spec = Spec(int, name="count", default=0)
        assert spec.default == 0
        assert spec.create_default_value() == 0

    def test_spec_default_false_is_stored(self):
        spec = Spec(bool, name="flag", default=False)
        assert spec.default is False
        assert spec.create_default_value() is False


class TestSpecDefaultValueEdgeCases:
    def test_spec_create_default_value_errors_without_default(self):
        spec = Spec(str, name="title")
        with pytest.raises(ValueError, match="No default value"):
            spec.create_default_value()

    async def test_spec_async_default_factory_requires_async_creation(self):
        """Sync create_default_value raises for async factory; acreate_default_value returns value."""
        import warnings

        async def factory():
            return "x"

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            spec = Spec(str, name="title", default_factory=factory)

        with pytest.raises(ValueError, match="asynchronous"):
            spec.create_default_value()

        result = await spec.acreate_default_value()
        assert result == "x"
