# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Additional coverage for lionhub.lndl._parse_function_call: nested arg types,
error paths, and batch-parse edge cases. Basic call/service/qualified_name
behavior is covered in test_parser_assembler.py::TestParseFunctionCall."""

from __future__ import annotations

import pytest

from lionhub.lndl._parse_function_call import (
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


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ('fn(x="from=1")', {"x": "from=1"}),
        ("fn(x='class=1, def = 2')", {"x": "class=1, def = 2"}),
        ('''fn(x="""from=1\nclass = 2""")''', {"x": "from=1\nclass = 2"}),
        (r'fn(x=r"from=1\\path")', {"x": r"from=1\\path"}),
        (r"""fn(x="escaped \" from=1")""", {"x": 'escaped " from=1'}),
        ('fn(x={"from=1": ["class=2", "await=3"]})', {"x": {"from=1": ["class=2", "await=3"]}}),
        ('fn(from="from=1", class = "class=2")', {"from_": "from=1", "class_": "class=2"}),
        ('fn(x="é from=1", from="from=2")', {"x": "é from=1", "from_": "from=2"}),
        ('fn(x="from=1" " class=2")', {"x": "from=1 class=2"}),
        ('fn(x=b"from=1")', {"x": b"from=1"}),
        ('fn(\nfrom\n= "from=1",\n)', {"from_": "from=1"}),
        ('fn(\nx="""line\rbreak""",\nfrom="from=1")', {"x": "line\nbreak", "from_": "from=1"}),
        ('fn(\n x="雪",\n from="class=2"\n)', {"x": "雪", "from_": "class=2"}),
    ],
)
def test_reserved_keyword_escaping_preserves_string_literals(batch, call, expected):
    if batch:
        result = parse_batch_function_calls(f"[{call}]")[0]
    else:
        result = parse_function_call(call)
    assert result["arguments"] == expected


class TestParseFunctionCallErrors:
    @pytest.mark.parametrize(
        ("parse", "text"),
        [
            (parse_function_call, 'fn(x="""unterminated)'),
            (parse_batch_function_calls, '[fn(x="""unterminated)]'),
        ],
    )
    def test_unterminated_string_keeps_public_error_type(self, parse, text):
        with pytest.raises(ValueError, match="Invalid .*function call syntax"):
            parse(text)

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

    @pytest.mark.parametrize(
        ("batch", "message"),
        [
            ("[fn(**d)]", r"^Invalid batch function call syntax: \*\*kwargs not supported$"),
            ("[x[0](y=1)]", r"^Invalid batch function call syntax: Unsupported function type"),
            ("[fn(*a)]", r"^Invalid batch function call syntax: "),
        ],
    )
    def test_batch_element_errors_carry_one_prefix(self, batch, message):
        with pytest.raises(ValueError, match=message) as info:
            parse_batch_function_calls(batch)
        assert str(info.value).count("Invalid") == 1

    def test_batch_with_service_prefix(self):
        results = parse_batch_function_calls('[memory.recall(q="a"), search(q="b")]')
        assert results[0]["service"] == "memory"
        assert results[0]["action"] == "recall"
        assert "service" not in results[1]

    def test_batch_elements_match_single_call_parses(self):
        calls = ['a(from="x", n=1)', 'svc.b([1, 2], d={"k": null}, flag=true)', "c()"]
        results = parse_batch_function_calls("[" + ", ".join(calls) + "]")
        assert results == [parse_function_call(call) for call in calls]
        assert results[0]["arguments"] == {"from_": "x", "n": 1}

    def test_batch_does_not_rewrite_a_decoded_literal_into_a_keyword(self):
        """A string whose escapes decode to ``from=`` keeps that text in a batch."""
        call = r'fn(x="\x66rom=1")'
        assert parse_function_call(call)["arguments"] == {"x": "from=1"}
        assert parse_batch_function_calls(f"[{call}]")[0]["arguments"] == {"x": "from=1"}
