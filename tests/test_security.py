"""Security-property tests: credentials, path confinement, transfers, HTTP auth."""

from __future__ import annotations

import io
import json
import os
import zipfile

import httpx
import pytest

from kaggle_mcp.auth import CredentialsError, load_credentials
from kaggle_mcp.client import KaggleAPIError
from kaggle_mcp.http_auth import BearerAuthMiddleware
from kaggle_mcp.workspace import Workspace, WorkspaceError, safe_filename

from .conftest import FAKE_TOKEN, result_data


# --- credentials -----------------------------------------------------------
@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ("KAGGLE_API_TOKEN", "KAGGLE_USERNAME", "KAGGLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KAGGLE_CONFIG_DIR", str(tmp_path / "kcfg"))
    return tmp_path / "kcfg"


def test_token_env_preferred(clean_env, monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "tok123")
    monkeypatch.setenv("KAGGLE_USERNAME", "u")
    monkeypatch.setenv("KAGGLE_KEY", "k")
    creds = load_credentials()
    assert creds.auth_header() == "Bearer tok123"


def test_legacy_key_pair_uses_basic(clean_env, monkeypatch):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    monkeypatch.setenv("KAGGLE_KEY", "secret")
    assert load_credentials().auth_header().startswith("Basic ")


def test_kaggle_json(clean_env):
    clean_env.mkdir()
    (clean_env / "kaggle.json").write_text(json.dumps({"username": "bob", "key": "abc"}))
    os.chmod(clean_env / "kaggle.json", 0o600)
    assert load_credentials().username == "bob"


def test_missing_credentials(clean_env):
    with pytest.raises(CredentialsError):
        load_credentials()


def test_credentials_repr_redacted(clean_env, monkeypatch):
    monkeypatch.setenv("KAGGLE_API_TOKEN", "supersecretvalue")
    creds = load_credentials()
    assert "supersecretvalue" not in repr(creds) and "supersecretvalue" not in str(creds)


# --- workspace confinement ---------------------------------------------------
@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../x", "C:\\Windows", "x\x00y"])
def test_workspace_blocks_escape(tmp_path, bad):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        ws.resolve(bad)


def test_workspace_blocks_symlink_escape(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure()
    (tmp_path / "secret.txt").write_text("s")
    os.symlink(tmp_path / "secret.txt", ws.root / "link.txt")
    with pytest.raises(WorkspaceError):
        ws.resolve("link.txt")
    assert ws.list() == []  # escaping symlinks are not listed either


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..") == "file"
    assert "/" not in safe_filename("a/b\\c.csv")


async def test_workspace_read_tool_rejects_traversal(make_server):
    server = make_server()
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="escapes|relative"):
        await server.call_tool("workspace_read", {"path": "../../etc/passwd"})


async def test_unzip_blocks_zip_slip(make_server, workspace):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "pwned")
    (workspace / "a.zip").write_bytes(buf.getvalue())
    server = make_server()
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="Unsafe path"):
        await server.call_tool("workspace_unzip", {"path": "a.zip"})
    assert not (workspace.parent / "evil.txt").exists()


# --- transfer safety ---------------------------------------------------------
async def test_auth_header_only_sent_to_kaggle_api(make_server, fake):
    fake.on(
        "datasets.DatasetApiService/DownloadDataset",
        lambda req: httpx.Response(302, headers={"location": "https://storage.googleapis.com/b/f.csv?sig=1"}),
    )
    fake.on("GET storage.googleapis.com/b/f.csv", lambda req: httpx.Response(200, content=b"a,b\n1,2\n"))
    server = make_server()
    data = result_data(await server.call_tool("dataset_download", {"ref": "o/d", "file_name": "f.csv"}))
    assert data["bytes"] == 8
    api_req, storage_req = fake.requests
    assert api_req.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "authorization" not in storage_req.headers


async def test_non_https_redirect_refused(make_server, fake):
    fake.on(
        "datasets.DatasetApiService/DownloadDataset",
        lambda req: httpx.Response(302, headers={"location": "http://evil.example/x"}),
    )
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="non-HTTPS"):
        await make_server().call_tool("dataset_download", {"ref": "o/d"})
    assert len(fake.requests) == 1


async def test_download_size_cap(make_server, fake, workspace):
    fake.on("datasets.DatasetApiService/DownloadDataset", lambda req: httpx.Response(200, content=b"x" * 5000))
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="limit"):
        await make_server(max_download_bytes=1000).call_tool("dataset_download", {"ref": "o/d"})
    leftovers = [p for p in workspace.rglob("*") if p.is_file()]
    assert leftovers == []  # partial file removed


async def test_api_error_message_surfaced(make_server, fake):
    fake.on(
        "competitions.CompetitionApiService/GetCompetition",
        lambda req: httpx.Response(403, json={"error": {"code": 403, "message": "You must accept the rules"}}),
    )
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="accept the rules"):
        await make_server().call_tool("competition_get", {"competition": "titanic"})


# --- policy gating ---------------------------------------------------------
async def test_read_only_hides_write_tools(make_server):
    names = {t.name for t in await make_server(read_only=True).list_tools()}
    assert "notebook_get" in names
    for w in ("notebook_create", "notebook_update", "notebook_run", "competition_submit", "dataset_create"):
        assert w not in names


async def test_delete_tools_opt_in(make_server):
    assert "notebook_delete" not in {t.name for t in await make_server().list_tools()}
    names = {t.name for t in await make_server(allow_delete=True).list_tools()}
    assert {"notebook_delete", "dataset_delete"} <= names


async def test_delete_requires_confirmation(make_server, fake):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="confirm_ref"):
        await make_server(allow_delete=True).call_tool("notebook_delete", {"ref": "me/nb", "confirm_ref": "me/other"})
    assert fake.requests == []


@pytest.mark.parametrize("ref", ["../etc", "a/b/c", "", "a b/c", "owner/"])
async def test_invalid_refs_rejected_before_network(make_server, fake, ref):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        await make_server().call_tool("notebook_get", {"ref": ref})
    assert fake.requests == []


# --- HTTP bearer middleware --------------------------------------------------
TOKEN = "t" * 40


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": scope["path"].encode()})


def _client(**kw) -> httpx.AsyncClient:
    mw = BearerAuthMiddleware(_ok_app, TOKEN, **kw)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mw), base_url="http://test")


async def test_http_requires_bearer():
    async with _client() as c:
        assert (await c.post("/mcp")).status_code == 401
        assert (await c.post("/mcp", headers={"Authorization": "Bearer wrong"})).status_code == 401
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and r.text == "/mcp"
        assert (await c.get("/healthz")).status_code == 200


async def test_path_token_disabled_by_default():
    async with _client() as c:
        assert (await c.post(f"/s/{TOKEN}/mcp")).status_code == 401


async def test_path_token_when_enabled():
    async with _client(allow_path_token=True) as c:
        r = await c.post(f"/s/{TOKEN}/mcp")
        assert r.status_code == 200 and r.text == "/mcp"
        assert (await c.post("/s/wrongtoken/mcp")).status_code == 404


async def test_bruteforce_throttled():
    async with _client(max_failures=3) as c:
        for _ in range(3):
            await c.post("/mcp", headers={"Authorization": "Bearer nope"})
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 429


def test_short_token_rejected():
    with pytest.raises(ValueError):
        BearerAuthMiddleware(_ok_app, "short")


def test_https_required_for_uploads():
    from kaggle_mcp.client import _require_https

    with pytest.raises(KaggleAPIError):
        _require_https("http://storage.googleapis.com/x")
    assert _require_https("https://storage.googleapis.com/x")


# --- proxy-auth mode (Claude Code cloud network secrets) ----------------------------
def _proxy_server(fake, workspace):
    from dataclasses import replace as _replace

    from kaggle_mcp.client import KaggleClient
    from kaggle_mcp.config import Settings
    from kaggle_mcp.server import build_server

    settings = _replace(Settings(workspace=workspace), proxy_auth=True)
    client = KaggleClient(transport=httpx.MockTransport(fake), proxy_auth=True)  # no credentials at all
    return build_server(settings, client)


async def test_proxy_mode_sends_no_credentials(fake, workspace, monkeypatch, clean_env):
    monkeypatch.setenv("KAGGLE_USERNAME", "alice")
    fake.on("kernels.KernelsApiService/GetAcceleratorQuotaStatistics", {})
    fake.on("kernels.KernelsApiService/GetKernelSessionStatus", {"status": "COMPLETE"})
    server = _proxy_server(fake, workspace)
    status = result_data(await server.call_tool("kaggle_auth_status", {}))
    assert status["username"] == "alice" and "proxy" in status["credential_source"]
    await server.call_tool("notebook_status", {"ref": "my-nb"})
    assert fake.bodies("kernels.KernelsApiService/GetKernelSessionStatus")[0]["userName"] == "alice"
    assert all("authorization" not in r.headers for r in fake.requests)
    assert fake.bodies("security.OAuthService/IntrospectToken") == []


async def test_proxy_mode_without_username(fake, workspace, monkeypatch, clean_env):
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="KAGGLE_USERNAME"):
        await _proxy_server(fake, workspace).call_tool("notebook_status", {"ref": "my-nb"})
    assert fake.requests == []
