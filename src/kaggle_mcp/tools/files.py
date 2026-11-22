"""Local workspace tools and upload/download helpers.

All paths are relative to the workspace (KAGGLE_MCP_WORKSPACE). Nothing
outside it can be read, written, uploaded or extracted to.
"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

from kaggle_mcp.core import App
from kaggle_mcp.workspace import WorkspaceError, safe_filename

MAX_ZIP_MEMBERS = 20_000


def read_upload(app: App, file_path: str | None, content: str | None, file_name: str) -> tuple[Path | bytes, str]:
    """Resolve an upload given as a workspace file or inline text, enforcing size limits."""
    if (file_path is None) == (content is None):
        raise ValueError("Provide exactly one of file_path or content")
    limit = app.settings.max_upload_bytes
    if file_path is not None:
        p = app.workspace.resolve(file_path, must_exist=True)
        if not p.is_file():
            raise WorkspaceError(f"Not a file: {file_path}")
        if p.stat().st_size > limit:
            raise ValueError(f"File exceeds the {limit}-byte upload limit")
        return p, safe_filename(p.name)
    data = content.encode("utf-8")  # type: ignore[union-attr]
    if len(data) > limit:
        raise ValueError(f"Content exceeds the {limit}-byte upload limit")
    return data, safe_filename(file_name, "submission.csv")


def finalize_download(app: App, dest: Path, size: int) -> dict:
    """Add a .zip suffix when Kaggle served a zip under a non-zip name."""
    if dest.suffix.lower() != ".zip" and zipfile.is_zipfile(dest):
        new = dest.with_name(dest.name + ".zip")
        dest.replace(new)
        dest = new
    out = {"path": app.workspace.relative(dest), "bytes": size}
    if dest.suffix.lower() == ".zip":
        out["hint"] = "Use workspace_unzip to extract."
    return out


def register(app: App) -> None:
    ws = app.workspace

    @app.tool(title="List workspace files", local_only=True)
    async def workspace_list(path: str | None = None) -> dict:
        """List files in the local workspace (downloads, outputs, files you wrote)."""
        return {"workspace": str(ws.root), "files": ws.list(path)}

    @app.tool(title="Read a workspace file", local_only=True)
    async def workspace_read(path: str, offset: int = 0, max_chars: int | None = None) -> dict:
        """Read a text file (CSV, JSON, code, logs...) from the workspace, paged by offset/max_chars."""
        p = ws.resolve(path, must_exist=True)
        if not p.is_file():
            raise WorkspaceError(f"Not a file: {path}")
        limit = min(max_chars or app.settings.max_output_chars, app.settings.max_output_chars)
        offset = max(0, offset)
        with p.open("rb") as fh:
            head = fh.read(8192)
        if b"\x00" in head:
            raise ValueError("Binary file; only text files can be read")
        # Read just enough bytes for the requested window (UTF-8 is <= 4 bytes/char).
        with p.open("rb") as fh:
            raw = fh.read((offset + limit) * 4 + 4)
        text = raw.decode("utf-8", errors="replace")
        chunk = text[offset:offset + limit]
        size = p.stat().st_size
        more = offset + len(chunk) < len(text) or len(raw) < size
        return {"path": ws.relative(p), "bytes": size, "offset": offset, "content": chunk, "has_more": more}

    @app.tool(title="Write a workspace file", local_only=True, local_write=True)
    async def workspace_write(path: str, content: str, overwrite: bool = False) -> dict:
        """Write a text file into the workspace (e.g. a submission CSV or dataset file to upload)."""
        p = ws.resolve(path)
        if p == ws.root:
            raise WorkspaceError("Path must name a file")
        if p.exists() and not overwrite:
            raise WorkspaceError(f"{path} exists; pass overwrite=true to replace it")
        data = content.encode("utf-8")
        if len(data) > app.settings.max_upload_bytes:
            raise ValueError("Content exceeds the upload size limit")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return {"path": ws.relative(p), "bytes": len(data)}

    @app.tool(title="Extract a zip in the workspace", local_only=True, local_write=True)
    async def workspace_unzip(path: str, dest_dir: str | None = None) -> dict:
        """Safely extract a zip archive inside the workspace (blocks path traversal,
        symlinks and zip bombs)."""
        src = ws.resolve(path, must_exist=True)
        dest = ws.resolve(dest_dir) if dest_dir else src.with_suffix("")
        if dest == src:
            dest = src.with_name(src.name + "_extracted")
        limit = app.settings.max_download_bytes * 4
        extracted, total = 0, 0
        with zipfile.ZipFile(src) as zf:
            members = zf.infolist()
            if len(members) > MAX_ZIP_MEMBERS:
                raise ValueError("Archive has too many entries")
            if sum(m.file_size for m in members) > limit:
                raise ValueError("Archive expands beyond the allowed size")
            for m in members:
                if m.is_dir():
                    continue
                if stat.S_ISLNK(m.external_attr >> 16):
                    continue  # never materialize symlinks
                target = (dest / m.filename).resolve()
                if dest.resolve() not in target.parents:
                    raise WorkspaceError(f"Unsafe path in archive: {m.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as fin, target.open("wb") as fout:
                    while block := fin.read(1024 * 1024):
                        total += len(block)
                        if total > limit:  # guards against lying headers
                            raise ValueError("Archive expands beyond the allowed size")
                        fout.write(block)
                extracted += 1
        return {"dest": ws.relative(dest), "files_extracted": extracted, "bytes": total}
