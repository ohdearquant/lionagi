# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for extract_docstring utility."""

import pytest

from lionagi.libs.schema.extract_docstring import extract_docstring


class TestExtractDocstringGoogle:
    def test_simple_google_docstring(self):

        def sample_func(x: int, y: str):
            """This is a sample function.

            Args:
                x: The first parameter.
                y: The second parameter.
            """
            pass

        description, params = extract_docstring(sample_func, style="google")

        assert description == "This is a sample function."
        assert params["x"] == "The first parameter."
        assert params["y"] == "The second parameter."

    def test_google_with_type_annotations(self):

        def typed_func(x: int, y: str):
            """Function with typed parameters.

            Args:
                x (int): An integer parameter.
                y (str): A string parameter.
            """
            pass

        description, params = extract_docstring(typed_func, style="google")

        assert description == "Function with typed parameters."
        assert params["x"] == "An integer parameter."
        assert params["y"] == "A string parameter."

    def test_google_multiline_param_description(self):

        def multiline_func(x: int):
            """Function with multiline parameter.

            Args:
                x: This is a parameter
                    with a multiline description
                    that spans multiple lines.
            """
            pass

        description, params = extract_docstring(multiline_func, style="google")

        assert "multiline description" in params["x"]
        assert "spans multiple lines" in params["x"]

    def test_google_with_parameters_keyword(self):

        def params_func(x: int):
            """Function using Parameters keyword.

            Parameters:
                x: The parameter.
            """
            pass

        description, params = extract_docstring(params_func, style="google")

        assert description == "Function using Parameters keyword."
        assert params["x"] == "The parameter."

    def test_google_with_arguments_keyword(self):

        def args_func(x: int):
            """Function using Arguments keyword.

            Arguments:
                x: The parameter.
            """
            pass

        description, params = extract_docstring(args_func, style="google")

        assert description == "Function using Arguments keyword."
        assert params["x"] == "The parameter."

    def test_google_no_docstring(self):

        def no_doc():
            pass

        description, params = extract_docstring(no_doc, style="google")

        assert description is None
        assert params == {}

    def test_google_description_only(self):

        def desc_only():
            """Just a description."""
            pass

        description, params = extract_docstring(desc_only, style="google")

        assert description == "Just a description."
        assert params == {}

    def test_google_complex_docstring(self):

        def complex_func(param1: str, param2: int):
            """Complex function with detailed documentation.

            Args:
                param1: The first parameter
                    with additional details.
                param2: The second parameter.

            Returns:
                A result value.

            Raises:
                ValueError: If something goes wrong.
            """
            pass

        description, params = extract_docstring(complex_func, style="google")

        assert description == "Complex function with detailed documentation."
        assert "param1" in params
        assert "param2" in params

    def test_google_empty_lines_in_args(self):

        def empty_lines_func(x: int, y: str):
            """Function with empty lines.

            Args:
                x: First parameter.

                y: Second parameter.
            """
            pass

        description, params = extract_docstring(empty_lines_func, style="google")

        assert "x" in params
        assert "y" in params


class TestExtractDocstringRest:
    def test_simple_rest_docstring(self):

        def sample_func(x: int, y: str):
            """This is a sample function.

            :param x: The first parameter.
            :param y: The second parameter.
            """
            pass

        description, params = extract_docstring(sample_func, style="rest")

        assert description == "This is a sample function."
        assert params["x"] == "The first parameter."
        assert params["y"] == "The second parameter."

    def test_rest_with_type_annotations(self):

        def typed_func(x: int, y: str):
            """Function with typed parameters.

            :param x: An integer parameter.
            :type x: int
            :param y: A string parameter.
            :type y: str
            """
            pass

        description, params = extract_docstring(typed_func, style="rest")

        assert description == "Function with typed parameters."
        assert params["x"] == "An integer parameter."
        assert params["y"] == "A string parameter."

    def test_rest_multiline_param_description(self):

        def multiline_func(x: int):
            """Function with multiline parameter.

            :param x: This is a parameter
                with a multiline description
                that spans multiple lines.
            """
            pass

        description, params = extract_docstring(multiline_func, style="rest")

        assert "x" in params
        # The implementation joins multiline descriptions with spaces
        assert "This is a parameter" in params["x"]

    def test_rest_no_docstring(self):

        def no_doc():
            pass

        description, params = extract_docstring(no_doc, style="rest")

        assert description is None
        assert params == {}

    def test_rest_description_only(self):

        def desc_only():
            """Just a description."""
            pass

        description, params = extract_docstring(desc_only, style="rest")

        assert description == "Just a description."
        assert params == {}

    def test_rest_complex_docstring(self):

        def complex_func(param1: str, param2: int):
            """Complex function with detailed documentation.

            :param param1: The first parameter.
            :type param1: str
            :param param2: The second parameter.
            :type param2: int
            :returns: A result value.
            :rtype: str
            :raises ValueError: If something goes wrong.
            """
            pass

        description, params = extract_docstring(complex_func, style="rest")

        assert description == "Complex function with detailed documentation."
        assert "param1" in params
        assert "param2" in params


class TestExtractDocstringGeneral:
    def test_unsupported_style(self):

        def sample_func():
            """Test function."""
            pass

        with pytest.raises(ValueError) as excinfo:
            extract_docstring(sample_func, style="numpy")

        assert "not supported" in str(excinfo.value).lower()

    def test_style_case_insensitive(self):

        def sample_func(x: int):
            """Test function.

            Args:
                x: Parameter x.
            """
            pass

        # Test various case combinations
        desc1, params1 = extract_docstring(sample_func, style="GOOGLE")
        desc2, params2 = extract_docstring(sample_func, style="Google")
        desc3, params3 = extract_docstring(sample_func, style="google")

        assert desc1 == desc2 == desc3
        assert params1 == params2 == params3

    def test_style_with_whitespace(self):

        def sample_func(x: int):
            """Test function.

            Args:
                x: Parameter x.
            """
            pass

        desc, params = extract_docstring(sample_func, style="  google  ")
        assert desc == "Test function."
        assert "x" in params

    def test_default_style_is_google(self):

        def sample_func(x: int):
            """Test function.

            Args:
                x: Parameter x.
            """
            pass

        # Call without specifying style (should default to google)
        desc, params = extract_docstring(sample_func)
        assert desc == "Test function."
        assert "x" in params

    def test_function_with_no_parameters_google(self):

        def no_params():
            """Function with no parameters.

            Returns:
                None
            """
            pass

        description, params = extract_docstring(no_params, style="google")
        assert description == "Function with no parameters."
        assert params == {}

    def test_function_with_no_parameters_rest(self):

        def no_params():
            """Function with no parameters.

            :returns: None
            """
            pass

        description, params = extract_docstring(no_params, style="rest")
        assert description == "Function with no parameters."
        assert params == {}

    def test_parameter_with_special_characters(self):

        def special_params(param_1: int, param_2_long: str):
            """Function with special parameter names.

            Args:
                param_1: First parameter.
                param_2_long: Second parameter.
            """
            pass

        description, params = extract_docstring(special_params, style="google")
        assert "param_1" in params
        assert "param_2_long" in params

    def test_real_world_example_google(self):

        def calculate_statistics(data: list, method: str, precision: int):
            """Calculate statistical measures from data.

            This function computes various statistical measures based on the
            specified method and returns results with the given precision.

            Args:
                data (list): The input data as a list of numbers.
                method (str): The statistical method to apply.
                    Supported methods: 'mean', 'median', 'std'.
                precision (int): Number of decimal places for results.

            Returns:
                dict: A dictionary containing the calculated statistics.

            Raises:
                ValueError: If the method is not supported.
                TypeError: If data contains non-numeric values.

            Example:
                >>> calculate_statistics([1, 2, 3, 4, 5], 'mean', 2)
                {'mean': 3.00}
            """
            pass

        description, params = extract_docstring(calculate_statistics, style="google")

        assert "Calculate statistical measures" in description
        assert "data" in params
        assert "method" in params
        assert "precision" in params
        # The implementation extracts the first line of multiline descriptions
        assert "statistical method" in params["method"].lower()

    def test_real_world_example_rest(self):

        def process_data(input_file: str, output_format: str):
            """Process data from input file and convert to specified format.

            :param input_file: Path to the input data file.
            :type input_file: str
            :param output_format: Desired output format (json, csv, xml).
            :type output_format: str
            :returns: Path to the generated output file.
            :rtype: str
            :raises FileNotFoundError: If input file does not exist.
            :raises ValueError: If output format is not supported.
            """
            pass

        description, params = extract_docstring(process_data, style="rest")

        assert "Process data from input file" in description
        assert "input_file" in params
        assert "output_format" in params
