"""Shared plumbing for tool modules: the app context, tool registration with
policy enforcement (read-only / delete gating), error mapping and helpers."""

from __future__ import annotations

import functools
import inspect
import re
from dataclasses import dataclass
from typing import Any, Callable

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from kaggle_mcp.auth import CredentialsError
from kaggle_mcp.client import KaggleAPIError, KaggleClient
from kaggle_mcp.config import Settings
from kaggle_mcp.notebooks import NotebookError
from kaggle_mcp.workspace import Workspace, WorkspaceError

# Errors whose messages are safe and useful to show the model.
_EXPECTED = (KaggleAPIError, WorkspaceError, NotebookError, CredentialsError, ValueError)

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def split_ref(ref: str, what: str = "ref") -> tuple[str, str]:
    """Validate and split an ``owner/slug`` reference (also accepts kaggle.com URLs)."""
    ref = (ref or "").strip()
    m = re.match(r"^https?://(?:www\.)?kaggle\.com/(?:code|datasets|models)/([^/?#]+)/([^/?#]+)", ref)
    if m:
        ref = f"{m.group(1)}/{m.group(2)}"
    if not _REF.match(ref):
        raise ValueError(f"{what} must look like 'owner/slug', got {ref!r}")
    owner, slug = ref.split("/", 1)
    return owner, slug


def check_slug(value: str, what: str = "slug") -> str:
    value = (value or "").strip()
    m = re.match(r"^https?://(?:www\.)?kaggle\.com/(?:competitions|c)/([^/?#]+)", value)
    if m:
        value = m.group(1)
    if not _SLUG.match(value):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


def enum_value(value: str | None, mapping: dict[str, str], what: str) -> str | None:
    if value is None or value == "":
        return None
    key = value.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in mapping:
        raise ValueError(f"Invalid {what} {value!r}. Choose one of: {', '.join(mapping)}")
    return mapping[key]


def pick(d: dict | None, keys: list[str]) -> dict:
    """Keep only ``keys`` with meaningful values (the API reports some unset fields as "None")."""
    d = d or {}
    out = {k: d[k] for k in keys if k in d and d[k] not in (None, "", [], "None")}
    if "ref" in out and isinstance(out["ref"], str):
        out["ref"] = clean_ref(out["ref"])
    return out


_REF_PREFIX = re.compile(r"^(?:https?://(?:www\.)?kaggle\.com)?/*(?:code|datasets|models|competitions|c)/")


def clean_ref(ref: str) -> str:
    """Normalize refs the API returns as URLs/paths ('/code/o/s', 'https://kaggle.com/competitions/x')
    to the 'owner/slug' or competition-slug form the tools accept."""
    return _REF_PREFIX.sub("", ref.strip()).strip("/")


@dataclass
class App:
    mcp: MCPServer
    client: KaggleClient
    settings: Settings
    workspace: Workspace

    async def own_ref(self, ref: str, what: str = "ref") -> tuple[str, str]:
        """Like split_ref, but a bare slug ('my-notebook') means one of *your* items:
        the authenticated username is filled in automatically."""
        value = (ref or "").strip()
        if "/" not in value and _SLUG.match(value):
            return await self.client.username(), value
        return split_ref(value, what)

    def truncate(self, text: str, limit: int | None = None) -> str:
        limit = min(limit or self.settings.max_output_chars, self.settings.max_output_chars)
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n\n[... truncated {len(text) - limit} characters; use offset/max_chars to page]"

    def tool(
        self,
        *,
        title: str,
        writes: bool = False,
        destructive: bool = False,
        local_only: bool = False,
        local_write: bool = False,
    ) -> Callable[[Callable], Callable]:
        """Register an async tool.

        ``writes``      - changes state on Kaggle; skipped in read-only mode.
        ``destructive`` - irreversible; additionally requires KAGGLE_MCP_ALLOW_DELETE.
        ``local_only``  - touches only the local workspace (no Kaggle call).
        ``local_write`` - writes files into the local workspace (e.g. downloads).
        """

        def decorator(fn: Callable) -> Callable:
            if writes and self.settings.read_only:
                return fn
            if destructive and not self.settings.allow_delete:
                return fn
            assert inspect.iscoroutinefunction(fn)

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except ToolError:
                    raise
                except _EXPECTED as exc:
                    raise ToolError(str(exc)) from exc

            annotations = ToolAnnotations(
                title=title,
                readOnlyHint=not (writes or destructive or local_write),
                destructiveHint=destructive,
                idempotentHint=not writes,
                openWorldHint=not local_only,
            )
            self.mcp.tool(title=title, annotations=annotations)(wrapper)
            return fn

        return decorator
