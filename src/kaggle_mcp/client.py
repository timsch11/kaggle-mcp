"""Minimal async client for the Kaggle public API.

The Kaggle API (the one used by the official ``kaggle`` CLI / ``kagglesdk``)
is RPC-style: every call is ``POST https://api.kaggle.com/v1/<service>/<Method>``
with a camelCase JSON body.

Security properties:

* The ``Authorization`` header is attached **only** to requests sent to
  ``api.kaggle.com``. File downloads are redirected to signed cloud-storage
  URLs; those are fetched by a separate client that never carries credentials.
* Redirect / upload targets must be ``https``.
* Downloads are streamed to a temporary file with a hard byte cap, then
  atomically renamed into place.
* Error messages surfaced to the model contain the API's message only, never
  request headers.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from kaggle_mcp import __version__
from kaggle_mcp.auth import Credentials, load_credentials

log = logging.getLogger(__name__)

API_BASE = "https://api.kaggle.com/v1"
USER_AGENT = f"kaggle-mcp/{__version__}"


class KaggleAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Kaggle API error {status}: {message}")
        self.status = status
        self.message = message


def _clean(value: Any) -> Any:
    """Drop None values recursively so optional fields are omitted."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _require_https(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise KaggleAPIError(0, "Refusing non-HTTPS transfer URL returned by the API")
    return url


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return (resp.text or resp.reason_phrase or "unknown error")[:500]
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:1000]
    return str(data)[:500]


class KaggleClient:
    def __init__(
        self,
        credentials: Credentials | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy_auth: bool = False,
    ):
        self._credentials = credentials
        self._proxy_auth = proxy_auth
        self._timeout = timeout
        self._transport = transport
        self._api: httpx.AsyncClient | None = None
        self._storage: httpx.AsyncClient | None = None
        self._username: str | None = None

    # -- lifecycle -------------------------------------------------------
    @property
    def credentials(self) -> Credentials:
        if self._credentials is None:
            self._credentials = load_credentials()
        return self._credentials

    def _api_client(self) -> httpx.AsyncClient:
        if self._api is None:
            self._api = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=self._timeout,
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
                transport=self._transport,
            )
        return self._api

    def _storage_client(self) -> httpx.AsyncClient:
        # Deliberately has NO auth header: used for signed GCS URLs only.
        if self._storage is None:
            self._storage = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, read=300.0),
                follow_redirects=False,
                headers={"User-Agent": USER_AGENT},
                transport=self._transport,
            )
        return self._storage

    async def aclose(self) -> None:
        for c in (self._api, self._storage):
            if c is not None:
                await c.aclose()
        self._api = self._storage = None

    @property
    def credential_source(self) -> str:
        if self._proxy_auth:
            return "egress proxy (KAGGLE_MCP_PROXY_AUTH) - token never present locally"
        return self.credentials.source

    def _auth(self) -> dict[str, str]:
        # In proxy mode the network proxy adds the Authorization header for
        # api.kaggle.com after the request leaves this machine.
        if self._proxy_auth:
            return {}
        return {"Authorization": self.credentials.auth_header()}

    # -- RPC -------------------------------------------------------------
    async def call(self, service: str, method: str, body: dict | None = None) -> dict:
        resp = await self._api_client().post(f"/{service}/{method}", json=_clean(body or {}), headers=self._auth())
        if resp.status_code >= 400:
            raise KaggleAPIError(resp.status_code, _error_message(resp))
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise KaggleAPIError(resp.status_code, "Unexpected non-JSON response") from exc
        if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] >= 400:
            raise KaggleAPIError(data["code"], str(data.get("message", "")))
        return data if isinstance(data, dict) else {"result": data}

    async def username(self) -> str:
        """The Kaggle username the credentials belong to (cached).

        Key-pair credentials carry it; for an API token it is looked up once via the
        same token-introspection endpoint the official CLI uses.
        """
        if self._username is None and self._proxy_auth:
            name = os.environ.get("KAGGLE_USERNAME", "").strip()
            if not name:
                raise KaggleAPIError(
                    0,
                    "Username unknown in proxy-auth mode: set KAGGLE_USERNAME, or pass full 'owner/slug' refs",
                )
            self._username = name
        if self._username is None:
            creds = self.credentials
            if creds.username:
                self._username = creds.username
            else:
                info = await self.call("security.OAuthService", "IntrospectToken", {"token": creds.token})
                name = info.get("username")
                if not name or info.get("active") is False:
                    raise KaggleAPIError(401, "Could not determine the username for this API token")
                self._username = str(name)
        return self._username

    # -- downloads -------------------------------------------------------
    async def download(
        self, service: str, method: str, body: dict, dest: Path, max_bytes: int
    ) -> int:
        """Run a download RPC and stream the file to ``dest``. Returns bytes written."""
        api = self._api_client()
        req = api.build_request("POST", f"/{service}/{method}", json=_clean(body), headers=self._auth())
        resp = await api.send(req, stream=True)
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise KaggleAPIError(resp.status_code, "Redirect without location")
                await resp.aclose()
                return await self.download_url(_require_https(location), dest, max_bytes)
            if resp.status_code >= 400:
                await resp.aread()
                raise KaggleAPIError(resp.status_code, _error_message(resp))
            return await self._stream_to(resp, dest, max_bytes)
        finally:
            await resp.aclose()

    async def download_url(self, url: str, dest: Path, max_bytes: int, _hops: int = 0) -> int:
        """Fetch a signed URL without credentials (follows up to 5 https redirects)."""
        client = self._storage_client()
        resp = await client.send(client.build_request("GET", _require_https(url)), stream=True)
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                if _hops >= 5:
                    raise KaggleAPIError(resp.status_code, "Too many redirects")
                location = resp.headers.get("location", "")
                await resp.aclose()
                return await self.download_url(str(httpx.URL(url).join(location)), dest, max_bytes, _hops + 1)
            if resp.status_code >= 400:
                raise KaggleAPIError(resp.status_code, "Download failed")
            return await self._stream_to(resp, dest, max_bytes)
        finally:
            await resp.aclose()

    @staticmethod
    async def _stream_to(resp: httpx.Response, dest: Path, max_bytes: int) -> int:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise KaggleAPIError(413, f"File is {int(declared)} bytes, above the {max_bytes}-byte download limit")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".partial-")
        written = 0
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in resp.aiter_bytes(1024 * 256):
                    written += len(chunk)
                    if written > max_bytes:
                        raise KaggleAPIError(413, f"Download exceeded the {max_bytes}-byte limit")
                    fh.write(chunk)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return written

    # -- uploads ---------------------------------------------------------
    async def upload_to_url(self, url: str, data: bytes | Path) -> None:
        """PUT bytes (or a file, streamed) to a signed upload URL returned by Kaggle."""
        _require_https(url)
        if isinstance(data, Path):
            size = data.stat().st_size

            async def chunks():
                with data.open("rb") as fh:
                    while block := fh.read(1024 * 1024):
                        yield block

            resp = await self._storage_client().put(url, content=chunks(), headers={"Content-Length": str(size)})
        else:
            resp = await self._storage_client().put(url, content=data)
        if resp.status_code not in (200, 201):
            raise KaggleAPIError(resp.status_code, "Upload to storage failed")
