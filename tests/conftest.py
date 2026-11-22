from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

import httpx
import pytest

from kaggle_mcp.auth import Credentials
from kaggle_mcp.client import KaggleClient
from kaggle_mcp.config import Settings
from kaggle_mcp.server import build_server

FAKE_TOKEN = "KGAT_test_token_value"


class FakeKaggle:
    """Records requests and dispatches them to per-endpoint handlers."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handlers: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def on(self, key: str, handler: Callable[[httpx.Request], httpx.Response] | dict) -> None:
        if isinstance(handler, dict):
            payload = handler
            handler = lambda req: httpx.Response(200, json=payload)  # noqa: E731
        self.handlers[key] = handler

    def bodies(self, key: str) -> list[dict]:
        return [json.loads(r.content or b"{}") for r in self.requests if self._key(r) == key]

    @staticmethod
    def _key(req: httpx.Request) -> str:
        if req.url.host == "api.kaggle.com":
            return req.url.path.split("/v1/", 1)[1]
        return f"{req.method} {req.url.host}{req.url.path}"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        key = self._key(req)
        if key not in self.handlers:
            return httpx.Response(404, json={"error": {"code": 404, "message": f"no handler for {key}"}})
        return self.handlers[key](req)


@pytest.fixture
def fake() -> FakeKaggle:
    return FakeKaggle()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def make_server(fake: FakeKaggle, workspace: Path):
    def _make(**overrides):
        settings = replace(Settings(workspace=workspace), **overrides)
        client = KaggleClient(
            credentials=Credentials(source="test", token=FAKE_TOKEN),
            transport=httpx.MockTransport(fake),
        )
        return build_server(settings, client)

    return _make


def result_data(result) -> dict:
    """Extract the JSON payload from a CallToolResult."""
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)
