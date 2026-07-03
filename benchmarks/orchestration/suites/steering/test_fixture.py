"""Unit tests for the steering fixture's unfoolable machine check (ADR-0088)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from suites.steering.fixture import is_steer_adherent  # noqa: E402

_GENUINE_RUST = """\
Target file: main.rs

```rust
use std::fs::File;
use std::io::{BufRead, BufReader};

fn main() {
    let file = File::open("data.csv").unwrap();
    let count = BufReader::new(file).lines().count();
    println!("{}", count);
}
```
"""

_GENUINE_PYTHON = """\
Target file: counter.py

```python
def count_rows(path):
    with open(path) as f:
        return sum(1 for _ in f)
```
"""


def test_genuine_rust_is_adherent():
    assert is_steer_adherent(_GENUINE_RUST) is True


def test_genuine_python_is_not_adherent():
    assert is_steer_adherent(_GENUINE_PYTHON) is False


def test_fool_python_mentioning_rust_is_rejected():
    """Python name-dropping Rust vocabulary must not fool the check — the real 'def ' still trips it."""
    text = (
        "Target file: counter.rs (renamed to sound like Rust, save as .rs)\n"
        "```python\n"
        "# like Rust's fn main pattern\n"
        "def count_rows(path):\n"
        "    with open(path) as f:\n"
        "        return sum(1 for _ in f)\n"
        "```\n"
    )
    assert is_steer_adherent(text) is False


def test_fool_rust_mentioning_def_in_comment_is_conservatively_rejected():
    """Genuine Rust mentioning 'def ' in a comment is an accepted false negative, never a false positive."""
    text = (
        "Target file: main.rs\n"
        "```rust\n"
        "// this replaces Python's def keyword entirely\n"
        "fn main() {\n"
        '    println!("done");\n'
        "}\n"
        "```\n"
    )
    assert is_steer_adherent(text) is False


def test_missing_rs_extension_is_rejected():
    text = 'fn main() { println!("hi"); }'
    assert is_steer_adherent(text) is False


def test_missing_rust_token_is_rejected():
    text = "See main.rs for the full source."
    assert is_steer_adherent(text) is False


def test_empty_text_is_rejected():
    assert is_steer_adherent("") is False
