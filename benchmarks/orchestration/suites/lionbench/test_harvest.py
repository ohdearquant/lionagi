"""Unit tests for the pure parts of the harvester: diff splitting + task-text scrub."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harvest import (  # noqa: E402
    default_oracle_command,
    infer_held_out_paths,
    linked_issue,
    scrub_task_text,
    split_diff,
)

_SAMPLE_DIFF = """\
diff --git a/src/pkg/x.py b/src/pkg/x.py
index 111..222 100644
--- a/src/pkg/x.py
+++ b/src/pkg/x.py
@@ -1,3 +1,3 @@
 def f():
-    return 1
+    return 2
diff --git a/tests/pkg/test_x.py b/tests/pkg/test_x.py
index 333..444 100644
--- a/tests/pkg/test_x.py
+++ b/tests/pkg/test_x.py
@@ -1,2 +1,2 @@
 def test_f():
-    assert f() == 1
+    assert f() == 2
diff --git a/conftest.py b/conftest.py
new file mode 100644
index 000..555
--- /dev/null
+++ b/conftest.py
@@ -0,0 +1 @@
+import pytest
diff --git a/src/pkg/y_test.py b/src/pkg/y_test.py
index 666..777 100644
--- a/src/pkg/y_test.py
+++ b/src/pkg/y_test.py
@@ -1 +1 @@
-old
+new
"""


def test_split_diff_classifies_tests_dir_and_conftest_and_suffix_style():
    test_patch, gold_patch = split_diff(_SAMPLE_DIFF)
    assert "tests/pkg/test_x.py" in test_patch
    assert "conftest.py" in test_patch
    assert "src/pkg/y_test.py" in test_patch  # *_test.py suffix convention
    assert "src/pkg/x.py" in gold_patch
    assert "tests/pkg/test_x.py" not in gold_patch
    assert "conftest.py" not in gold_patch


def test_split_diff_empty_input():
    assert split_diff("") == ("", "")


def test_split_diff_root_level_conftest_only_still_matches():
    diff = (
        "diff --git a/conftest.py b/conftest.py\n"
        "--- a/conftest.py\n+++ b/conftest.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    test_patch, gold_patch = split_diff(diff)
    assert test_patch.strip()
    assert not gold_patch.strip()


def test_split_diff_does_not_misclassify_similarly_named_source_file():
    # "latest.py" contains the substring "test" but is not a test-path convention.
    diff = (
        "diff --git a/src/latest.py b/src/latest.py\n"
        "--- a/src/latest.py\n+++ b/src/latest.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    test_patch, gold_patch = split_diff(diff)
    assert not test_patch.strip()
    assert gold_patch.strip()


def test_infer_held_out_paths_dedupes_in_order():
    test_patch, _ = split_diff(_SAMPLE_DIFF)
    paths = infer_held_out_paths(test_patch)
    assert paths == ["tests/pkg/test_x.py", "conftest.py", "src/pkg/y_test.py"]


def test_default_oracle_command_joins_paths():
    assert default_oracle_command(["a.py", "b.py"]) == "uv run pytest a.py b.py -q"


def test_linked_issue_detects_closing_keyword():
    assert linked_issue("This closes #1791 for good.") == 1791
    assert linked_issue("Fixes: #42") == 42
    assert linked_issue("no reference here") is None


_PR_BODY_WITH_LEAK = """\
## Summary

Calling `parse(x)` raises when x is empty; see #1791.

The fix is to check `if not x: return None` before parsing (AUDIT-005).

```python
def parse(x):
-    return x.strip()
+    if not x:
+        return None
+    return x.strip()
```

## Test plan
- [ ] add a regression test
"""


def test_scrub_task_text_strips_fences_diff_numbers_and_fix_language():
    scrubbed = scrub_task_text(_PR_BODY_WITH_LEAK)
    assert "```" not in scrubbed
    assert "#1791" not in scrubbed
    assert "AUDIT-005" not in scrubbed
    assert "the fix is to check" not in scrubbed.lower()
    # the symptom sentence should survive — it's not fix-approach language
    assert "raises when x is empty" in scrubbed


def test_scrub_task_text_combines_issue_body():
    scrubbed = scrub_task_text("PR body text.", "Issue body text describing symptom.")
    assert "PR body text." in scrubbed
    assert "Issue body text describing symptom." in scrubbed


def test_scrub_task_text_empty_input_is_empty():
    assert scrub_task_text("", None) == ""
