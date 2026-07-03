# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Additional coverage for lionagi.lndl._parse_function_call: nested arg types,
error paths, and batch-parse edge cases. Basic call/service/qualified_name
behavior is covered in test_parser_assembler.py::TestParseFunctionCall."""

from __future__ import annotations

import pytest

from lionagi.lndl._parse_function_call import (
    parse_batch_function_calls,
    parse_function_call,
)


class TestArgumentValueTypes:
    def test_list_argument(self):
        result = parse_function_call("fn(x=[1, 2, 3])")
        assert result["arguments"]["x"] == [1, 2, 3]

    def test_tuple_argument(self):
        result = parse_function_call("fn(x=(1, 2))")
        assert result["arguments"]["x"] == (1, 2)

    def test_positional_arguments_get_pos_keys(self):
        result = parse_function_call("fn(1, 2, x=3)")
        assert result["arguments"]["_pos_0"] == 1
        assert result["arguments"]["_pos_1"] == 2
        assert result["arguments"]["x"] == 3

    def test_nested_service_attribute(self):
        result = parse_function_call("a.b.tool(x=1)")
        assert result["action"] == "tool"
        assert result["service"] == "b"


class TestParseFunctionCallErrors:
    def test_unsupported_function_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported function type"):
            parse_function_call("x[0](y=1)")

    def test_dict_unpack_kwargs_raises(self):
        with pytest.raises(ValueError, match=r"\*\*kwargs not supported"):
            parse_function_call("fn(**data)")

    def test_bare_name_argument_raises(self):
        """An unquoted identifier as an argument value isn't a valid literal."""
        with pytest.raises(ValueError, match="not a valid literal"):
            parse_function_call("fn(x=undefined_name)")

    def test_not_a_call_raises(self):
        with pytest.raises(ValueError, match="Invalid function call"):
            parse_function_call("[1, 2, 3]")


class TestParseBatchFunctionCalls:
    def test_missing_brackets_raises(self):
        with pytest.raises(ValueError, match=r"enclosed in \[ \]"):
            parse_batch_function_calls("not brackets")

    def test_non_list_expression_raises(self):
        """Starts/ends with [ ] but isn't a plain list literal (e.g. a comprehension)."""
        with pytest.raises(ValueError, match="Not a list expression"):
            parse_batch_function_calls("[x for x in y]")

    def test_non_call_element_raises(self):
        with pytest.raises(ValueError, match="not a function call"):
            parse_batch_function_calls("[1, 2]")

    def test_element_parse_failure_propagates(self):
        with pytest.raises(ValueError, match="not a valid literal"):
            parse_batch_function_calls("[fn(x=undefined_name)]")

    def test_batch_with_service_prefix(self):
        results = parse_batch_function_calls('[memory.recall(q="a"), search(q="b")]')
        assert results[0]["service"] == "memory"
        assert results[0]["action"] == "recall"
        assert "service" not in results[1]
