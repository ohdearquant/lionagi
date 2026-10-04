"""A ```mermaid block in the model's turn prints as code, and a page beside the chat log draws it: one
file per block, named for the chat, the entry and the block's place in it, linked from the console."""

from lion_cli.view import Console, diagram_page, mermaid_blocks
from rich.markdown import Markdown

from lion1.record import Entry, Kind

FLOW = "graph TD\n  A-->B"
SEQUENCE = "sequenceDiagram\n  a->>b: hi"
TURN = f"Here is the flow.\n\n```mermaid\n{FLOW}\n```\n\nand the exchange:\n\n```mermaid\n{SEQUENCE}\n```\n"


def test_mermaid_blocks_finds_each_block_in_order_and_nothing_else():
    text = (
        "A mermaid diagram would help here, written as ```mermaid in prose.\n\n"
        f"```python\nprint('mermaid')\n```\n\n{TURN}\n"
        "```mermaidx\ngraph LR\n  x-->y\n```\n\n"
        # the inner ``` is the outer fence's text: closing there would swallow the block after it
        "````markdown\n```mermaid\ngraph LR\n  inside-->a-longer-fence\n```\n````\n\n"
        "~~~mermaid theme\ngraph LR\n  t-->u\n~~~\n\n"
        "```mermaid\ngraph LR\n  never-->closed\n"
    )
    assert mermaid_blocks(text) == [FLOW, SEQUENCE, "graph LR\n  t-->u"]
    assert mermaid_blocks("no fences at all") == []


def test_diagram_page_escapes_the_source_and_loads_mermaid_as_a_module():
    page = diagram_page('graph TD\n  A["<b>x</b> & y"]-->B')
    shown = "graph TD\n  A[&quot;&lt;b&gt;x&lt;/b&gt; &amp; y&quot;]--&gt;B"
    assert f'<pre class="mermaid">\n{shown}\n</pre>' in page
    assert "<b>" not in page
    assert '<script type="module">' in page
    assert 'import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";' in page
    assert "mermaid.initialize({startOnLoad: true});" in page


def test_a_turn_with_mermaid_writes_a_page_per_block_beside_the_log_and_links_it(tmp_path):
    log = tmp_path / "chats" / "20260929-120000.jsonl"
    say = Console("m", log, color=False, width=200)
    pieces = say.render(Entry(7, Kind.TEXT, TURN))
    pages = [log.parent / f"20260929-120000-7-{n}.html" for n in (1, 2)]
    assert sorted(log.parent.glob("*.html")) == pages
    assert FLOW.replace(">", "&gt;") in pages[0].read_text()
    assert "sequenceDiagram\n  a-&gt;&gt;b: hi" in pages[1].read_text()
    uris = [p.resolve().as_uri() for p in pages]
    links = [f"[dim][link={u}]diagram {n}: {u}[/link][/dim]" for n, u in enumerate(uris, 1)]
    # the name, then each block's markdown with its link under it, then the prose after the last
    assert pieces[0] == "[bold]m ▸[/bold]" and pieces[2] == links[0] and pieces[4] == links[1]
    assert [type(p) for p in pieces[1::2]] == [Markdown, Markdown] and len(pieces) == 5


def test_the_console_prints_the_block_as_code_and_the_link_under_it(tmp_path, capsys):
    log = tmp_path / "c.jsonl"
    say = Console("m", log, color=False, width=2000)  # the link on one line, however long the directory
    say(Entry(3, Kind.TEXT, f"Before.\n\n```mermaid\n{FLOW}\n```\n\nAfter."))
    out = capsys.readouterr().out
    uri = (tmp_path / "c-3-1.html").resolve().as_uri()
    assert out.index("A-->B") < out.index(f"diagram 1: {uri}") < out.index("After.")


def test_without_a_log_the_line_says_the_diagram_was_not_saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pieces = Console("m", None, color=False).render(Entry(0, Kind.TEXT, f"```mermaid\n{FLOW}\n```"))
    assert pieces[-1] == "[dim]diagram 1: not saved, this chat has no log[/dim]"
    assert list(tmp_path.rglob("*.html")) == []


def test_a_turn_without_mermaid_writes_nothing(tmp_path):
    log = tmp_path / "c.jsonl"
    pieces = Console("m", log, color=False).render(Entry(1, Kind.TEXT, "```python\nx = 1\n```\n\nDone."))
    assert list(tmp_path.iterdir()) == [log]
    assert len(pieces) == 2 and isinstance(pieces[1], Markdown)
