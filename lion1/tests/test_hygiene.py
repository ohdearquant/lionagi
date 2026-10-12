"""Tracked files carry no temporary audit markers; the detector reports each marker's file and line."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = re.compile(
    r"review\)|round-[0-9]|\bP[0-9]-[0-9]+\b|\bF[0-9]{2}:|tests-quality|\bported from"
    r"|night review|apps review"
)


def labelled(root: Path, skip: frozenset[str] = frozenset()) -> tuple[int, list[str]]:
    """How many tracked files `git ls-files` names under `root`, and every line of them with a label."""
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    names = [n for n in out.decode().split("\0") if n and n not in skip]
    hits = []
    for name in names:
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        hits += [f"{name}:{i}: {s.strip()}" for i, s in enumerate(text.splitlines(), 1) if LABELS.search(s)]
    return len(names), hits


def test_a_labelled_line_in_a_tracked_file_is_found(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    lines = [
        "# P1-2 (hub review): the explanation",
        "# F08: the explanation",
        "# ported from another file",
        "round-3 said so",
        "(night review, a date)",
        "(apps review, a date)",
        "the explanation alone",
        "a name exported from the package",
    ]
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "untracked.py").write_text(lines[0] + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    count, hits = labelled(tmp_path)
    assert count == 1 and [h.split(":")[1] for h in hits] == ["1", "2", "3", "4", "5", "6"]


def test_no_tracked_file_carries_a_review_label():
    me = Path(__file__).resolve().relative_to(ROOT).as_posix()  # its marker examples exercise the detector
    count, hits = labelled(ROOT, frozenset({me}))
    assert count > 50, count  # the listing read the tree
    assert hits == []
