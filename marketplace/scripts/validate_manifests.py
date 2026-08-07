#!/usr/bin/env python3
"""Validate marketplace manifests — checks required fields, SKILL.md presence,
per-plugin plugin.json files, duplicate names/sources, and stub mcpServer entries."""

import json
import sys
from pathlib import Path

PLUGIN_REQUIRED = ["name", "source", "description"]
TOP_REQUIRED = ["name", "version", "description"]
PER_PLUGIN_REQUIRED = ["name", "version", "description"]
PER_PLUGIN_STRING_FIELDS = ["name", "version", "description"]
PER_PLUGIN_OPTIONAL_STRINGS = ["repository", "license", "homepage"]


# Plain scalars spelled with letters that a YAML parser still resolves to something
# other than a string. Compared case-insensitively, and measured rather than assumed:
# `y` and `n` are NOT here, because a parser resolves those to the strings "y" and "n".
# Do not add them.
_NOT_A_STRING = frozenset({"null", "true", "false", "yes", "no", "on", "off"})


def _yaml_line_safe(text: str) -> bool:
    """Whether every character is one YAML allows inside a line.

    A character outside this set makes the whole document unreadable, so accepting one
    would report a description while the host cannot load the file at all. The excluded
    ranges were measured code point by code point against a real parser, not reasoned
    about, and two of the results are counterintuitive enough to state:

    - The C1 range is excluded along with C0. It is not only the obvious control
      characters: a bare tab, a vertical tab, a form feed, NUL, escape and the C1 block
      up to 0x9f all make the document invalid.
    - Characters that merely *look* unprintable are fine. A non-breaking space, a
      zero-width space, a byte-order mark, accented and CJK letters and emoji are all
      accepted by a parser, so they are accepted here. Do not reach for
      ``str.isprintable()`` as a shortcut — it rejects the first three and would turn
      legitimate descriptions into failures.
    """
    return not any(
        code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029) for code in map(ord, text)
    )


def _nests_a_mapping(head: str) -> bool:
    """Whether a colon in this text makes the line parse as a nested mapping.

    A colon followed by whitespace, or ending the line, opens a nested mapping, and a
    mapping cannot open on a line that is already a mapping value. The document is then
    rejected outright rather than merely resolving oddly. A colon *not* followed by
    whitespace is ordinary text, which is what keeps ``ratio 1:2`` and ``12:30`` usable.
    """
    return any(
        char == ":" and (index + 1 == len(head) or head[index + 1].isspace())
        for index, char in enumerate(head)
    )


# Characters that, when they open a value, begin something other than a plain scalar:
# flow collections, a comment, an anchor, an alias, a tag, a block scalar, either quote
# style, and the two reserved indicators. Resolving any of them needs the parser this
# file cannot import, so a value opening with one is refused rather than guessed at.
_OPENS_NON_SCALAR = frozenset(",[]{}#&*!|>'\"%@`")


def _value_stays_on_its_line(value: str) -> bool:
    """Whether a frontmatter value provably cannot restructure the document.

    Applied to every entry other than the description, which gets a stricter test. The
    point is narrow: not "is this value sensible" but "can this value make the file
    unparseable". A sibling entry is allowed to be empty, a number, a boolean or null,
    since none of those affects whether the description is readable.

    Each rejection below was measured as a document a parser refuses while the
    description entry itself is perfectly well formed: ``name: a: b`` and ``name: text:``
    nest a mapping, ``name: *x`` aliases an anchor that does not exist, and
    ``name: "unterminated``, ``name: {a: b`` and ``name: [a`` never close. Forms that are
    legal and are refused anyway, such as a quoted value containing a colon or an inline
    flow mapping, are recorded as narrowings in the surface suite.

    Trimming here is safe, unlike in the description path: the caller has already tested
    the whole raw line for characters YAML does not allow, so no character this could
    remove is one anything downstream still needs to see.
    """
    head = value.strip()
    if not head:
        return True
    if head[0] in _OPENS_NON_SCALAR:
        return False
    if head[0] in "-?:" and (len(head) == 1 or head[1].isspace()):
        return False
    return not _nests_a_mapping(head)


def _describes_a_nonempty_string(value: str) -> bool:
    """Whether a plain scalar is certainly a non-empty string to a YAML parser.

    Deliberately a whitelist, not an attempt at parser equivalence. Each clause below
    exists because a sweep over generated values found documents it would otherwise
    have let through, so none of them is precautionary:

    - **Begins with a letter.** Rules out every YAML indicator character in one test,
      and so every number, timestamp, quoted form, block or folded scalar, flow
      collection, anchor, alias, tag and comment.
    - **Every character is one YAML permits inside a line.** A character outside that
      set makes the *document* invalid, not merely the value odd. The excluded set was
      measured code point by code point rather than guessed; see ``_yaml_line_safe``.
    - **No colon followed by whitespace, and none at the end.** That spelling makes the
      line parse as a nested mapping, so the document is rejected outright. A colon
      *not* followed by whitespace is fine, which keeps ``ratio 1:2`` usable.
    - **Not a boolean or null word.** The remaining way a letter-initial scalar avoids
      being a string. The set is closed and was measured: ``y`` and ``n`` are strings.

    A trailing ``#`` comment is dropped before the letter and word tests, since a parser
    drops it and keeps the text before it. That matters in both directions: ``text #
    note`` is the string "text", while ``yes # note`` is still a boolean.
    """
    # The character test must see the raw text. Trimming first would remove the very
    # characters being looked for, so this function takes everything after the colon
    # exactly as written and does its own trimming below.
    if not _yaml_line_safe(value):
        return False
    head = value.split(" #", 1)[0].strip()
    if not head or not head[0].isalpha():
        return False
    if _nests_a_mapping(head):
        return False
    return head.lower() not in _NOT_A_STRING


def _has_frontmatter_description(path: Path) -> bool:
    """True when the file opens with frontmatter carrying a non-empty description.

    No YAML parser is used, and that is forced rather than preferred: ci.sh runs this
    file on the bare system interpreter, never through uv, so nothing outside the
    standard library is available and an import would turn a lint into a crash.

    The grammar is therefore hand-rolled, and it is narrower than YAML on purpose. It
    is pinned in the marketplace surface suite against a real parser, over a generated
    corpus rather than a list of remembered cases, with one invariant: it must never
    accept a document a parser would reject. Forms it rejects despite being legal YAML
    are recorded there with a category. Two rules worth stating here because both are
    tempting to "fix":

    - A fence is ``---`` alone on its line, optionally followed by spaces. Trailing
      spaces are legal and accepted; a trailing tab is not legal and is rejected. Do
      not tighten this to ``line == "---"`` — that rejects a fence a parser accepts.
    - The colon after ``description`` must be followed by a space or end the line.
      ``description:value`` is a plain scalar, not a mapping, and must not count.
    """
    return not _frontmatter_problem(path)


def _frontmatter_problem(path: Path) -> str:
    """Empty when the file's frontmatter carries a usable description, else why not."""
    try:
        text = path.read_text()
    except OSError as error:
        return f"could not be read ({error.strerror or error})"
    except UnicodeDecodeError:
        # A decode error is a ValueError rather than an OSError, so leaving it uncaught
        # would end the run with a traceback instead of a reported failure.
        return "is not valid UTF-8"
    return _frontmatter_description_problem(text)


def _mcp_servers_gate(mcp: object) -> tuple[dict[str, dict], list[str]]:
    """Validate a plugin.json 'mcpServers' block without ever raising.

    The Claude Code plugin schema documents 'mcpServers' as a string (a path to
    an external MCP config file), an array of those, or an inline object
    mapping server names to their configuration — this validator used to accept
    only the object form, refusing valid plugins that used the other two. Only
    the object form has entries this validator can inspect further (e.g. for
    the stub check below), so a string or an array passes the type gate with
    nothing left to check; an array element that is not a string is reported
    the same way a malformed object entry is.

    Returns the entries safe to iterate further (e.g. for the stub check) plus a
    list of problem descriptions. A malformed top-level block, or an entry whose
    value is not itself a non-empty object, is reported and dropped from the
    returned dict — never left in it — so a caller can keep going without
    calling a dict method on something that turned out not to be one. An empty
    inline entry (``{}``) is rejected to match a behavioral divergence observed
    against the upstream `claude plugin validate` command (Claude Code
    2.1.224): this validator used to accept it silently where that command
    refuses it with ``mcpServers: Invalid input``. The captured command
    output backing that observation is checked in at
    testdata/claude_plugin_validate_empty_mcp_server.txt (rejection) and
    testdata/claude_plugin_validate_nonempty_mcp_server.txt (a well-formed
    entry, for contrast) alongside this script, so the parity claim is
    checkable against a recorded run rather than resting on prose alone. Both
    the per-plugin and the standalone-scan branch share this gate, so a fix
    here fixes both at once and the two can never drift back apart.
    """
    if mcp is None:
        return {}, []
    if isinstance(mcp, str):
        return {}, []
    if isinstance(mcp, list):
        problems = [
            f"mcpServers[{index}] must be a string, got {type(item).__name__}"
            for index, item in enumerate(mcp)
            if not isinstance(item, str)
        ]
        return {}, problems
    if not isinstance(mcp, dict):
        return {}, [
            f"'mcpServers' must be a string, an array, or an object, got {type(mcp).__name__}"
        ]
    usable: dict[str, dict] = {}
    problems: list[str] = []
    for server_name, server_cfg in mcp.items():
        if not isinstance(server_cfg, dict):
            problems.append(
                f"mcpServers['{server_name}'] must be an object, got {type(server_cfg).__name__}"
            )
            continue
        if not server_cfg:
            problems.append(f"mcpServers['{server_name}'] must not be empty")
            continue
        usable[server_name] = server_cfg
    return usable, problems


def _is_plain_top_level_key(key: str) -> bool:
    """Whether a key is one this validator can read without a parser."""
    return bool(key) and key[0].isalpha() and all(c.isalnum() or c in "_-" for c in key)


def _frontmatter_description_problem(text: str) -> str:
    """Empty when the frontmatter carries a description this validator can vouch for.

    Otherwise a short phrase naming what is wrong, so a caller can report the actual
    problem rather than always claiming the description is missing.

    Every line of the block is classified, not just the description. A block whose
    *other* lines are malformed is one a host cannot load at all, so returning early on
    finding the description would vouch for a file that does not work. That is a real
    case rather than a hypothetical: a tab-indented line, an over-indented line and a
    bare scalar line each make the document invalid while leaving the description entry
    itself perfectly well formed.

    The cost is that nested frontmatter is rejected, since resolving it needs the parser
    this cannot import. The message says so, which is the point of returning one.
    """

    def is_fence(line: str) -> bool:
        return line.rstrip(" ") == "---"

    # Line breaks are normalised to newlines and then split on, rather than handed to
    # str.splitlines(). The three sequences below are exactly the ones a YAML parser treats
    # as a line break, and CR alone is one of them: a CR-only file is valid and its
    # frontmatter loads, so splitting on "\n" alone never found the opening fence and
    # rejected a file the host reads. str.splitlines() would fix that and break more,
    # because it ALSO breaks on vertical tab, form feed, the file/group/record separators,
    # NEL and the Unicode line and paragraph separators — none of which a parser accepts
    # here, so splitting on them moves an illegal character out of a value and past the
    # character check. That is how such characters got through before, so the set stays
    # explicit. Note the ordering: CRLF must collapse before a lone CR, or one break
    # becomes two. A CR *inside* a value is still refused, because normalising it leaves a
    # bare scalar on its own line and the block walk rejects that.
    # Exactly ONE leading byte-order mark is dropped, not every one of them. Editors add
    # a mark, a parser tolerates it at the start of a stream, and read_text() with the
    # default encoding leaves it in place, so keeping it would fail such a file for a
    # reason its author cannot see. But the tolerance is for one mark at the stream start
    # only: a second mark is content sitting before the document marker and the parser
    # refuses the file, so removing every leading mark would vouch for a file no host can
    # load. removeprefix drops at most one; lstrip drops all of them. It is spelled as an
    # escape rather than pasted in literally, because a literal mark is invisible in every
    # editor that shows this file.
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or not is_fence(lines[0]):
        return "no opening '---' frontmatter fence"
    close = next((i for i, line in enumerate(lines[1:], 1) if is_fence(line)), None)
    if close is None:
        return "no closing '---' frontmatter fence"

    description = None
    for line in lines[1:close]:
        if not line.strip() or line.startswith("#"):
            continue
        if not _yaml_line_safe(line):
            return "frontmatter has a character YAML does not allow inside a line"
        key, separator, rest = line.partition(":")
        if not separator or not _is_plain_top_level_key(key) or rest[:1] not in ("", " "):
            return f"frontmatter line is not a plain top-level key: {line.strip()[:40]!r}"
        if key == "description":
            description = rest
        elif not _value_stays_on_its_line(rest):
            # A sibling entry can break the file on its own, and it does so while the
            # description entry stays well formed. Classifying only the keys left seven
            # such spellings passing, so the value is classified too.
            return f"frontmatter line needs a YAML parser to read: {line.strip()[:40]!r}"
    if description is None:
        return "no top-level 'description' key"
    if not _describes_a_nonempty_string(description):
        return "'description' is not a non-empty single-line scalar"
    return ""


def _frontmatter_description_ok(text: str) -> bool:
    """The grammar as a boolean, over text rather than a file.

    Split out from the file-reading wrapper so the surface suite can sweep it over
    generated values without writing a file per candidate. That is what makes a corpus
    of tens of thousands of documents cheap enough to be worth having.
    """
    return not _frontmatter_description_problem(text)


def main(repo_root: Path | None = None) -> int:
    if repo_root is None:
        repo_root = Path(__file__).parent.parent.parent
    manifest_path = repo_root / ".claude-plugin" / "marketplace.json"

    if not manifest_path.exists():
        print(f"FAIL: manifest not found at {manifest_path}")
        return 1

    with manifest_path.open() as f:
        manifest = json.load(f)

    failures = 0

    # Top-level required fields
    for field in TOP_REQUIRED:
        if field not in manifest:
            print(f"FAIL [manifest]: missing top-level field '{field}'")
            failures += 1

    plugins = manifest.get("plugins", [])
    if not isinstance(plugins, list):
        print("FAIL [manifest]: 'plugins' must be an array")
        return 1

    print(f"Checking {len(plugins)} plugin(s) in {manifest_path.relative_to(repo_root)}")

    seen_names: dict[str, int] = {}
    seen_sources: dict[str, int] = {}

    for idx, plugin in enumerate(plugins):
        name = plugin.get("name", "<unnamed>")
        plugin_ok = True

        for field in PLUGIN_REQUIRED:
            if field not in plugin:
                print(f"FAIL [{name}]: missing required field '{field}'")
                plugin_ok = False
                failures += 1

        # Duplicate name/source detection
        if name in seen_names:
            print(f"FAIL [{name}]: duplicate plugin name (also at index {seen_names[name]})")
            plugin_ok = False
            failures += 1
        else:
            seen_names[name] = idx

        source = plugin.get("source", "")
        if source:
            if source in seen_sources:
                print(
                    f"FAIL [{name}]: duplicate source '{source}' (also used by index {seen_sources[source]})"
                )
                plugin_ok = False
                failures += 1
            else:
                seen_sources[source] = idx

        if "source" in plugin:
            source_rel = plugin["source"].lstrip("./")
            source_dir = repo_root / source_rel
            if not source_dir.is_dir():
                print(f"FAIL [{name}]: source directory not found: {plugin['source']}")
                plugin_ok = False
                failures += 1
            else:
                skill_files = sorted(source_dir.rglob("SKILL.md"))
                if not skill_files:
                    print(f"FAIL [{name}]: no SKILL.md found under {plugin['source']}")
                    plugin_ok = False
                    failures += 1
                else:
                    for sf in skill_files:
                        rel = sf.relative_to(repo_root)
                        if not sf.is_file():
                            print(f"FAIL [{name}]: SKILL.md not a file: {rel}")
                            plugin_ok = False
                            failures += 1

                # Agent markdown must be a direct child of agents/ and carry a
                # description. Plugin hosts walk nested agents/<subdir>/*.md as
                # agents too, so support docs kept there surface as malformed
                # agents; the description is what makes an agent selectable by
                # intent rather than merely installed.
                agents_dir = source_dir / "agents"
                if agents_dir.is_dir():
                    for md in sorted(agents_dir.rglob("*.md")):
                        rel_md = md.relative_to(repo_root)
                        if md.parent != agents_dir:
                            print(
                                f"FAIL [{name}]: agent markdown must be a direct child of "
                                f"agents/: {rel_md}"
                            )
                            plugin_ok = False
                            failures += 1
                        else:
                            problem = _frontmatter_problem(md)
                            if problem:
                                print(f"FAIL [{name}]: agent {rel_md}: {problem}")
                                plugin_ok = False
                                failures += 1

                # Per-plugin plugin.json validation
                per_plugin_json = source_dir / ".claude-plugin" / "plugin.json"
                if not per_plugin_json.exists():
                    print(
                        f"FAIL [{name}]: Listed source '{plugin['source']}' has no .claude-plugin/plugin.json"
                    )
                    plugin_ok = False
                    failures += 1
                else:
                    with per_plugin_json.open() as pf:
                        per_plugin = json.load(pf)
                    for field in PER_PLUGIN_REQUIRED:
                        if field not in per_plugin:
                            print(f"FAIL [{name}]: plugin.json missing required field '{field}'")
                            plugin_ok = False
                            failures += 1
                    for field in PER_PLUGIN_STRING_FIELDS:
                        val = per_plugin.get(field)
                        if val is not None and not isinstance(val, str):
                            print(
                                f"FAIL [{name}]: plugin.json '{field}' must be a string, got {type(val).__name__}"
                            )
                            plugin_ok = False
                            failures += 1
                    for field in PER_PLUGIN_OPTIONAL_STRINGS:
                        val = per_plugin.get(field)
                        if val is not None and not isinstance(val, str):
                            print(
                                f"FAIL [{name}]: plugin.json '{field}' must be a string, got {type(val).__name__}"
                            )
                            plugin_ok = False
                            failures += 1
                    author = per_plugin.get("author")
                    if author is not None and not isinstance(author, dict):
                        print(
                            f"FAIL [{name}]: plugin.json 'author' must be an object, got {type(author).__name__}"
                        )
                        plugin_ok = False
                        failures += 1
                    # A malformed mcpServers block has to end in a reported failure and
                    # never a traceback, which means the type check must GATE the
                    # iteration rather than only report alongside it. The same applies
                    # one level down, to each entry inside the block.
                    mcp, mcp_problems = _mcp_servers_gate(per_plugin.get("mcpServers"))
                    for problem in mcp_problems:
                        print(f"FAIL [{name}]: plugin.json {problem}")
                        plugin_ok = False
                        failures += 1
                    for server_cfg in mcp.values():
                        if server_cfg.get("type") == "stub":
                            print(f"FAIL [{name}]: plugin.json contains stub mcpServers entry")
                            plugin_ok = False
                            failures += 1

        if plugin_ok:
            print(f"PASS [{name}]")

    # Also scan marketplace subdirs for plugin.json files not referenced by manifest
    marketplace_dir = repo_root / "marketplace"
    for pjson in sorted(marketplace_dir.glob("*/.claude-plugin/plugin.json")):
        with pjson.open() as f:
            pdata = json.load(f)
        pname = pdata.get("name", "<unnamed>")
        pversion = pdata.get("version")
        plugin_dir = pjson.parent.parent.name
        if pname not in seen_names:
            # Standalone plugin.json not in marketplace.json — validate it anyway
            ok = True
            for field in PER_PLUGIN_REQUIRED:
                if field not in pdata:
                    print(f"FAIL [standalone:{plugin_dir}]: plugin.json missing '{field}'")
                    failures += 1
                    ok = False
            mcp, mcp_problems = _mcp_servers_gate(pdata.get("mcpServers"))
            for problem in mcp_problems:
                print(f"FAIL [standalone:{plugin_dir}]: plugin.json {problem}")
                failures += 1
                ok = False
            for server_cfg in mcp.values():
                if server_cfg.get("type") == "stub":
                    print(
                        f"FAIL [standalone:{plugin_dir}]: plugin.json contains stub mcpServers entry"
                    )
                    failures += 1
                    ok = False
            if ok:
                print(f"PASS [standalone:{plugin_dir}] (version={pversion})")

    if failures == 0:
        print(f"\nAll {len(plugins)} plugin(s) passed.")
        return 0
    else:
        print(f"\n{failures} failure(s) found.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
