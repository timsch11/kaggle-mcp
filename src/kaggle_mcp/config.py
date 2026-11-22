"""Runtime configuration, read once from environment variables.

Every setting has a safe default: the server starts read/write but with
deletion disabled, file access confined to a dedicated workspace directory and
conservative size limits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class Settings:
    # Directory that is the *only* place the server reads or writes local files.
    workspace: Path = field(default_factory=lambda: Path.home() / "kaggle-mcp-workspace")
    # Disable every tool that changes state on Kaggle (push, submit, create...).
    read_only: bool = False
    # Deleting notebooks/datasets is irreversible, so it is opt-in.
    allow_delete: bool = False
    # Hard cap on a single download written to the workspace.
    max_download_bytes: int = 1024 * 1024 * 1024
    # Hard cap on a single upload read from the workspace / passed inline.
    max_upload_bytes: int = 512 * 1024 * 1024
    # Max characters of text returned to the model in one tool result.
    max_output_chars: int = 60_000
    # Timeout for Kaggle API requests (seconds).
    request_timeout: int = 60
    # Credentials are injected by an egress proxy (e.g. Claude Code cloud "network
    # secrets"): send no Authorization header and need no local token.
    proxy_auth: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        ws = os.environ.get("KAGGLE_MCP_WORKSPACE")
        return cls(
            workspace=Path(ws).expanduser() if ws else Path.home() / "kaggle-mcp-workspace",
            read_only=_bool("KAGGLE_MCP_READ_ONLY", False),
            allow_delete=_bool("KAGGLE_MCP_ALLOW_DELETE", False),
            max_download_bytes=_int("KAGGLE_MCP_MAX_DOWNLOAD_MB", 1024) * 1024 * 1024,
            max_upload_bytes=_int("KAGGLE_MCP_MAX_UPLOAD_MB", 512) * 1024 * 1024,
            max_output_chars=_int("KAGGLE_MCP_MAX_OUTPUT_CHARS", 60_000, minimum=1000),
            request_timeout=_int("KAGGLE_MCP_TIMEOUT", 60),
            proxy_auth=_bool("KAGGLE_MCP_PROXY_AUTH", False),
        )
