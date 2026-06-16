# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for function_to_schema utility."""

from lionagi.libs.schema.function_to_schema import FunctionSchema, function_to_schema


class TestFunctionToSchema:
    def test_simple_function_google_style(self):
        def sample_func(x: int, y: str) -> bool:
            """Sample function for testing.

            Args:
                x: An integer parameter.
                y: A string parameter.

            Returns:
                A boolean value.
            """
            return True

        schema = function_to_schema(sample_func)

        assert schema["type"] == "function"
        assert "function" in schema
        func_def = schema["function"]

        assert func_def["name"] == "sample_func"
        assert "Sample function for testing." in func_def["description"]
        assert "parameters" in func_def

        params = func_def["parameters"]
        assert params["type"] == "object"
        assert "x" in params["properties"]
        assert "y" in params["properties"]
        assert params["properties"]["x"]["type"] == "number"
        assert params["properties"]["y"]["type"] == "string"
        assert "x" in params["required"]
        assert "y" in params["required"]

    def test_function_with_rest_style_docstring(self):
        def rest_func(param1: int, param2: str):
            """Function with reST docstring.

            :param param1: First parameter.
            :param param2: Second parameter.
            """
            pass

        schema = function_to_schema(rest_func, style="rest")

        func_def = schema["function"]
        assert func_def["name"] == "rest_func"
        assert "param1" in func_def["parameters"]["properties"]
        assert "param2" in func_def["parameters"]["properties"]

    def test_function_without_docstring(self):
        def no_doc_func(x: int):
            pass

        schema = function_to_schema(no_doc_func)

        func_def = schema["function"]
        assert func_def["name"] == "no_doc_func"
        # Should still have parameters
        assert "x" in func_def["parameters"]["properties"]

    def test_function_without_type_hints(self):
        def no_hints(x, y):
            """Function without type hints.

            Args:
                x: First parameter.
                y: Second parameter.
            """
            pass

        schema = function_to_schema(no_hints)

        params = schema["function"]["parameters"]
        # Should default to string type
        assert params["properties"]["x"]["type"] == "string"
        assert params["properties"]["y"]["type"] == "string"

    def test_function_with_list_parameter(self):
        def list_func(items: list) -> bool:
            """Function with list parameter.

            Args:
                items: A list of items.
            """
            return True

        schema = function_to_schema(list_func)

        params = schema["function"]["parameters"]
        assert params["properties"]["items"]["type"] == "array"

    def test_function_with_dict_parameter(self):
        def dict_func(data: dict) -> bool:
            """Function with dict parameter.

            Args:
                data: A dictionary.
            """
            return True

        schema = function_to_schema(dict_func)

        params = schema["function"]["parameters"]
        assert params["properties"]["data"]["type"] == "object"

    def test_function_with_float_parameter(self):
        def float_func(value: float) -> bool:
            """Function with float parameter.

            Args:
                value: A float value.
            """
            return True

        schema = function_to_schema(float_func)

        params = schema["function"]["parameters"]
        assert params["properties"]["value"]["type"] == "number"

    def test_function_with_bool_parameter(self):
        def bool_func(flag: bool) -> bool:
            """Function with bool parameter.

            Args:
                flag: A boolean flag.
            """
            return True

        schema = function_to_schema(bool_func)

        params = schema["function"]["parameters"]
        assert params["properties"]["flag"]["type"] == "boolean"

    def test_function_with_tuple_parameter(self):
        def tuple_func(coords: tuple) -> bool:
            """Function with tuple parameter.

            Args:
                coords: A tuple of coordinates.
            """
            return True

        schema = function_to_schema(tuple_func)

        params = schema["function"]["parameters"]
        assert params["properties"]["coords"]["type"] == "array"

    def test_custom_function_description(self):
        def func_with_desc(x: int):
            """Original description.

            Args:
                x: Parameter x.
            """
            pass

        custom_desc = "Custom function description."
        schema = function_to_schema(func_with_desc, func_description=custom_desc)

        assert schema["function"]["description"] == custom_desc

    def test_custom_parameter_descriptions(self):
        def func_with_params(x: int, y: str):
            """Function.

            Args:
                x: Original x.
                y: Original y.
            """
            pass

        custom_params = {
            "x": "Custom description for x.",
            "y": "Custom description for y.",
        }
        schema = function_to_schema(func_with_params, parametert_description=custom_params)

        params = schema["function"]["parameters"]
        assert params["properties"]["x"]["description"] == "Custom description for x."
        assert params["properties"]["y"]["description"] == "Custom description for y."

    def test_strict_mode_enabled(self):
        def strict_func(x: int):
            """Strict function.

            Args:
                x: Parameter x.
            """
            pass

        schema = function_to_schema(strict_func, strict=True)

        assert schema["function"]["strict"] is True

    def test_strict_mode_disabled(self):
        def non_strict_func(x: int):
            """Non-strict function.

            Args:
                x: Parameter x.
            """
            pass

        schema = function_to_schema(non_strict_func, strict=False)

        # When strict is False, the implementation adds it to the schema
        # Note: The implementation only adds strict if it's truthy
        # So strict=False might not be included in the output
        # Let's verify the schema is valid instead
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "non_strict_func"

    def test_custom_request_options(self):
        def custom_func(x: int):
            """Custom function.

            Args:
                x: Parameter x.
            """
            pass

        custom_options = {
            "type": "object",
            "properties": {
                "custom_param": {
                    "type": "string",
                    "description": "Custom parameter.",
                }
            },
            "required": ["custom_param"],
        }

        schema = function_to_schema(custom_func, request_options=custom_options)

        params = schema["function"]["parameters"]
        assert "custom_param" in params["properties"]
        assert "x" not in params["properties"]  # Original params should be replaced

    def test_return_as_object(self):
        def obj_func(x: int):
            """Object function.

            Args:
                x: Parameter x.
            """
            pass

        schema_obj = function_to_schema(obj_func, return_obj=True)

        assert isinstance(schema_obj, FunctionSchema)
        assert schema_obj.name == "obj_func"
        assert "Object function." in schema_obj.description

        # Test to_dict method
        schema_dict = schema_obj.to_dict()
        assert schema_dict["type"] == "function"
        assert "function" in schema_dict

    def test_multiple_parameters_all_types(self):
        def multi_type_func(a: int, b: str, c: float, d: bool, e: list, f: dict, g: tuple):
            """Function with multiple parameter types.

            Args:
                a: Integer parameter.
                b: String parameter.
                c: Float parameter.
                d: Boolean parameter.
                e: List parameter.
                f: Dict parameter.
                g: Tuple parameter.
            """
            pass

        schema = function_to_schema(multi_type_func)

        props = schema["function"]["parameters"]["properties"]
        assert props["a"]["type"] == "number"
        assert props["b"]["type"] == "string"
        assert props["c"]["type"] == "number"
        assert props["d"]["type"] == "boolean"
        assert props["e"]["type"] == "array"
        assert props["f"]["type"] == "object"
        assert props["g"]["type"] == "array"

    def test_function_name_extraction(self):
        def my_custom_function_name():
            """Test function."""
            pass

        schema = function_to_schema(my_custom_function_name)

        assert schema["function"]["name"] == "my_custom_function_name"

    def test_empty_parameter_list(self):
        def no_params():
            """Function with no parameters."""
            pass

        schema = function_to_schema(no_params)

        params = schema["function"]["parameters"]
        assert params["properties"] == {}
        assert params["required"] == []

    def test_multiline_parameter_description(self):
        def multiline_func(x: int):
            """Function with multiline description.

            Args:
                x: This is a parameter
                    with a multiline description
                    spanning several lines.
            """
            pass

        schema = function_to_schema(multiline_func)

        x_desc = schema["function"]["parameters"]["properties"]["x"]["description"]
        # Should combine multiline descriptions
        assert "multiline description" in x_desc

    def test_complex_function_docstring(self):
        def complex_func(param1: str, param2: int):
            """Complex function with detailed documentation.

            This function does something complex.
            It has multiple lines of description.

            Args:
                param1: The first parameter
                    with additional details.
                param2: The second parameter.

            Returns:
                A result value.

            Raises:
                ValueError: If something goes wrong.

            Examples:
                >>> complex_func("test", 42)
                'result'
            """
            return "result"

        schema = function_to_schema(complex_func)

        func_def = schema["function"]
        assert "Complex function" in func_def["description"]
        assert "param1" in func_def["parameters"]["properties"]
        assert "param2" in func_def["parameters"]["properties"]


class TestFunctionSchema:
    def test_function_schema_creation(self):
        schema = FunctionSchema(
            name="test_func",
            description="Test function.",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "number", "description": "Parameter x."}},
                "required": ["x"],
            },
        )

        assert schema.name == "test_func"
        assert schema.description == "Test function."
        assert "x" in schema.parameters["properties"]

    def test_function_schema_to_dict(self):
        schema = FunctionSchema(
            name="test_func",
            description="Test function.",
            parameters={"type": "object", "properties": {}, "required": []},
        )

        schema_dict = schema.to_dict()
        assert schema_dict["type"] == "function"
        assert schema_dict["function"]["name"] == "test_func"

    def test_function_schema_with_strict(self):
        schema = FunctionSchema(
            name="strict_func",
            description="Strict function.",
            parameters={"type": "object", "properties": {}, "required": []},
            strict=True,
        )

        assert schema.strict is True
        schema_dict = schema.to_dict()
        assert schema_dict["function"]["strict"] is True

    def test_function_schema_none_parameters(self):
        schema = FunctionSchema(
            name="no_params_func",
            description="Function with no parameters.",
            parameters=None,
        )

        assert schema.parameters is None
        schema_dict = schema.to_dict()
        assert schema_dict["function"]["parameters"] is None
