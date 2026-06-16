# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import json
import sys
from typing import Any

from lionagi.utils import to_dict

# Try to import rich for enhanced console output
try:
    from rich.align import Align
    from rich.box import MINIMAL, ROUNDED
    from rich.console import Console
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.theme import Theme

    DARK_THEME = Theme(
        {
            "info": "bright_cyan",
            "warning": "bright_yellow",
            "error": "bold bright_red",
            "success": "bold bright_green",
            "panel.border": "bright_blue",
            "panel.title": "bold bright_cyan",
            "markdown.h1": "bold bright_magenta",
            "markdown.h2": "bold bright_blue",
            "markdown.h3": "bold bright_cyan",
            "markdown.h4": "bold bright_green",
            "markdown.code": "bright_yellow on grey23",
            "markdown.code_block": "bright_white on grey15",
            "markdown.paragraph": "bright_white",
            "markdown.text": "bright_white",
            "markdown.emph": "italic bright_yellow",
            "markdown.strong": "bold bright_white",
            "markdown.item": "bright_cyan",
            "markdown.item.bullet": "bright_blue",
            "json.key": "bright_cyan",
            "json.string": "bright_green",
            "json.number": "bright_yellow",
            "json.boolean": "bright_magenta",
            "json.null": "bright_red",
            "yaml.key": "bright_cyan",
            "yaml.string": "bright_green",
            "yaml.number": "bright_yellow",
            "yaml.boolean": "bright_magenta",
        }
    )

    LIGHT_THEME = Theme(
        {
            "info": "blue",
            "warning": "dark_orange",
            "error": "bold red",
            "success": "bold green4",
            "panel.border": "blue",
            "panel.title": "bold blue",
            "markdown.h1": "bold dark_magenta",
            "markdown.h2": "bold dark_blue",
            "markdown.h3": "bold dark_cyan",
            "markdown.h4": "bold dark_green",
            "markdown.code": "dark_orange on grey93",
            "markdown.code_block": "black on grey82",
            "markdown.paragraph": "black",
            "markdown.text": "black",
            "markdown.emph": "italic dark_orange",
            "markdown.strong": "bold black",
            "markdown.item": "dark_blue",
            "markdown.item.bullet": "blue",
            "json.key": "dark_blue",
            "json.string": "dark_green",
            "json.number": "dark_orange",
            "json.boolean": "dark_magenta",
            "json.null": "dark_red",
            "yaml.key": "dark_blue",
            "yaml.string": "dark_green",
            "yaml.number": "dark_orange",
            "yaml.boolean": "dark_magenta",
        }
    )
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    DARK_THEME = None
    LIGHT_THEME = None


def in_notebook() -> bool:
    """Return True if running inside a Jupyter notebook."""
    try:
        from IPython import get_ipython

        shell = get_ipython().__class__.__name__
        return "ZMQInteractiveShell" in shell
    except Exception:
        return False


def in_console() -> bool:
    """Return True if stdout is a TTY and not inside a notebook."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty() and not in_notebook()


def format_dict(data: Any, indent: int = 0) -> str:
    """Format nested Python data as a YAML-like indented string."""
    lines = []
    prefix = "  " * indent  # 2 spaces per indent level

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"{prefix}{key}:")
                lines.append(format_dict(value, indent + 1))
            elif isinstance(value, list):
                lines.append(f"{prefix}{key}:")
                for item in value:
                    item_str = format_dict(item, indent + 2).lstrip()
                    lines.append(f"{prefix}  - {item_str}")
            elif isinstance(value, str) and "\n" in value:
                lines.append(f"{prefix}{key}: |")
                subprefix = "  " * (indent + 1)
                for line in value.splitlines():
                    lines.append(f"{subprefix}{line}")
            else:
                item_str = format_dict(value, indent + 1).lstrip()
                lines.append(f"{prefix}{key}: {item_str}")
        return "\n".join(lines)

    elif isinstance(data, list):
        for item in data:
            item_str = format_dict(item, indent + 1).lstrip()
            lines.append(f"{prefix}- {item_str}")
        return "\n".join(lines)

    return prefix + str(data)


def as_readable(
    input_: Any,
    /,
    *,
    md: bool = False,
    format_curly: bool = False,
    display_str: bool = False,
    max_chars: int | None = None,
    use_rich: bool = True,
    theme: str = "dark",
    max_panel_width: int = 140,
    panel: bool = True,
    border: bool = True,
) -> str:
    """Render input as JSON or YAML-like string; display_str=True prints via rich/IPython/print."""

    def to_dict_safe(obj: Any) -> Any:
        to_dict_kwargs = {
            "use_model_dump": True,
            "fuzzy_parse": True,
            "recursive": True,
            "recursive_python_only": False,
            "max_recursive_depth": 5,
        }
        return to_dict(obj, **to_dict_kwargs)

    def _inner(i_: Any) -> Any:
        items = []
        try:
            if isinstance(i_, list):
                items = [to_dict_safe(x) for x in i_]
            else:
                maybe_list = to_dict_safe(i_)
                items = maybe_list if isinstance(maybe_list, list) else [maybe_list]
        except Exception:
            return str(i_)

        rendered = []
        for item in items:
            if format_curly:
                rendered.append(format_dict(item))
            else:
                try:
                    rendered.append(json.dumps(item, indent=2, ensure_ascii=False))
                except Exception:
                    rendered.append(str(item))

        final_str = "\n\n".join(rendered).strip()

        if md:
            if format_curly:
                return f"```yaml\n{final_str}\n```"
            else:
                return f"```json\n{final_str}\n```"

        return final_str

    str_ = _inner(input_).strip()
    if max_chars is not None and len(str_) > max_chars:
        trunc = str_[:max_chars] + "...\n\n[Truncated output]"
        str_ = trunc + ("\n```" if str_.endswith("\n```") else "")

    # -------------------- PRINT / DISPLAY LOGIC ---------------------------
    if not display_str:
        return str_  # caller will handle printing

    # (1) IPython notebook --------------------------------------------------
    if md and in_notebook():
        from IPython.display import Markdown, display

        display(Markdown(str_))
        return

    # (2) Rich console ------------------------------------------------------
    if RICH_AVAILABLE and in_console() and use_rich:
        console_theme = DARK_THEME if theme == "dark" else LIGHT_THEME
        syntax_theme = "github-dark" if theme == "dark" else "solarized-light"
        console = Console(theme=console_theme)

        # determine prose / fenced code
        is_fenced_code = md and str_.startswith("```") and str_.rstrip().endswith("```")
        is_prose_md = md and not is_fenced_code
        panel_width = min(console.width - 4, max_panel_width)

        def _out(rich_obj):
            if not panel:
                console.print(Padding(rich_obj, (0, 0, 0, 2)))
                return

            console.print(
                Padding(
                    Panel(
                        Align.left(rich_obj, pad=False),
                        border_style="panel.border" if border else "",
                        box=ROUNDED if border else MINIMAL,
                        width=panel_width,
                        expand=False,
                    ),
                    (0, 0, 0, 4),
                )
            )

        # 2‑a prose markdown ------------------------------------------------
        if is_prose_md:
            from rich.markdown import Markdown as RichMarkdown

            _out(RichMarkdown(str_, code_theme=syntax_theme))
            return

        # 2‑b code (fenced or explicit) -------------------------------------
        if is_fenced_code:
            lines = str_.splitlines()
            lang, code = (
                (lines[0][3:].strip() or ("yaml" if format_curly else "json")),
                "\n".join(lines[1:-1]),
            )
        else:
            lang, code = ("yaml" if format_curly else "json"), str_

        syntax = Syntax(
            code,
            lang,
            theme=syntax_theme,
            line_numbers=False,
            word_wrap=True,
            background_color="default",
        )
        _out(syntax)
        return

    # (3) Plain fallback ----------------------------------------------------
    print(str_)
