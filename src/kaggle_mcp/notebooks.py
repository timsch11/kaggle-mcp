"""Helpers for working with Jupyter notebooks (.ipynb JSON) without nbformat."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

CellType = Literal["code", "markdown", "raw"]


class Cell(BaseModel):
    cell_type: CellType = "code"
    source: str = ""


class CellEdit(BaseModel):
    """One edit operation applied to a notebook's cell list.

    * ``replace`` - overwrite cell ``index`` (source, and type if given)
    * ``insert``  - insert a new cell *before* ``index``
    * ``append``  - add a new cell at the end (``index`` ignored)
    * ``delete``  - remove cell ``index``
    """

    op: Literal["replace", "insert", "append", "delete"]
    index: int | None = Field(default=None, description="0-based cell index (not needed for append)")
    source: str | None = None
    cell_type: CellType | None = None


class NotebookError(ValueError):
    pass


_KERNELSPECS = {
    "python": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "r": {"display_name": "R", "language": "R", "name": "ir"},
}


def _source_text(src: Any) -> str:
    if isinstance(src, list):
        return "".join(str(s) for s in src)
    return "" if src is None else str(src)


def parse_notebook(text: str) -> dict:
    try:
        nb = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotebookError(f"Notebook is not valid JSON: {exc.msg}") from exc
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise NotebookError("Notebook JSON must be an object with a 'cells' list")
    return nb


def make_cell(cell_type: str, source: str) -> dict:
    cell: dict[str, Any] = {"cell_type": cell_type, "metadata": {}, "source": source}
    if cell_type == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def new_notebook(cells: list[Cell], language: str = "python") -> dict:
    return {
        "cells": [make_cell(c.cell_type, c.source) for c in cells],
        "metadata": {
            "kernelspec": _KERNELSPECS.get(language, _KERNELSPECS["python"]),
            "language_info": {"name": "python" if language != "r" else "R"},
        },
        "nbformat": 4,
        "nbformat_minor": 4,
    }


def cells_from_percent_script(text: str) -> list[Cell]:
    """Split a "percent format" script (``# %%`` / ``# %% [markdown]`` markers) into cells.

    Text without any marker becomes a single code cell.
    """
    cells: list[Cell] = []
    ctype: CellType = "code"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip("\n")
        if ctype == "markdown":
            body = "\n".join(line[2:] if line.startswith("# ") else line.lstrip("#") for line in body.split("\n"))
        if body.strip():
            cells.append(Cell(cell_type=ctype, source=body))

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# %%") or stripped.startswith("#%%"):
            flush()
            buf = []
            ctype = "markdown" if "[markdown]" in stripped.lower() else "code"
            continue
        buf.append(line)
    flush()
    return cells or [Cell(cell_type="code", source=text)]


def prepare_for_push(nb: dict) -> str:
    """Mirror the official CLI: clear code outputs, flatten list sources."""
    for cell in nb.get("cells", []):
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        if isinstance(cell.get("source"), list):
            cell["source"] = "".join(cell["source"])
    return json.dumps(nb)


def apply_edits(nb: dict, edits: list[CellEdit]) -> dict:
    cells: list[dict] = nb["cells"]
    for i, e in enumerate(edits):
        n = len(cells)
        if e.op == "append":
            cells.append(make_cell(e.cell_type or "code", e.source or ""))
            continue
        if e.index is None:
            raise NotebookError(f"edit #{i}: 'index' is required for {e.op}")
        if e.op == "insert":
            if not 0 <= e.index <= n:
                raise NotebookError(f"edit #{i}: insert index {e.index} out of range 0..{n}")
            cells.insert(e.index, make_cell(e.cell_type or "code", e.source or ""))
            continue
        if not 0 <= e.index < n:
            raise NotebookError(f"edit #{i}: index {e.index} out of range 0..{n - 1}")
        if e.op == "delete":
            cells.pop(e.index)
        elif e.op == "replace":
            old = cells[e.index]
            new_type = e.cell_type or old.get("cell_type", "code")
            if new_type != old.get("cell_type"):
                cells[e.index] = make_cell(new_type, e.source if e.source is not None else _source_text(old.get("source")))
            elif e.source is not None:
                old["source"] = e.source
    return nb


def _output_text(out: dict) -> str:
    t = out.get("output_type")
    if t == "stream":
        return _source_text(out.get("text"))
    if t == "error":
        return f"{out.get('ename')}: {out.get('evalue')}"
    if t in ("execute_result", "display_data"):
        data = out.get("data", {})
        if "text/plain" in data:
            return _source_text(data["text/plain"])
        return "[" + ", ".join(sorted(data)) + "]"
    return ""


def render_notebook(nb: dict, language: str = "python", include_outputs: bool = True) -> str:
    """Compact, model-friendly rendering: one numbered block per cell."""
    parts: list[str] = []
    for i, cell in enumerate(nb.get("cells", [])):
        ctype = cell.get("cell_type", "code")
        src = _source_text(cell.get("source"))
        if ctype == "code":
            parts.append(f"### Cell {i} [code]\n```{language}\n{src}\n```")
            if include_outputs:
                outs = [o for o in (_output_text(o) for o in cell.get("outputs", [])) if o.strip()]
                if outs:
                    parts.append("Output:\n```\n" + "\n".join(outs).rstrip()[:4000] + "\n```")
        else:
            parts.append(f"### Cell {i} [{ctype}]\n{src}")
    return "\n\n".join(parts)
