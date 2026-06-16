"""End-to-end tests for PydanticSpecAdapter: Spec → FieldInfo → Model → Validation."""

import pytest
from pydantic import BaseModel, ValidationError

from lionagi.adapters.spec_adapters import PydanticSpecAdapter
from lionagi.ln.types import Operable, Spec


class TestProtocolConformance:
    def test_conforms_to_protocol(self):
        assert hasattr(PydanticSpecAdapter, "create_field")
        assert hasattr(PydanticSpecAdapter, "create_model")
        assert hasattr(PydanticSpecAdapter, "create_validator")
        assert hasattr(PydanticSpecAdapter, "parse_json")
        assert hasattr(PydanticSpecAdapter, "fuzzy_match_fields")
        assert hasattr(PydanticSpecAdapter, "validate_response")
        assert hasattr(PydanticSpecAdapter, "update_model")


class TestCreateField:
    def test_basic_field(self):
        spec = Spec(str, name="username")
        field_info = PydanticSpecAdapter.create_field(spec)

        assert field_info is not None
        assert field_info.annotation == str

    def test_field_with_default(self):
        spec = Spec(str, name="username", default="anonymous")
        field_info = PydanticSpecAdapter.create_field(spec)

        assert field_info.default == "anonymous"

    def test_field_with_default_factory(self):
        spec = Spec(list, name="tags", default_factory=list)
        field_info = PydanticSpecAdapter.create_field(spec)

        assert field_info.default_factory is not None
        assert callable(field_info.default_factory)

    def test_nullable_field(self):
        spec = Spec(str, name="bio", nullable=True)
        field_info = PydanticSpecAdapter.create_field(spec)

        # Nullable fields should have default=None
        assert field_info.default is None
        assert field_info.annotation == str | None

    def test_listable_field(self):
        spec = Spec(str, name="tags", listable=True)
        field_info = PydanticSpecAdapter.create_field(spec)

        assert field_info.annotation == list[str]


class TestCreateModel:
    def test_basic_model_creation(self):
        specs = [
            Spec(str, name="username"),
            Spec(int, name="age"),
        ]
        operable = Operable(specs, name="User")

        UserModel = PydanticSpecAdapter.create_model(operable, "UserModelBasic")

        assert issubclass(UserModel, BaseModel)
        assert "username" in UserModel.model_fields
        assert "age" in UserModel.model_fields

    def test_model_with_defaults(self):
        specs = [
            Spec(str, name="username", default="anonymous"),
            Spec(int, name="age", default=0),
        ]
        operable = Operable(specs)

        UserModel = PydanticSpecAdapter.create_model(operable, "UserModelDefaults")
        instance = UserModel()

        assert instance.username == "anonymous"
        assert instance.age == 0

    def test_model_with_nullable_fields(self):
        specs = [
            Spec(str, name="username"),
            Spec(str, name="bio", nullable=True),
        ]
        operable = Operable(specs)

        UserModel = PydanticSpecAdapter.create_model(operable, "UserModelNullable")
        instance = UserModel(username="alice")

        assert instance.username == "alice"
        assert instance.bio is None

    def test_model_validation(self):
        specs = [
            Spec(str, name="username"),
            Spec(int, name="age"),
        ]
        operable = Operable(specs)

        UserModel = PydanticSpecAdapter.create_model(operable, "UserModelValidation")

        # Valid data
        user = UserModel(username="alice", age=30)
        assert user.username == "alice"
        assert user.age == 30

        # Invalid data
        with pytest.raises(ValidationError):
            UserModel(username="alice", age="not_an_int")

    def test_model_with_include(self):
        specs = [
            Spec(str, name="username"),
            Spec(int, name="age"),
            Spec(str, name="email"),
        ]
        operable = Operable(specs)

        UserModel = PydanticSpecAdapter.create_model(
            operable, "UserModelInclude", include={"username", "age"}
        )

        assert "username" in UserModel.model_fields
        assert "age" in UserModel.model_fields
        assert "email" not in UserModel.model_fields

    def test_model_with_exclude(self):
        specs = [
            Spec(str, name="username"),
            Spec(int, name="age"),
            Spec(str, name="password"),
        ]
        operable = Operable(specs)

        UserModel = PydanticSpecAdapter.create_model(
            operable, "UserModelExclude", exclude={"password"}
        )

        assert "username" in UserModel.model_fields
        assert "age" in UserModel.model_fields
        assert "password" not in UserModel.model_fields


class TestEndToEnd:
    def test_spec_to_model_to_instance(self):
        # Step 1: Define specs
        specs = [
            Spec(str, name="name"),
            Spec(int, name="age"),
            Spec(str, name="email", nullable=True),
            Spec(list, name="tags", default_factory=list, listable=False),
        ]

        # Step 2: Create operable
        operable = Operable(specs, name="Person")

        # Step 3: Generate model
        PersonModel = PydanticSpecAdapter.create_model(operable, "PersonModel")

        # Step 4: Create instance
        person = PersonModel(name="Alice", age=30)

        # Step 5: Validate
        assert person.name == "Alice"
        assert person.age == 30
        assert person.email is None
        assert person.tags == []

    def test_operable_create_model_integration(self):
        specs = [
            Spec(str, name="username"),
            Spec(int, name="score", default=0),
        ]
        operable = Operable(specs, name="Player")

        # Use Operable's create_model method
        PlayerModel = operable.create_model(adapter="pydantic", model_name="PlayerModel")

        assert issubclass(PlayerModel, BaseModel)
        player = PlayerModel(username="player1")
        assert player.username == "player1"
        assert player.score == 0

    def test_complex_types(self):
        specs = [
            Spec(dict[str, int], name="scores"),
            Spec(list[str], name="tags"),
        ]
        operable = Operable(specs)

        DataModel = PydanticSpecAdapter.create_model(operable, "DataModel")
        instance = DataModel(scores={"a": 1, "b": 2}, tags=["tag1", "tag2"])

        assert instance.scores == {"a": 1, "b": 2}
        assert instance.tags == ["tag1", "tag2"]


class TestValidationMethods:
    def test_parse_json(self):
        json_str = '{"name": "Alice", "age": 30}'
        data = PydanticSpecAdapter.parse_json(json_str, fuzzy=False)

        assert isinstance(data, dict)
        assert data["name"] == "Alice"
        assert data["age"] == 30

    def test_parse_json_fuzzy(self):
        # JSON in markdown code block
        text = """Here is the data:
```json
{"name": "Bob", "age": 25}
```
and more text"""
        data = PydanticSpecAdapter.parse_json(text, fuzzy=True)

        assert isinstance(data, dict)
        assert data["name"] == "Bob"

    def test_fuzzy_match_fields(self):
        specs = [Spec(str, name="user_name"), Spec(int, name="user_age")]
        operable = Operable(specs)
        UserModel = PydanticSpecAdapter.create_model(operable, "UserModelFuzzy")

        # Data with slightly different keys
        data = {"username": "Alice", "age": 30}
        matched = PydanticSpecAdapter.fuzzy_match_fields(data, UserModel, strict=False)

        # Should fuzzy match username → user_name, age → user_age
        assert "user_name" in matched or "username" in matched

    def test_update_model(self):
        specs = [Spec(str, name="name"), Spec(int, name="age")]
        operable = Operable(specs)
        PersonModel = PydanticSpecAdapter.create_model(operable, "PersonModel")

        original = PersonModel(name="Alice", age=30)
        updated = PydanticSpecAdapter.update_model(original, {"age": 31})

        assert updated.name == "Alice"
        assert updated.age == 31
        # Original unchanged (immutable)
        assert original.age == 30


# ---------------------------------------------------------------------------
# callable default becomes default_factory
# ---------------------------------------------------------------------------


def test_pydantic_field_adapter_uses_callable_metadata_as_default_factory():
    """A callable passed as default= to Spec becomes default_factory in FieldInfo."""
    factory = lambda: "computed"  # noqa: E731
    spec = Spec(str, name="result", default=factory, nullable=True)
    field_info = PydanticSpecAdapter.create_field(spec)

    # The callable must land in default_factory, not default
    assert field_info.default_factory is factory
    # A model built from this field should produce the factory's value
    specs = [spec]
    operable = Operable(specs)
    ResultModel = PydanticSpecAdapter.create_model(operable, "ResultModel")
    instance = ResultModel()
    assert instance.result == "computed"


# ---------------------------------------------------------------------------
# strict fuzzy_match_fields raises; non-strict coerces typos and drops unknowns
# ---------------------------------------------------------------------------


def test_pydantic_field_adapter_strict_fuzzy_match_raises_on_unmatched_key():
    """strict=True raises ValueError; strict=False coerces near-matches and drops unknowns."""
    specs = [Spec(str, name="first_name"), Spec(int, name="age")]
    operable = Operable(specs)
    NameModel = PydanticSpecAdapter.create_model(operable, "NameModel")

    # strict=True: completely unknown key must raise
    with pytest.raises(ValueError):
        PydanticSpecAdapter.fuzzy_match_fields(
            {"zzz_unknown": "x", "age": 30}, NameModel, strict=True
        )

    # strict=False: near-match "frist_name" → "first_name", unknown key dropped
    matched = PydanticSpecAdapter.fuzzy_match_fields(
        {"frist_name": "Alice", "age": 30, "extra_junk": "ignored"},
        NameModel,
        strict=False,
    )
    assert matched.get("first_name") == "Alice"
    assert matched.get("age") == 30
    assert "extra_junk" not in matched


class TestEdgeCases:
    def test_empty_operable(self):
        operable = Operable([])
        EmptyModel = PydanticSpecAdapter.create_model(operable, "EmptyModel")

        assert issubclass(EmptyModel, BaseModel)
        instance = EmptyModel()
        assert instance is not None

    def test_spec_without_name(self):
        specs = [
            Spec(str, name="valid"),
            Spec(int),  # No name
        ]
        operable = Operable(specs)
        TestModel = PydanticSpecAdapter.create_model(operable, "TestModel")

        # Only named field should be included
        assert "valid" in TestModel.model_fields
        assert len(TestModel.model_fields) == 1
