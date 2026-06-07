# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for extract_code_block utility."""

from lionagi.libs.schema.extract_code_block import extract_code_block


class TestExtractCodeBlock:
    """Test cases for extract_code_block function."""

    def test_single_code_block_default(self):
        """Test extracting a single code block with default settings."""
        markdown = """
Some text before.

```python
def hello():
    return "world"
```

Some text after.
"""
        result = extract_code_block(markdown)
        assert isinstance(result, str)
        assert "def hello():" in result
        assert '    return "world"' in result

    def test_multiple_code_blocks_as_string(self):
        """Test extracting multiple code blocks joined as string."""
        markdown = """
```python
x = 1
```

```javascript
const y = 2;
```
"""
        result = extract_code_block(markdown)
        assert isinstance(result, str)
        assert "x = 1" in result
        assert "const y = 2;" in result
        assert "\n\n" in result  # Should be joined with double newline

    def test_multiple_code_blocks_as_list(self):
        """Test extracting multiple code blocks as list."""
        markdown = """
```python
x = 1
```

```javascript
const y = 2;
```
"""
        result = extract_code_block(markdown, return_as_list=True)
        assert isinstance(result, list)
        assert len(result) == 2
        assert "x = 1" in result[0]
        assert "const y = 2;" in result[1]

    def test_categorize_by_language(self):
        """Test categorizing code blocks by language."""
        markdown = """
```python
x = 1
```

```python
y = 2
```

```javascript
const z = 3;
```
"""
        result = extract_code_block(markdown, categorize=True)
        assert isinstance(result, dict)
        assert "python" in result
        assert "javascript" in result
        assert len(result["python"]) == 2
        assert len(result["javascript"]) == 1
        assert "x = 1" in result["python"][0]
        assert "y = 2" in result["python"][1]
        assert "const z = 3;" in result["javascript"][0]

    def test_filter_by_language(self):
        """Test filtering code blocks by specific languages."""
        markdown = """
```python
x = 1
```

```javascript
const y = 2;
```

```ruby
z = 3
```
"""
        result = extract_code_block(markdown, languages=["python", "ruby"], return_as_list=True)
        assert len(result) == 2
        assert "x = 1" in result[0]
        assert "z = 3" in result[1]

    def test_tilde_fence(self):
        """Test extraction with tilde fence (~~~) instead of backticks."""
        markdown = """
~~~python
def test():
    pass
~~~
"""
        result = extract_code_block(markdown)
        assert "def test():" in result
        assert "    pass" in result

    def test_mixed_fences(self):
        """Test extraction with mixed fence types."""
        markdown = """
```python
x = 1
```

~~~javascript
const y = 2;
~~~
"""
        result = extract_code_block(markdown, return_as_list=True)
        assert len(result) == 2
        assert "x = 1" in result[0]
        assert "const y = 2;" in result[1]

    def test_no_language_specified(self):
        """Test code blocks without language identifier."""
        markdown = """
```
plain code block
no language
```
"""
        result = extract_code_block(markdown, categorize=True)
        assert "plain" in result
        assert "plain code block" in result["plain"][0]

    def test_empty_code_block(self):
        """Test empty code blocks."""
        markdown = """
```python
```
"""
        result = extract_code_block(markdown, return_as_list=True)
        assert len(result) == 1
        assert result[0] == ""

    def test_no_code_blocks(self):
        """Test markdown with no code blocks."""
        markdown = "Just some plain text with no code."
        result = extract_code_block(markdown)
        assert result == ""

    def test_no_code_blocks_as_list(self):
        """Test markdown with no code blocks returning as list."""
        markdown = "Just some plain text with no code."
        result = extract_code_block(markdown, return_as_list=True)
        assert result == []

    def test_no_code_blocks_categorized(self):
        """Test markdown with no code blocks categorized."""
        markdown = "Just some plain text with no code."
        result = extract_code_block(markdown, categorize=True)
        assert result == {}

    def test_code_block_with_inline_backticks(self):
        """Test code blocks containing inline backticks."""
        markdown = """
```python
# This is a comment with `inline code`
x = `some value`
```
"""
        result = extract_code_block(markdown)
        assert "# This is a comment with `inline code`" in result
        assert "x = `some value`" in result

    def test_malformed_fence_not_extracted(self):
        """Test that malformed fences are not extracted."""
        markdown = """
```python
x = 1

Some text without closing fence.
"""
        result = extract_code_block(markdown, return_as_list=True)
        assert len(result) == 0  # Should not extract incomplete block

    def test_nested_code_in_markdown(self):
        """Test handling of nested-looking structures."""
        markdown = """
```markdown
# Example Markdown

```python
def nested():
    pass
```

End of example.
```
"""
        result = extract_code_block(markdown, return_as_list=True)
        # Should extract the outer markdown block only
        assert len(result) == 1
        assert "# Example Markdown" in result[0]

    def test_language_with_hyphens(self):
        """Test language identifiers with hyphens."""
        markdown = """
```objective-c
NSString *test = @"hello";
```
"""
        result = extract_code_block(markdown, categorize=True)
        assert "objective-c" in result
        assert "NSString *test" in result["objective-c"][0]

    def test_language_with_plus(self):
        """Test language identifiers with plus signs."""
        markdown = """
```c++
int main() { return 0; }
```
"""
        result = extract_code_block(markdown, categorize=True)
        assert "c++" in result

    def test_whitespace_handling(self):
        """Test proper handling of whitespace in and around code blocks."""
        markdown = """
```python
x = 1
y = 2
```
"""
        result = extract_code_block(markdown)
        assert "x = 1" in result
        assert "y = 2" in result

    def test_multiline_code_with_blank_lines(self):
        """Test code blocks with blank lines inside."""
        markdown = """
```python
def function1():
    pass

def function2():
    pass
```
"""
        result = extract_code_block(markdown)
        assert "def function1():" in result
        assert "def function2():" in result

    def test_consecutive_code_blocks(self):
        """Test extraction of consecutive code blocks."""
        markdown = """
```python
x = 1
```
```python
y = 2
```
"""
        result = extract_code_block(markdown, return_as_list=True)
        assert len(result) == 2

    def test_real_world_example(self):
        """Test with a real-world-like markdown document."""
        markdown = """
# Documentation

Here's an example in Python:

```python
def calculate_sum(a, b):
    return a + b
```

And the equivalent in JavaScript:

```javascript
function calculateSum(a, b) {
    return a + b;
}
```

You can also use it in Ruby:

```ruby
def calculate_sum(a, b)
  a + b
end
```
"""
        result = extract_code_block(markdown, categorize=True)
        assert "python" in result
        assert "javascript" in result
        assert "ruby" in result
        assert "calculate_sum" in result["python"][0]
        assert "calculateSum" in result["javascript"][0]
        assert "calculate_sum" in result["ruby"][0]

    def test_filter_and_categorize_conflict(self):
        """Test that categorize and language filter work together."""
        markdown = """
```python
x = 1
```

```javascript
const y = 2;
```

```python
z = 3
```
"""
        result = extract_code_block(markdown, languages=["python"], categorize=True)
        assert "python" in result
        assert "javascript" not in result
        assert len(result["python"]) == 2
