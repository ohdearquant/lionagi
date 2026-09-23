"""Lint the ADR corpus under docs/adr against the record standard.

Checks, per file: the frontmatter fields and their vocabularies; the file lives in a topic folder and its
`area` equals the folder's suffix; the record number matches the file name; the section order; every
claim and decision heading carries its id anchor; a mechanical claim carries Subject and Violated-when;
every decision carries Landing evidence; an optional Definitions section after Context holds only
`- **term**: ...` bullets; every wikilink resolves to a file and, when it names an anchor, to an item; no
em dash; and the prose limits of the register (Context 200 words, definition 50, assumption 50, claim
160, decision 90 plus 40 of evidence, paragraph 90, sentence 45, table cell 45). Trigger is legacy, merged
into Context, and flagged. Format: prose lines wrap at 100 columns; a definition ends with a period, is used in
its own record outside the Definitions section, and no term is defined by two records; Context ends with
a `Source:` sentence; a consequence bullet is `- **S1**: ...`.

    uv run python scripts/adr_lint.py [files...]      # no files: the whole corpus
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parent.parent
)  # docs/adr: the scripts live inside the corpus they lint
STATUS = {
    "draft",
    "proposed",
    "signed",
    "superseded",
    "withdrawn",
    "rejected",
    "abandoned",
}
LIVENESS = {"unimplemented", "partial", "operating", "retired"}
KIND = {
    "new",
    "supersede",
    "extends",
    "edit",
    "correct",
    "withdraw",
    "backfill",
    "abandon",
}
ENFORCED = {"mechanical", "scheduled", "process", "none"}
SECTIONS = [
    "Trigger",
    "Context",
    "Definitions",
    "Assumptions",
    "Claims",
    "Decisions",
    "Alternatives",
    "Consequences",
]
OPTIONAL = {
    "Trigger",
    "Definitions",
}  # Definitions: a record with no terms of its own omits it
LEGACY = {"Trigger"}  # merged into Context; a record that still carries it is flagged
LIMITS = {
    "Trigger": 80,
    "Context": 200,
    "definition": 50,
    "assumption": 50,
    "claim": 160,
    "decision": 90,
    "evidence": 40,
}
_DEFN = re.compile(r"^- \*\*([^*]+)\*\*: (.+)$", re.M)
PARA, SENT, CELL = 90, 45, 45

# a link inside a table cell escapes its pipe as `\|`; the backslash is not part of the stem or anchor
_LINK = re.compile(r"\[\[([^\]|#\\]+)(?:#([^\]|\\]+))?\\?(?:\|([^\]]+))?\]\]")
_ITEM = re.compile(
    r"^### ([CD])(\d+): (.+?)(?: _\(enforced: ([a-z]+)\)_)? \^([cd]\d+)\s*$"
)


def words(s: str) -> int:
    return len(s.split())


def frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    fm: dict[str, object] = {}
    key = None
    for line in head.splitlines():
        if line.startswith("  - "):
            fm.setdefault(key, []).append(line[4:].strip().strip('"'))  # type: ignore[union-attr]
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            fm[key] = [] if value == "[]" else (value.strip('"') if value else [])
    return fm, body


def lint(path: Path, corpus: dict[str, Path]) -> list[str]:
    out: list[str] = []
    text = path.read_text()
    if "\u2014" in text:
        out.append("em dash")
    fm, body = frontmatter(text)
    if not fm:
        return ["no frontmatter"]
    for field in (
        "adr",
        "status",
        "liveness",
        "date",
        "area",
        "kind",
        "depends_on",
        "tags",
    ):
        if field not in fm:
            out.append(f"frontmatter lacks {field}")
    adr = str(fm.get("adr", ""))
    if not path.stem.startswith(adr + "-"):
        out.append(f"file name does not start with {adr}")
    if path.parent.parent != ROOT or not re.match(
        r"^\d{3}-[a-z0-9-]+$", path.parent.name
    ):
        out.append("not in a topic folder NNN-<topic>")
    elif fm.get("area") != path.parent.name[4:]:
        out.append(
            f"area {fm.get('area')!r} is not the folder suffix {path.parent.name[4:]!r}"
        )
    if fm.get("status") not in STATUS:
        out.append(f"status {fm.get('status')!r}")
    liveness = str(fm.get("liveness", "")).split(" (")[0]
    if liveness not in LIVENESS:
        out.append(f"liveness {fm.get('liveness')!r}")
    if fm.get("kind") not in KIND:
        out.append(f"kind {fm.get('kind')!r}")
    for dep in fm.get("depends_on") or []:
        if not _LINK.fullmatch(dep):
            out.append(f"depends_on entry is not a wikilink: {dep}")

    if not re.search(rf"^# {re.escape(adr)}: ", body, re.M):
        out.append(f"title line is not '# {adr}: ...'")
    seen = [m.group(1) for m in re.finditer(r"^## (.+?)\s*$", body, re.M)]
    expected = [s for s in SECTIONS if s not in OPTIONAL or s in seen]
    if fm.get("status") != "superseded" and seen != expected:
        out.append(f"sections {seen} are not {expected}")
    if LEGACY & set(seen):
        out.append(f"{sorted(LEGACY & set(seen))} merged into Context in this corpus")

    # items
    anchors: set[str] = set()
    ids: dict[str, list[int]] = {"C": [], "D": []}
    section = None
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section == "Definitions" and line.strip() and not line.startswith("  "):
            if not _DEFN.match(line):
                out.append(
                    f"line {i + 1}: a definition is '- **term**: ...', nothing else in the section"
                )
            continue
        if line.startswith("### ") and section in ("Claims", "Decisions"):
            m = _ITEM.match(line)
            if not m:
                out.append(
                    f"line {i + 1}: item heading not '### C1: name _(enforced: x)_ ^c1'"
                )
                continue
            letter, num, _name, enforced, anchor = m.groups()
            if anchor != f"{letter.lower()}{num}":
                out.append(
                    f"line {i + 1}: anchor ^{anchor} does not match {letter}{num}"
                )
            anchors.add(anchor)
            ids[letter].append(int(num))
            block = "\n".join(lines[i + 1 : next_heading(lines, i + 1)])
            if letter == "C":
                if enforced not in ENFORCED:
                    out.append(f"{letter}{num}: enforced {enforced!r}")
                if enforced == "mechanical" and (
                    "- **Subject**:" not in block or "- **Violated when**:" not in block
                ):
                    out.append(
                        f"C{num}: mechanical claim lacks Subject or Violated when"
                    )
            else:
                if enforced is not None:
                    out.append(f"D{num}: a decision carries no enforcement marker")
                if "- **Landing evidence**:" not in block:
                    out.append(f"D{num}: lacks Landing evidence")
                if not re.search(r"\bC\d+\b", block):
                    out.append(f"D{num}: names no served claim")
    for letter, nums in ids.items():
        if nums != list(range(1, len(nums) + 1)):
            out.append(f"{letter} ids {nums} are not 1..n")

    # links
    for m in _LINK.finditer(body):
        stem, anchor, alias = m.groups()
        target = corpus.get(stem)
        if target is None:
            out.append(f"link to unknown record {stem}")
            continue
        if anchor and anchor != "Assumptions":
            if not anchor.startswith("^"):
                out.append(f"link anchor {anchor!r} is not an item anchor")
            elif f" {anchor}" not in target.read_text():
                out.append(f"link {stem}#{anchor} names no item")
        if alias and not alias.startswith(
            stem.split("-", 2)[0] + "-" + stem.split("-", 2)[1]
        ):
            out.append(f"link alias {alias!r} does not name {stem}")

    out.extend(prose(body, fm))
    out.extend(fresh_format(body))
    return out


WIDTH = 100


def fresh_format(body: str) -> list[str]:
    out: list[str] = []
    lines = body.splitlines()
    section = None
    in_code = False
    defined: list[tuple[int, str]] = []
    rest: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if len(line) > WIDTH and not line.startswith(("|", "#")) and "[[" not in line:
            out.append(f"line {i + 1}: {len(line)} columns > {WIDTH}")
        if section == "Definitions":
            m = _DEFN.match(line)
            if m:
                defined.append((i + 1, m.group(1)))
            if line.strip() and not line.rstrip().endswith((".", ")")):
                if not (i + 1 < len(lines) and lines[i + 1].startswith("  ")):
                    out.append(f"line {i + 1}: definition does not end with a period")
        else:
            rest.append(line)
        if (
            section == "Consequences"
            and line.startswith("- ")
            and not re.match(r"^- \*\*S\d+\*\*: ", line)
        ):
            out.append(f"line {i + 1}: consequence bullet is not '- **S1**: ...'")
    text = "\n".join(rest).lower()
    for n, term in defined:
        bare = term.replace("`", "").lower()
        if bare not in text:
            out.append(f"line {n}: defined term {term!r} is not used in the record")
    ctx = re.search(r"## Context\n(.*?)\n## ", body, re.S)
    if ctx and "Source:" not in ctx.group(1):
        out.append("Context has no Source: sentence")
    return out


def next_heading(lines: list[str], start: int) -> int:
    for j in range(start, len(lines)):
        if lines[j].startswith("#"):
            return j
    return len(lines)


def prose(body: str, fm: dict[str, object]) -> list[str]:
    out: list[str] = []
    if fm.get("status") == "superseded":
        return out  # the body is kept as written; only the marker was added
    body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    section = key = None
    units: dict[str, list[str]] = {}
    para: list[str] = []

    def flush() -> None:
        if para and key is not None:
            units.setdefault(key, []).append(" ".join(para))
        para.clear()

    for line in body.splitlines():
        if line.startswith("## "):
            flush()
            section = line[3:].strip()
            key = section
            continue
        if line.startswith("### "):
            flush()
            key = f"{section} / {line[4:].strip().split(':')[0]}"
            continue
        if line.startswith("|"):
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if section == "Assumptions" and cells and re.fullmatch(r"A\d+", cells[0]):
                if words(cells[1]) > LIMITS["assumption"]:
                    out.append(
                        f"{cells[0]} statement {words(cells[1])} words > {LIMITS['assumption']}"
                    )
                continue
            for c in cells:
                if words(c) > CELL:
                    out.append(f"table cell {words(c)} words > {CELL}: {c[:50]!r}")
            continue
        if line.startswith("> "):
            continue
        if not line.strip():
            flush()
            continue
        if line.startswith("- "):  # a bullet is its own unit
            flush()
        para.append(line.strip())
    flush()

    for k, paras in units.items():
        evidence = [p for p in paras if p.startswith("- **Landing evidence**")]
        rest = [p for p in paras if not p.startswith("- **Landing evidence**")]
        total = sum(words(p) for p in rest)
        cap = LIMITS.get(k)
        if k in ("Trigger", "Context") and total > cap:
            out.append(f"{k} {total} words > {cap}")
        if k == "Definitions":
            for p in rest:
                if words(p) > LIMITS["definition"]:
                    out.append(
                        f"definition {words(p)} words > {LIMITS['definition']}: {p[:40]!r}"
                    )
        if " / C" in k and total > LIMITS["claim"]:
            out.append(f"{k} {total} words > {LIMITS['claim']}")
        if " / D" in k:
            if total > LIMITS["decision"]:
                out.append(f"{k} {total} words > {LIMITS['decision']}")
            for e in evidence:
                if words(e) - 3 > LIMITS["evidence"]:
                    out.append(
                        f"{k} landing evidence {words(e) - 3} words > {LIMITS['evidence']}"
                    )
        for p in rest:
            if words(p) > PARA:
                out.append(f"{k}: paragraph {words(p)} words > {PARA}: {p[:50]!r}")
            for s in re.split(r"(?<=[.!?])\s+(?=[A-Z`(\[])", p):
                if words(s) > SENT:
                    out.append(f"{k}: sentence {words(s)} words > {SENT}: {s[:50]!r}")
    return out


def main(argv: list[str]) -> int:
    files = [Path(a).resolve() for a in argv] or sorted(ROOT.glob("*/ADR-*.md"))
    corpus = {p.stem: p for p in ROOT.glob("*/ADR-*.md")}
    bad = 0
    if True:
        owners: dict[str, list[str]] = {}
        for p in corpus.values():
            sec = re.search(r"## Definitions\n(.*?)\n## ", p.read_text(), re.S)
            for m in _DEFN.finditer(sec.group(1) if sec else ""):
                owners.setdefault(m.group(1).replace("`", ""), []).append(p.stem)
        for term, who in sorted(owners.items()):
            if len(who) > 1:
                bad += 1
                print(f"{term!r} is defined by {who}; one record owns a term")
    for path in files:
        if path.name == "INDEX.md" or not path.name.startswith("ADR-"):
            continue
        for finding in lint(path, corpus):
            bad += 1
            print(f"{path.relative_to(ROOT.parent.parent)}: {finding}")
    if bad:
        print(f"adr_lint: {bad} finding(s)", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
