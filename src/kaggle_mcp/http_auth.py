"""Authentication for the HTTP transport (remote use by ChatGPT / Claude).

A pure-ASGI middleware that protects every route except ``/healthz``:

* **Bearer token** (recommended): ``Authorization: Bearer <KAGGLE_MCP_AUTH_TOKEN>``.
  Works with Claude Code, the Claude API MCP connector, the OpenAI Responses
  API ``mcp`` tool, ``mcp-remote`` and any client that can send a header.
* **Secret path** (opt-in, ``KAGGLE_MCP_ALLOW_PATH_TOKEN=true``): the endpoint
  is also served at ``/s/<token>/mcp`` for UIs that cannot send custom headers
  (e.g. ChatGPT / claude.ai connectors configured with "No authentication").
  The URL itself becomes the secret - treat it like a password.

Tokens are compared in constant time. Repeated failures from one client IP are
throttled to slow down brute-force attempts.
"""

from __future__ import annotations

import hmac
import json
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
ASGIApp = Callable[[Scope, Callable, Callable], Awaitable[None]]

MIN_TOKEN_LENGTH = 32
PATH_PREFIX = "/s/"


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        token: str,
        allow_path_token: bool = False,
        max_failures: int = 20,
        window_seconds: int = 300,
    ):
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"Auth token must be at least {MIN_TOKEN_LENGTH} characters")
        self.app = app
        self._token = token.encode()
        self._allow_path_token = allow_path_token
        self._max_failures = max_failures
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _check(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._token)

    def _throttled(self, ip: str) -> bool:
        q = self._failures[ip]
        cutoff = time.monotonic() - self._window
        while q and q[0] < cutoff:
            q.popleft()
        if not q:
            self._failures.pop(ip, None)
        return len(q) >= self._max_failures

    def _record_failure(self, ip: str) -> None:
        if len(self._failures) > 10_000:  # bound memory under IP-spraying
            cutoff = time.monotonic() - self._window
            for key in [k for k, q in self._failures.items() if not q or q[-1] < cutoff]:
                del self._failures[key]
        self._failures[ip].append(time.monotonic())

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path == "/healthz":
            await _json(send, 200, {"status": "ok"})
            return

        client = scope.get("client") or ("unknown", 0)
        ip = str(client[0])
        if self._throttled(ip):
            await _json(send, 429, {"error": "too_many_failed_attempts"})
            return

        # 1) Secret path token: /s/<token>/mcp -> /mcp
        if self._allow_path_token and path.startswith(PATH_PREFIX):
            rest = path[len(PATH_PREFIX):]
            candidate, _, tail = rest.partition("/")
            if self._check(candidate):
                new_scope = dict(scope)
                new_scope["path"] = "/" + tail
                new_scope["raw_path"] = new_scope["path"].encode()
                await self.app(new_scope, receive, send)
                return
            self._record_failure(ip)
            await _json(send, 404, {"error": "not_found"})
            return

        # 2) Authorization: Bearer <token>
        header = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                header = v
                break
        scheme, _, value = header.decode("latin-1").partition(" ")
        if scheme.lower() == "bearer" and value and self._check(value.strip()):
            await self.app(scope, receive, send)
            return

        self._record_failure(ip)
        await _json(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b'Bearer realm="kaggle-mcp"')])


async def _json(send: Callable, status: int, body: dict, extra: list | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": payload})
