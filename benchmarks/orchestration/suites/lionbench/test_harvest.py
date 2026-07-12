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
    # --all-extras: held-out tests can import extra-gated modules (e.g. studio's
    # fastapi) that a plain `uv run pytest` won't have installed (PR #1643).
    assert default_oracle_command(["a.py", "b.py"]) == "uv run --all-extras pytest a.py b.py -q"


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


_PR_BODY_WITH_MARKDOWN_BULLETS = """\
## What

Something broke. Two coupled fixes:

- **`pkg/a.py`** — did the first thing, unrelated to any diff hunk.
- **`pkg/b.py`** — did the second thing.

## Tests
- `tests/test_a.py` — coverage for the first thing.
"""


def test_scrub_task_text_preserves_markdown_bullet_lists():
    """A bare '- ' prefix is an ordinary markdown bullet, not a diff line — the
    scrub must not treat every dash-prefixed line as diff noise (real
    regression: this nuked PR #1665's bulleted fix summary before the fix)."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_MARKDOWN_BULLETS)
    assert "did the first thing" in scrubbed
    assert "did the second thing" in scrubbed
    assert "coverage for the first thing" in scrubbed


_PR_BODY_WITH_UNFENCED_RAW_DIFF = """\
## Summary

Calling `parse(x)` raises when x is empty.

diff --git a/pkg/a.py b/pkg/a.py
index abc123..def456 100644
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -10,6 +10,8 @@ def parse(x):
     if not x:
-        return old_value
+        return secret_fix_value

## Test plan
- [ ] add a regression test
"""


def test_scrub_task_text_strips_unfenced_raw_diff_blocks():
    """A PR/issue body is not guaranteed to fence a pasted patch — a diff can
    appear raw. The scrub must recognize a real `diff --git ` header and drop
    its structural + content lines, including a hunk header with trailing
    function context (`@@ ... @@ def parse(x):`), without needing a fence."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_UNFENCED_RAW_DIFF)
    assert "diff --git" not in scrubbed
    assert "secret_fix_value" not in scrubbed
    assert "old_value" not in scrubbed
    assert "@@" not in scrubbed
    # the surrounding prose survives
    assert "raises when x is empty" in scrubbed
    assert "add a regression test" in scrubbed


_PR_BODY_WITH_NEW_FILE_DIFF = """\
## Summary

Adds a helper module.

diff --git a/pkg/new.py b/pkg/new.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/pkg/new.py
@@ -0,0 +1,2 @@
+secret_fix_value = 1
+return secret_fix_value

## Test plan
- [ ] add a regression test
"""

_PR_BODY_WITH_RENAME_DIFF = """\
## Summary

Renames a module and tweaks a constant.

diff --git a/pkg/old_name.py b/pkg/new_name.py
similarity index 92%
rename from pkg/old_name.py
rename to pkg/new_name.py
index abc123..def456 100644
--- a/pkg/old_name.py
+++ b/pkg/new_name.py
@@ -1,3 +1,3 @@
 unchanged_context_line
-old_secret_value
+new_secret_value

## Test plan
- [ ] add a regression test
"""

_PR_BODY_WITH_DELETED_FILE_DIFF = """\
## Summary

Removes a dead module.

diff --git a/pkg/gone.py b/pkg/gone.py
deleted file mode 100644
index 1111111..0000000
--- a/pkg/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-deleted_secret_value = 1
-return deleted_secret_value

## Test plan
- [ ] add a regression test
"""

_PR_BODY_WITH_BINARY_PATCH = """\
## Summary

Updates the logo asset.

diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..abcdef1
GIT binary patch
literal 128
zcmZ?wbhEHb6k=W1@$Vw+super_secret_blob_data
literal 0
HcmV?d00001

## Test plan
- [ ] check the logo renders
"""


def test_scrub_task_text_strips_new_file_diff():
    """`new file mode` is an unprefixed extended-header line — a naive scanner
    that disarms on the first non +/-/space/blank line lets the rest of a
    new-file diff's content through. Must stay armed across it."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_NEW_FILE_DIFF)
    assert "diff --git" not in scrubbed
    assert "new file mode" not in scrubbed
    assert "secret_fix_value" not in scrubbed
    assert "raises" not in scrubbed  # sanity: this fixture has no such sentence
    assert "adds a helper module" in scrubbed.lower()
    assert "add a regression test" in scrubbed


def test_scrub_task_text_strips_rename_diff():
    """`similarity index` / `rename from` / `rename to` are unprefixed extended
    headers too — same leak shape as new-file diffs."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_RENAME_DIFF)
    assert "diff --git" not in scrubbed
    assert "similarity index" not in scrubbed
    assert "rename from" not in scrubbed
    assert "rename to" not in scrubbed
    assert "old_secret_value" not in scrubbed
    assert "new_secret_value" not in scrubbed
    assert "renames a module" in scrubbed.lower()
    assert "add a regression test" in scrubbed


def test_scrub_task_text_strips_deleted_file_diff():
    scrubbed = scrub_task_text(_PR_BODY_WITH_DELETED_FILE_DIFF)
    assert "diff --git" not in scrubbed
    assert "deleted file mode" not in scrubbed
    assert "deleted_secret_value" not in scrubbed
    assert "removes a dead module" in scrubbed.lower()
    assert "add a regression test" in scrubbed


def test_scrub_task_text_strips_binary_patch_blob():
    """`GIT binary patch` base85 blob lines have no fixed prefix shape (no +/-/
    space guarantee) — they need their own always-drop mode, not the
    structural/content/extended-header checks. Binary mode stays armed
    through end-of-input here (no later `diff --git ` header), so the
    trailing prose is eaten too — an accepted over-strip trade-off, not a
    bug: a blank line is not a reliable end-of-binary-patch marker (see the
    two-hunk test below), so there is no safe place to resume."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_BINARY_PATCH)
    assert "diff --git" not in scrubbed
    assert "GIT binary patch" not in scrubbed
    assert "super_secret_blob_data" not in scrubbed
    assert "check the logo renders" not in scrubbed
    assert "updates the logo asset" in scrubbed.lower()


_PR_BODY_WITH_TWO_HUNK_BINARY_PATCH = """\
## Summary

Tweaks an icon (real `git diff --binary` two-hunk shape for one file).

diff --git a/assets/icon.png b/assets/icon.png
index 1111111..2222222 100644
GIT binary patch
literal 15
WcmZQz%u6lTP1G$;O)g3;`40dhZv~71_old_blob_secret

literal 8
PcmZQz%+E>DP5ci42%Q4X_new_blob_secret
"""

_PR_BODY_WITH_TWO_FILE_BINARY_DIFF = """\
## Summary

Updates two logo assets.

diff --git a/assets/logo_a.png b/assets/logo_a.png
index 1111111..2222222 100644
GIT binary patch
literal 15
WcmZQz%u6lTP1G$;O)g3;`40dhZv~71_logo_a_secret

literal 8
PcmZQz%+E>DP5ci42%Q4X_logo_a_secret_2
diff --git a/assets/logo_b.png b/assets/logo_b.png
index 3333333..4444444 100644
GIT binary patch
literal 20
WcmZQz%u6lTP1G$;O)g3;`40dhZv~71_logo_b_secret

literal 9
PcmZQz%+E>DP5ci42%Q4X_logo_b_secret_2
"""


def test_scrub_task_text_strips_two_hunk_binary_patch_internal_blank_line():
    """Regression: real `git diff --binary` puts a blank line BETWEEN
    a file's two binary hunk records (literal N / base85 / blank / literal M
    / base85) — that blank line is not the end of the patch. The previous
    fix disarmed binary mode on it and leaked the second hunk."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_TWO_HUNK_BINARY_PATCH)
    assert "diff --git" not in scrubbed
    assert "GIT binary patch" not in scrubbed
    assert "_old_blob_secret" not in scrubbed
    assert "_new_blob_secret" not in scrubbed
    assert "tweaks an icon" in scrubbed.lower()


def test_scrub_task_text_strips_two_file_binary_diff_with_internal_blank_lines():
    """Two files, each a two-hunk binary patch (multiple internal blank
    separators plus a second `diff --git ` header re-arming for the second
    file) — zero base85/literal leakage across both files."""
    scrubbed = scrub_task_text(_PR_BODY_WITH_TWO_FILE_BINARY_DIFF)
    assert "diff --git" not in scrubbed
    assert "GIT binary patch" not in scrubbed
    assert "_logo_a_secret" not in scrubbed
    assert "_logo_b_secret" not in scrubbed
    assert "updates two logo assets" in scrubbed.lower()
