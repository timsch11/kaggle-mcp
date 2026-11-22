"""Kaggle API credential discovery.

Supported sources, first match wins (same precedence as the official CLI):

1. ``KAGGLE_API_TOKEN``            - API token value, or a path to a file holding it
2. ``~/.kaggle/access_token``      - file containing an API token
3. ``KAGGLE_USERNAME`` + ``KAGGLE_KEY`` - legacy key pair (HTTP Basic auth)
4. ``~/.kaggle/kaggle.json``       - legacy ``{"username": ..., "key": ...}`` file

Credentials are kept in memory only, are never logged, and are never returned
by any tool. ``Credentials.__repr__`` is redacted so they cannot leak through
tracebacks or debug output by accident.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class CredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    source: str
    token: str | None = None
    username: str | None = None
    key: str | None = None

    def auth_header(self) -> str:
        if self.token:
            return f"Bearer {self.token}"
        raw = f"{self.username}:{self.key}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def __repr__(self) -> str:  # never print secrets
        return f"Credentials(source={self.source!r}, kind={'token' if self.token else 'key'}, secret=<redacted>)"

    __str__ = __repr__


def _kaggle_dir() -> Path:
    cfg = os.environ.get("KAGGLE_CONFIG_DIR")
    return Path(cfg).expanduser() if cfg else Path.home() / ".kaggle"


def _warn_if_world_readable(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("Credential file %s is readable by other users; run: chmod 600 %s", path, path)


def _read_secret_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    _warn_if_world_readable(path)
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def load_credentials() -> Credentials:
    token = os.environ.get("KAGGLE_API_TOKEN", "").strip()
    if token:
        # The variable may hold the token itself or a path to a token file.
        maybe_path = Path(token).expanduser()
        if len(token) < 4096 and maybe_path.is_file():
            file_token = _read_secret_file(maybe_path)
            if file_token:
                return Credentials(source="KAGGLE_API_TOKEN (file)", token=file_token)
        else:
            return Credentials(source="KAGGLE_API_TOKEN", token=token)

    kdir = _kaggle_dir()
    for name in ("access_token", "access_token.txt"):
        file_token = _read_secret_file(kdir / name)
        if file_token:
            return Credentials(source=f"{kdir / name}", token=file_token)

    user = os.environ.get("KAGGLE_USERNAME", "").strip()
    key = os.environ.get("KAGGLE_KEY", "").strip()
    if user and key:
        return Credentials(source="KAGGLE_USERNAME/KAGGLE_KEY", username=user, key=key)

    kjson = kdir / "kaggle.json"
    if kjson.is_file():
        _warn_if_world_readable(kjson)
        try:
            data = json.loads(kjson.read_text(encoding="utf-8"))
            if data.get("username") and data.get("key"):
                return Credentials(source=str(kjson), username=data["username"], key=data["key"])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise CredentialsError(f"{kjson} is not valid JSON") from exc

    raise CredentialsError(
        "No Kaggle credentials found. Create an API token at https://www.kaggle.com/settings "
        "and set KAGGLE_API_TOKEN (or KAGGLE_USERNAME + KAGGLE_KEY, or ~/.kaggle/kaggle.json)."
    )
