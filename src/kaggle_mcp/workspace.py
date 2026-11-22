"""Confinement of all local file access to a single workspace directory.

Tools accept *relative* paths only. Every path is resolved (following
symlinks) and must stay inside the workspace, which blocks ``..`` traversal,
absolute paths and symlinks that point outside.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath


class WorkspaceError(ValueError):
    pass


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, fallback: str = "file") -> str:
    """Reduce an untrusted (server-provided) file name to a single safe component."""
    base = PurePosixPath(name.replace("\\", "/")).name
    base = _SAFE_NAME.sub("_", base).strip("._")
    return base[:200] or fallback


class Workspace:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    def ensure(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.root

    def resolve(self, relative: str | None, *, must_exist: bool = False) -> Path:
        rel = (relative or "").strip()
        if "\x00" in rel:
            raise WorkspaceError("Invalid path")
        p = PurePosixPath(rel.replace("\\", "/"))
        if p.is_absolute() or re.match(r"^[A-Za-z]:", rel):
            raise WorkspaceError("Paths must be relative to the workspace")
        self.ensure()
        target = (self.root / p).resolve()
        if target != self.root and self.root not in target.parents:
            raise WorkspaceError("Path escapes the workspace")
        if must_exist and not target.exists():
            raise WorkspaceError(f"Not found in workspace: {rel}")
        return target

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def list(self, relative: str | None = None, max_entries: int = 500) -> list[dict]:
        base = self.resolve(relative, must_exist=True)
        if base.is_file():
            return [{"path": self.relative(base), "type": "file", "size": base.stat().st_size}]
        out: list[dict] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for name in sorted(filenames):
                fp = Path(dirpath) / name
                try:
                    rp = fp.resolve()
                except OSError:
                    continue
                if self.root not in rp.parents:
                    continue  # skip symlinks pointing outside
                out.append({"path": self.relative(fp), "type": "file", "size": fp.stat().st_size})
                if len(out) >= max_entries:
                    return out
        return out
