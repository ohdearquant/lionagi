#!/usr/bin/env python3
"""Content-level lint for marketplace .md files.

Catches Lion-internal leakage: khive paths, LION identity symbols,
deprecated verb syntax, nonexistent CLI commands, and stale model names.

Usage:
    uv run python marketplace/scripts/lint_skills.py [path ...]

Exit 0 if clean, exit 1 if any findings.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------

# (pattern, description)
FORBIDDEN_PATHS: list[tuple[str, str]] = [
    (r"\.khive/", "khive workspace path (.khive/)"),
    (r"khive-work/", "Lion-internal show directory (khive-work/)"),
    (r"\bfirm/", "private firm repo reference (firm/)"),
    (r"/Users/\w+/", "hardcoded home directory path (/Users/<name>/)"),
]

FORBIDDEN_SYMBOLS: list[tuple[str, str]] = [
    (r"∵α\[", "LION agent identity prefix (∵α[)"),
    (r"→LION\.", "LION affiliation marker (→LION.)"),
    (r"\bkpp\s+format", "internal .kpp format reference"),
    (r"\bplan\.kpp\b", "internal plan.kpp reference"),
]

DEPRECATED_PATTERNS: list[tuple[str, str]] = [
    (r"mcp__khive__\w+\s*\(action=", "deprecated service.action() dispatch (action= kwarg)"),
    (r"\bmemory\.recall\(", "bare Python method syntax (memory.recall())"),
    (r"\bwork\.tasks\(", "bare Python method syntax (work.tasks())"),
    (r"\bforget_batch\b", "nonexistent verb (forget_batch)"),
    (r"\bmcp__khive__graph\b", "nonexistent verb (mcp__khive__graph)"),
    (r"\bmcp__khive__waves\b", "nonexistent verb (mcp__khive__waves)"),
    (r"\bmcp__khive__work\b", "nonexistent verb (mcp__khive__work)"),
    (r"\bmcp__khive__communication\b", "nonexistent verb (mcp__khive__communication)"),
    # A plugin-provided MCP server's tools are namespaced
    # mcp__plugin_<plugin-name>_<server-name>__<tool>, never the bare mcp__<server>__
    # form. A bare reference names a tool that does not exist for a user who
    # installed this bundle as a plugin, even though it reads correctly and lints
    # clean otherwise. The negative lookahead is what keeps a correctly scoped name
    # (mcp__plugin_orchestrate_lion__request) out of this rule.
    (
        r"\bmcp__(?!plugin_)[a-z0-9_]+__",
        "unscoped MCP tool name (mcp__<server>__) — a plugin-provided server's tools "
        "are namespaced mcp__plugin_<plugin>_<server>__<tool>",
    ),
]

# Only matched in .md files (not plugin.json where Ocean is an author name)
OCEAN_PATTERNS: list[tuple[str, str]] = [
    (r"\bOcean\b", "Lion-internal person name (Ocean) in skill body"),
]

# Author attribution lines to skip for the Ocean check
_AUTHOR_SKIP_RE = re.compile(
    r"""(?xi)
    (
        author\s*:          |   # YAML author: Ocean
        "author"\s*:        |   # JSON "author": ...
        by\s+Ocean\b        |   # "by Ocean" attribution
        Ocean\s*\(he/       |   # bio pronoun context
        Ocean\s+Li\b        |   # full name
        created\s+by\s+Ocean |
        maintainer.*Ocean   |
        Ocean.*maintainer
    )
""",
    re.IGNORECASE,
)

NONEXISTENT_COMMANDS: list[tuple[str, str]] = [
    (r"li\s+o\s+flow\s+validate", "nonexistent subcommand (li o flow validate)"),
    (r"li\s+o\s+flow\s+run", "nonexistent subcommand (li o flow run)"),
    (r"\bnohup\s+li\s", "nohup li ... (use --background flag instead)"),
]

STALE_MODELS: list[tuple[str, str]] = [
    (r"\bcodex/gpt-5\.3-codex\b", "stale model name (codex/gpt-5.3-codex)"),
]

# ---------------------------------------------------------------------------
# Compiled rule sets
# ---------------------------------------------------------------------------

_RULE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("FORBIDDEN_PATH", FORBIDDEN_PATHS),
    ("LION_SYMBOL", FORBIDDEN_SYMBOLS),
    ("DEPRECATED_VERB", DEPRECATED_PATTERNS),
    ("INTERNAL_NAME", OCEAN_PATTERNS),
    ("NONEXISTENT_CMD", NONEXISTENT_COMMANDS),
    ("STALE_MODEL", STALE_MODELS),
]

_COMPILED: list[tuple[str, re.Pattern[str], str]] = []
for _category, _rules in _RULE_GROUPS:
    for _pat, _desc in _rules:
        _COMPILED.append((_category, re.compile(_pat), _desc))

# --yolo without --bypass: window size in lines
_YOLO_WINDOW = 3
_YOLO_RE = re.compile(r"li\s+\S*\s+.*--yolo|li\s+agent\b.*--yolo|li\s+play\b.*--yolo")
_BYPASS_RE = re.compile(r"--bypass")


# ---------------------------------------------------------------------------
# Dead source-path check
# ---------------------------------------------------------------------------

# A backticked token. This rule checks backticked references only, which is the
# convention these docs use for a source reference. It does NOT check paths
# written as bare prose. That is deliberate rather than complete coverage: the
# only bare path-shaped tokens in this bundle are illustrative example filenames,
# and matching them would turn every example into a finding.
_BACKTICKED = re.compile(r"`([^`\s]+)`")

# A trailing :123 line citation, which names a position rather than the file.
_LINE_SUFFIX = re.compile(r":\d+$")

# A fenced block delimiter. Paths inside fences are illustrative far more often
# than referential, so fences are skipped.
#
# The delimiter's CHARACTER AND LENGTH are both captured because a fence may be
# longer than three markers, and inside such a fence a shorter run is ordinary
# content that cannot close it. Toggling a flag on any three-marker line instead
# desynchronises from the document: the scanner leaves a block the document is
# still inside, skips real prose, and ends in the un-fenced state so the
# unterminated-fence diagnostic does not fire either.
_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")


def _indent_columns(whitespace: str) -> int:
    """Leading whitespace measured in columns, with tabs to four-column stops."""
    n = 0
    for ch in whitespace:
        n = n + (4 - n % 4) if ch == "\t" else n + 1
    return n


def _fence_parts(line: str) -> tuple[str, str] | None:
    """The delimiter run and its trailing text, or None if this is not a fence line.

    Four columns of indent begin an indented code block, so a line that deep can
    neither open a fence nor close one. Accepting it as a delimiter is a silent
    bypass rather than a cosmetic error: the indented line becomes the opener, the
    unindented prose after it is skipped as though it were fence content, and the
    next delimiter closes a block the document was never in. Nothing is reported,
    including the unterminated fence.

    Measured across this bundle before tightening: all 302 fence delimiters sit at
    column zero, and no line indented four or more columns carries a backticked
    source path. So the strict reading costs nothing here, and where it is wrong it
    reads real content as prose and reports too much rather than too little.
    """
    m = _FENCE.match(line)
    if m is None or _indent_columns(m.group(1)) > 3:
        return None
    return m.group(2), m.group(3)


def _opens_fence(delim: str, trailing: str) -> bool:
    """Whether a delimiter line actually opens a fence.

    A backtick fence's info string may not itself contain a backtick, so
    ``` python ``` is a paragraph rather than an opener. Accepting it as one puts
    the scanner inside a block the document is not in: the prose that follows is
    skipped, the next delimiter reads as its closer, and the file ends balanced,
    so neither a dead path nor an unterminated fence is reported. Tildes carry no
    such restriction.
    """
    return not (delim[0] == "`" and "`" in trailing)


# The source roots this bundle's documentation refers to.
#
# Stated explicitly and then ASSERTED to exist, rather than derived from whatever
# the checkout happens to contain. Deriving them looks more robust and is worse:
# in a tree missing one of these (a sparse checkout, an sdist), the derived set
# simply omits that root, every reference under it stops being recognised as a
# path, and the rule reports a clean pass. That is precisely the silent-match-
# nothing failure this check exists to prevent, so a missing root is an error
# here and a rename fails loudly instead of quietly.
DOC_SOURCE_ROOTS: tuple[str, ...] = (
    "lionagi/",
    "apps/",
    "tests/",
    "marketplace/",
    "docs/",
    "examples/",
)


def missing_source_roots(repo_root: Path) -> list[str]:
    """Return the declared source roots absent from this tree; empty means all present."""
    return [r for r in DOC_SOURCE_ROOTS if not (repo_root / r.rstrip("/")).is_dir()]


# Characters marking a token as a template, glob or shell expansion, not a
# literal path.
_PLACEHOLDER_CHARS = "$<>*{}"


def _looks_like_repo_path(token: str, prefixes: tuple[str, ...]) -> bool:
    if not token.startswith(prefixes):
        return False
    return not any(c in token for c in _PLACEHOLDER_CHARS)


def _undeclared_root(token: str, repo_root: Path, prefixes: tuple[str, ...]) -> str | None:
    """Name the root of a token that points into a real directory nobody declared.

    The declared roots protect only what is listed, so a reference under a
    top-level directory added later is not recognised as a path at all and its
    nonexistence reads as a pass. Asserting that the listed roots exist does not
    close that: it confirms the old names are still there, not that the list
    still covers the tree.

    The check is gated on the root EXISTING because that is what separates the
    rot case from ordinary prose. Measured against this bundle, every
    path-shaped token under an undeclared root named a directory that is not in
    the tree at all: a workspace the orchestrator creates at run time, a
    deliberately generic example, a bundle-relative link, a model spec whose
    version reads as a file extension. Flagging those is 17 false positives and
    no true ones. A token pointing into a directory that IS in the tree is the
    opposite: something the docs reference and nothing checks.

    Limit worth stating, and it is wider than a typo: any reference whose root is
    not in the tree stays unchecked, which covers a reference left behind by a
    root that was deleted or renamed as well as one that was never right. That is
    the price of not guessing which path-shaped strings in prose are meant to be
    repo paths, and it is the case a reader should not assume is covered.
    """
    if "/" not in token or "://" in token:
        return None
    if token[0] in "/~." or any(c in token for c in _PLACEHOLDER_CHARS):
        return None
    root = token.split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", root):
        return None
    if f"{root}/" in prefixes or not (repo_root / root).is_dir():
        return None
    return root


def _resolves(token: str, base: Path) -> bool:
    # Trailing sentence punctuation that ran into the reference. A closing paren
    # is NOT stripped and no split on "(" happens: a token here is backtick-
    # delimited and whitespace-free, so a parenthesis inside it belongs to the
    # pathname. The annotated form these docs use, `schema.sql` (`CREATE TABLE x`),
    # is two separate backtick spans and never arrives as one token. Treating
    # every paren as an annotation separator instead truncates a real path at its
    # first paren, and the surviving prefix is usually a directory that exists,
    # so a missing file resolves clean.
    cleaned = token.rstrip(".,;:")
    cleaned = _LINE_SUFFIX.sub("", cleaned)
    return bool(cleaned) and (base / cleaned).exists()


def scan_dead_paths(
    path: Path, repo_root: Path, prefixes: tuple[str, ...] = DOC_SOURCE_ROOTS
) -> list[str]:
    """Flag backticked repo-relative paths that do not resolve.

    These rot silently: a stale path sits in a table whose other rows are
    correct, so reading the table does not reveal it.

    Scope, so the rule is not mistaken for wider coverage than it has: backticked
    tokens outside fenced blocks. Under a declared source root, the path must
    resolve. Under a root that exists in the tree but was never declared, the
    reference is reported so the root gets added rather than staying invisible.
    A path written as bare prose, shown inside a fence, or naming a root that is
    not in the tree at all, is not checked.
    """
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"[ERROR] {path} — cannot read: {exc}"]

    # Split on newlines only. The read above is in universal-newlines mode, so CR
    # and CRLF have already become LF and nothing else here is a line ending.
    #
    # str.splitlines() would additionally break on \v \f \x1c \x1d \x1e \x85
    #  , none of which end a line in Markdown, and it breaks by REMOVING the
    # character. A fragment the indent rule would have rejected for its leading
    # character therefore arrives at column zero instead. That is a silent bypass
    # rather than a cosmetic difference: the fragment becomes a delimiter, a real
    # reference after it is skipped as fence content, and a second such fragment
    # closes the block, so nothing is reported at all.
    lines = text.split("\n")

    opener = ""  # the delimiter run that opened the current fence; "" when outside one
    fence_opened_at = 0
    for lineno, line in enumerate(lines, start=1):
        parts = _fence_parts(line)
        if parts:
            delim, trailing = parts
            if not opener:
                if _opens_fence(delim, trailing):
                    opener = delim
                    fence_opened_at = lineno
                    continue
                # Not an opener after all, so fall through and read it as prose.
            # A closer must use the opener's own character, run at least as long,
            # and carry nothing but whitespace after it. Anything shorter or
            # different is content inside the block, not the end of it.
            elif delim[0] == opener[0] and len(delim) >= len(opener) and not trailing.strip():
                opener = ""
                fence_opened_at = 0
                continue
        if opener:
            continue
        for token in _BACKTICKED.findall(line):
            if _looks_like_repo_path(token, prefixes):
                if not _resolves(token, repo_root):
                    findings.append(
                        f"[DEAD_PATH] {path}:{lineno} — `{token}` does not exist in the repo"
                    )
            elif root := _undeclared_root(token, repo_root, prefixes):
                findings.append(
                    f"[UNDECLARED_ROOT] {path}:{lineno} — `{token}` points into `{root}/`, "
                    "a directory in this tree that is not in DOC_SOURCE_ROOTS, so nothing "
                    "checks whether it resolves. Add the root to the list."
                )

    # Ending inside a fence means every line after it was skipped. That is lost
    # coverage, and staying quiet about it would reproduce, one level down, the
    # silent-match-nothing failure this check exists to prevent.
    if opener:
        findings.append(
            f"[UNTERMINATED_FENCE] {path}:{fence_opened_at} — fence opened here is never "
            "closed, so path checks were skipped for the rest of the file"
        )
    return findings


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------


def _check_ocean_line(line: str) -> bool:
    """Return True if the line should be flagged for the Ocean check."""
    if not re.search(r"\bOcean\b", line):
        return False
    # Skip attribution contexts
    if _AUTHOR_SKIP_RE.search(line):
        return False
    return True


def scan_file(path: Path) -> list[str]:
    """Return a list of finding strings for one file."""
    findings: list[str] = []
    is_json = path.suffix == ".json"
    is_md = path.suffix == ".md"

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"[ERROR] {path} — cannot read: {exc}"]

    lines = text.splitlines()

    for lineno, line in enumerate(lines, start=1):
        for category, pattern, desc in _COMPILED:
            # Ocean check: skip in JSON files (author attribution in plugin.json)
            if category == "INTERNAL_NAME" and is_json:
                continue
            if category == "INTERNAL_NAME" and is_md:
                if not _check_ocean_line(line):
                    continue

            if pattern.search(line):
                findings.append(f"[{category}] {path}:{lineno} — {desc}")

    # --yolo without --bypass check: only in .md files
    if is_md:
        for lineno, line in enumerate(lines, start=1):
            if not _YOLO_RE.search(line):
                continue
            if _BYPASS_RE.search(line):
                continue
            # Check the window: current line + next _YOLO_WINDOW lines
            window = lines[lineno - 1 : lineno - 1 + _YOLO_WINDOW + 1]
            window_text = "\n".join(window)
            if not _BYPASS_RE.search(window_text):
                findings.append(
                    f"[YOLO_NO_BYPASS] {path}:{lineno} — --yolo without --bypass"
                    f" in next {_YOLO_WINDOW} lines"
                )

    return findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def collect_md_files(roots: list[Path]) -> list[Path]:
    """Recursively collect .md files from the given root directories/files."""
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            if root.suffix in (".md", ".json"):
                files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.md")))
            # plugin.json for INTERNAL_NAME check (but Ocean check skips JSON)
            files.extend(sorted(root.rglob("plugin.json")))
    return sorted(set(files))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    repo_root = Path(__file__).parent.parent.parent

    if argv:
        scan_roots = [Path(a) for a in argv]
    else:
        scan_roots = [repo_root / "marketplace"]

    files = collect_md_files(scan_roots)

    if not files:
        # Resolving nothing is an instrument defect, not a clean bill of health:
        # a pass here is indistinguishable from a scan that never looked.
        print(f"lint_skills: ERROR — no .md files found under {[str(r) for r in scan_roots]}")
        return 1

    missing = missing_source_roots(repo_root)
    if missing:
        print(
            f"lint_skills: ERROR — declared source root(s) {missing} absent under {repo_root}. "
            "Path checks would silently skip every reference under them, so this is an error "
            "rather than a pass. If a directory was renamed, update DOC_SOURCE_ROOTS."
        )
        return 1
    prefixes = DOC_SOURCE_ROOTS

    all_findings: list[str] = []
    for f in files:
        # Make paths relative to repo root for cleaner output
        try:
            display_path = f.relative_to(repo_root)
        except ValueError:
            display_path = f
        file_findings = scan_file(f)
        if f.suffix == ".md":
            file_findings.extend(scan_dead_paths(f, repo_root, prefixes))
        # Replace absolute path with relative in finding strings
        file_findings = [finding.replace(str(f), str(display_path)) for finding in file_findings]
        all_findings.extend(file_findings)

    if all_findings:
        for finding in all_findings:
            print(finding)
        print(f"\nlint_skills: {len(all_findings)} finding(s) in {len(files)} file(s)")
        return 1

    print(f"lint_skills: PASS — {len(files)} file(s) scanned, no issues found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
