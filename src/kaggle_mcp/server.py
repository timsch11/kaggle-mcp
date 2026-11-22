"""Builds the MCP server with all tool groups registered."""

from __future__ import annotations

from mcp.server import MCPServer

from kaggle_mcp import __version__
from kaggle_mcp.client import KaggleClient
from kaggle_mcp.config import Settings
from kaggle_mcp.core import App
from kaggle_mcp.tools import competitions, datasets, discovery, files, notebooks
from kaggle_mcp.workspace import Workspace

INSTRUCTIONS = """\
Kaggle MCP server. Typical flows:
- Explore: search / search_kaggle, competitions_list, datasets_list, notebooks_list, models_list.
- Notebooks: notebook_get (format='cells' shows numbered cells) -> notebook_update with `edits`
  (or notebook_create for a new one) -> notebook_wait -> notebook_output (log + files).
- Competitions: competition_get, competition_pages, competition_files, competition_submit or
  competition_submit_notebook, then competition_submissions for scores.
- Local files live in a sandboxed workspace (workspace_list / workspace_read / workspace_write).
References use 'owner/slug' (notebooks, datasets, models) or the competition slug. For your own
notebooks/datasets a bare slug is enough: the server fills in your username (see kaggle_auth_status).
Creating/updating notebooks, submitting and publishing datasets act on the user's real Kaggle
account: confirm with the user before doing so. New notebooks/datasets are private by default.
"""


def build_server(settings: Settings | None = None, client: KaggleClient | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    mcp = MCPServer(name="kaggle", title="Kaggle", version=__version__, instructions=INSTRUCTIONS)
    app = App(
        mcp=mcp,
        client=client or KaggleClient(timeout=settings.request_timeout, proxy_auth=settings.proxy_auth),
        settings=settings,
        workspace=Workspace(settings.workspace),
    )
    for module in (discovery, notebooks, competitions, datasets, files):
        module.register(app)
    return mcp
