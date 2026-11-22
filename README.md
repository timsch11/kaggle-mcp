# kaggle-mcp

A lightweight, security-focused [Model Context Protocol](https://modelcontextprotocol.io) server for
Kaggle. It works with **Claude** (Desktop, Code, claude.ai, API) and **ChatGPT** (developer-mode
connectors, deep research, OpenAI Responses API).

With it a model can **read, write and run Kaggle notebooks**, explore competitions, datasets and models,
download data, submit to competitions and publish datasets, all through the official Kaggle API with
your API token.

* **Lightweight:** three runtime dependencies (`mcp`, `httpx`, `uvicorn`). It calls the Kaggle REST API
  directly and does not need the `kaggle` CLI or `kagglesdk`.
* **Secure by default:** new content is private, delete tools are off, local file access is
  sandboxed, transfers are size-capped, and HTTP mode requires a token. See [SECURITY.md](SECURITY.md).
* **Two transports:** `stdio` for local clients and Streamable HTTP for remote ones (ChatGPT, claude.ai).
* **Runs in Claude Code cloud sessions** with the Kaggle token held outside the VM. See [cloud/README.md](cloud/README.md).

---

## Contents

1. [Quick start](#quick-start)
2. [Kaggle authentication](#kaggle-authentication)
3. [Connecting clients](#connecting-clients)
   [Claude Desktop](#claude-desktop) · [Claude Code](#claude-code) · [claude.ai / ChatGPT (remote)](#remote-clients-chatgpt-claudeai) · [OpenAI Responses API](#openai-responses-api) · [Anthropic Messages API](#anthropic-messages-api)
4. [Tools](#tools)
5. [Working with notebooks](#working-with-notebooks)
6. [Configuration](#configuration)
7. [Deployment](#deployment-http)
8. [Development](#development)

---

## Quick start

```bash
git clone <this repo> kaggle-mcp && cd kaggle-mcp
uv venv && uv pip install -e .          # or: python -m venv .venv && .venv/bin/pip install -e .
export KAGGLE_API_TOKEN=...             # see "Kaggle authentication"
.venv/bin/kaggle-mcp                    # stdio server (what local clients launch)
```

Requires Python 3.10+.

## Kaggle authentication

The server uses the **Kaggle public API** with your personal credentials. It looks for them in this
order (the same order as the official CLI) and uses the first it finds:

| # | Source | Notes |
|---|--------|-------|
| 1 | `KAGGLE_API_TOKEN` env var | The token itself, or a path to a file containing it. **Recommended.** |
| 2 | `~/.kaggle/access_token` | File containing an API token |
| 3 | `KAGGLE_USERNAME` + `KAGGLE_KEY` | Legacy key pair, sent as HTTP Basic auth |
| 4 | `~/.kaggle/kaggle.json` | Legacy `{"username": "...", "key": "..."}` file |

To get a token: **kaggle.com → Settings → API → Generate New Token**. Keep credential files at `chmod 600`;
the server logs a warning when one is readable by other users. `KAGGLE_CONFIG_DIR` changes where it
looks for `~/.kaggle`.

Credentials are loaded lazily, held in memory only, sent only to `https://api.kaggle.com`, and never
logged or returned by any tool. Run the `kaggle_auth_status` tool to check that they work.

> Some actions also need consent on kaggle.com first. For example, to download competition data or
> submit, you must have joined the competition and accepted its rules in the browser.

## Connecting clients

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "kaggle": {
      "command": "/absolute/path/to/kaggle-mcp/.venv/bin/kaggle-mcp",
      "env": {
        "KAGGLE_API_TOKEN": "your-kaggle-api-token",
        "KAGGLE_MCP_WORKSPACE": "/absolute/path/to/kaggle-workspace"
      }
    }
  }
}
```

Restart Claude Desktop. The Kaggle tools appear in the tools menu.

### Claude Code

```bash
claude mcp add kaggle --env KAGGLE_API_TOKEN=your-token -- /absolute/path/to/kaggle-mcp/.venv/bin/kaggle-mcp
```

To use a remote deployment instead:

```bash
claude mcp add --transport http kaggle https://mcp.example.com/mcp --header "Authorization: Bearer $KAGGLE_MCP_AUTH_TOKEN"
```

### Remote clients (ChatGPT, claude.ai)

ChatGPT and claude.ai connect to a public **HTTPS** URL, so you need to run the HTTP transport
and expose it (see [Deployment](#deployment-http)).

1. Generate a token and start the server:

   ```bash
   export KAGGLE_MCP_AUTH_TOKEN=$(kaggle-mcp generate-token)
   export KAGGLE_MCP_ALLOW_PATH_TOKEN=true          # only for UIs that can't send headers (see below)
   export KAGGLE_MCP_ALLOWED_HOSTS=mcp.example.com  # your public hostname
   kaggle-mcp --transport http --behind-proxy
   ```

2. Expose it over HTTPS with a reverse proxy, Cloudflare Tunnel, ngrok or similar.

3. Add the connector:
   * **ChatGPT:** Settings → Apps & Connectors → Advanced settings → enable **Developer mode**,
     then **Create** a connector. MCP server URL: `https://mcp.example.com/s/<KAGGLE_MCP_AUTH_TOKEN>/mcp`,
     Authentication: *No authentication*. The server implements the `search` and `fetch` tools that
     ChatGPT deep research expects, and every other tool is available in developer mode.
   * **claude.ai:** Settings → Connectors → **Add custom connector**, using the same URL.

These connector UIs can do OAuth but cannot send a static `Authorization` header. The
**secret-path** URL (`/s/<token>/mcp`) is the opt-in workaround: whoever has the URL can use the
server, so keep it as private as a password, and rotate the token if it leaks. Clients that can send
headers should use `https://mcp.example.com/mcp` with `Authorization: Bearer <token>` and leave
`KAGGLE_MCP_ALLOW_PATH_TOKEN` off.

### OpenAI Responses API

```python
from openai import OpenAI

client = OpenAI()
resp = client.responses.create(
    model="gpt-5",
    tools=[{
        "type": "mcp",
        "server_label": "kaggle",
        "server_url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer <KAGGLE_MCP_AUTH_TOKEN>"},
        "require_approval": "always",
    }],
    input="Summarize the evaluation metric of the Titanic competition.",
)
```

### Anthropic Messages API

Use the [MCP connector](https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector) with
`{"type": "url", "url": "https://mcp.example.com/mcp", "name": "kaggle", "authorization_token": "<KAGGLE_MCP_AUTH_TOKEN>"}`
in `mcp_servers`.

## Tools

The server registers 36 tools by default (plus 2 delete tools if you enable them). Every tool carries MCP
annotations (`readOnlyHint`, `destructiveHint`, ...), which lets clients ask for confirmation before
write actions.

References use `owner/slug` for notebooks, datasets and models, and the slug alone for competitions
(e.g. `titanic`). Kaggle URLs work too. For **your own** notebooks and datasets a bare slug is enough
(`notebook_create(ref="titanic-baseline", ...)`): the server looks up the username behind your API
token once (via Kaggle's token-introspection endpoint, as the official CLI does) and fills it in.
`kaggle_auth_status` also reports it.

**Notebooks**

| Tool | What it does |
|---|---|
| `notebooks_list` | Search notebooks. Filter by user, competition, dataset or language; `mine=true` lists your own, including private ones. |
| `notebook_get` | Read a notebook as numbered cells (with outputs), raw `.ipynb`, or settings only. Long content is paged. |
| `notebook_create` ✏️ | Create a notebook or script from `cells`, raw `notebook_json`, or `source` (`# %%` markers split cells), with data sources, GPU/TPU, internet and privacy settings. Runs it by default. |
| `notebook_update` ✏️ | Save a new version, either with cell-level `edits` (replace/insert/append/delete) or a full replacement. Settings you don't pass stay as they are. Runs by default. |
| `notebook_run` ✏️ | Re-run an existing notebook as-is. |
| `notebook_status` | QUEUED / RUNNING / COMPLETE / ERROR. |
| `notebook_wait` | Poll until the run finishes (up to 15 min per call), reporting progress. |
| `notebook_output` | Output file list plus the tail of the execution log (stdout/stderr/tracebacks). |
| `notebook_download_output` 💾 | Download output files into the workspace. |
| `accelerator_quota` | Remaining GPU/TPU hours. |
| `notebook_delete` 🗑️ | Off by default; requires `KAGGLE_MCP_ALLOW_DELETE=true` and `confirm_ref`. |

**Competitions**

| Tool | What it does |
|---|---|
| `competitions_list` | Search by category, group (`entered`, ...) and sort order. |
| `competition_get` | Deadline, metric, submission limits, whether it's code-only. |
| `competition_pages` | Overview, evaluation, rules and data description text. |
| `competition_files` | List data files. |
| `competition_download` 💾 | Download one file or all data (zip) into the workspace. |
| `competition_leaderboard` | Public leaderboard. |
| `competition_submissions` | Your submissions with status and scores. |
| `competition_submit` ✏️ | Submit a workspace file or inline CSV `content`. |
| `competition_submit_notebook` ✏️ | Submit a notebook's output to a code competition. |

**Datasets**

| Tool | What it does |
|---|---|
| `datasets_list`, `dataset_get`, `dataset_files` | Search datasets and inspect metadata, files and columns. |
| `dataset_download` 💾 | Download a whole dataset or a single file. |
| `dataset_create` ✏️ / `dataset_create_version` ✏️ | Publish workspace files as a new (private) dataset or a new version. |
| `dataset_status` | Processing status after upload. |
| `dataset_delete` 🗑️ | Off by default. |

**Models, search and account**

| Tool | What it does |
|---|---|
| `models_list`, `model_get` | Search models and list their variations, for use as notebook `model_sources`. |
| `search_kaggle` | Full-text search across competitions, datasets, notebooks, models, discussions and users. |
| `search`, `fetch` | The ChatGPT connector / deep-research pair. Ids look like `dataset:owner/slug`. |
| `kaggle_auth_status` | Checks that the credentials work and shows the server policy. Never shows secrets. |

**Local workspace** (sandboxed directory)

| Tool | What it does |
|---|---|
| `workspace_list` / `workspace_read` | Browse downloaded files and read text files (CSV, logs, ...), paged. |
| `workspace_write` 💾 | Write a file, e.g. a submission CSV or a dataset file to upload. |
| `workspace_unzip` 💾 | Extract a downloaded archive, with zip-slip and zip-bomb protection. |

✏️ = changes your Kaggle account (hidden when `KAGGLE_MCP_READ_ONLY=true`) · 💾 = writes local workspace files · 🗑️ = irreversible, opt-in

## Working with notebooks

A typical edit-and-run loop driven by the model:

1. `notebook_get(ref="me/titanic-baseline")` returns cells numbered `### Cell 0 [markdown]`, `### Cell 1 [code]`, ...
2. `notebook_update(ref="me/titanic-baseline", edits=[{"op": "replace", "index": 3, "source": "model = RandomForestClassifier(n_estimators=300)"}])`
   saves a new version and starts a run on Kaggle.
3. `notebook_wait(ref=...)` waits until the run reports `COMPLETE` or `ERROR`.
4. `notebook_output(ref=...)` returns the log tail (tracebacks included) and output files. Fix the code and repeat.
5. Optionally `competition_submit_notebook(competition="titanic", notebook_ref=..., message="rf 300")`.

To create a notebook from scratch:

```json
{
  "ref": "titanic-baseline",
  "title": "Titanic Baseline",
  "competition_sources": ["titanic"],
  "cells": [
    {"cell_type": "markdown", "source": "# Titanic baseline"},
    {"cell_type": "code", "source": "import pandas as pd\ndf = pd.read_csv('/kaggle/input/titanic/train.csv')\nprint(df.shape)"}
  ]
}
```

Notes:

* The slug in `ref` should match the title (`Titanic Baseline` → `titanic-baseline`). Kaggle derives
  URLs from titles.
* Notebooks run on Kaggle's infrastructure, not on your machine. Attached data appears under `/kaggle/input/...`
  and files written to `/kaggle/working` become outputs.
* Pass `run=false` for a quick save without executing. `machine_shape` (e.g. `NvidiaTeslaT4`, `TpuV5E8`) or
  `enable_gpu` selects accelerators, which count against your weekly quota (`accelerator_quota`).
* As with the official CLI, outputs are stripped from pushed notebooks. Kaggle regenerates them when the notebook runs.

## Configuration

All settings are environment variables. See [.env.example](.env.example) for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `KAGGLE_MCP_WORKSPACE` | `~/kaggle-mcp-workspace` | The only directory the server reads or writes locally. |
| `KAGGLE_MCP_READ_ONLY` | `false` | Hide every tool that changes Kaggle state. |
| `KAGGLE_MCP_ALLOW_DELETE` | `false` | Expose the delete tools. |
| `KAGGLE_MCP_MAX_DOWNLOAD_MB` | `1024` | Per-file download cap (archive extraction is capped at 4×). |
| `KAGGLE_MCP_MAX_UPLOAD_MB` | `512` | Per-file upload cap. |
| `KAGGLE_MCP_MAX_OUTPUT_CHARS` | `60000` | Maximum text returned in a single tool result. |
| `KAGGLE_MCP_TIMEOUT` | `60` | Kaggle API request timeout (seconds). |
| `KAGGLE_MCP_TRANSPORT` | `stdio` | `stdio` or `http`. |
| `KAGGLE_MCP_HOST` / `KAGGLE_MCP_PORT` | `127.0.0.1` / `8000` | HTTP bind address. |
| `KAGGLE_MCP_AUTH_TOKEN` | — | Bearer token for HTTP (≥ 32 chars). Required for HTTP. |
| `KAGGLE_MCP_ALLOW_PATH_TOKEN` | `false` | Also serve the endpoint at `/s/<token>/mcp`. |
| `KAGGLE_MCP_ALLOWED_HOSTS` | — | Comma-separated public hostnames for Host-header validation. |
| `KAGGLE_MCP_LOG_LEVEL` | `INFO` | Log level. Logs go to stderr. |
| `KAGGLE_MCP_PROXY_AUTH` | `false` | An egress proxy injects the Kaggle credentials (Claude Code cloud network secrets). The server then sends none and needs no local token. Combine with `KAGGLE_USERNAME`. |

CLI flags: `--transport`, `--host`, `--port`, `--stateless` (no session affinity, good for serverless or
multiple replicas), `--behind-proxy` (trust `X-Forwarded-*` from `FORWARDED_ALLOW_IPS`), and
`--insecure-no-auth` (local testing only, localhost only).

## Deployment (HTTP)

**Docker:**

```bash
docker build -t kaggle-mcp .
docker run -d --name kaggle-mcp -p 127.0.0.1:8000:8000 \
  -e KAGGLE_API_TOKEN=... \
  -e KAGGLE_MCP_AUTH_TOKEN=$(openssl rand -base64 48 | tr -d '/+=') \
  -e KAGGLE_MCP_ALLOWED_HOSTS=mcp.example.com \
  -v kaggle-ws:/data/workspace \
  kaggle-mcp
```

Then put TLS in front of it, for example with Caddy (`mcp.example.com { reverse_proxy 127.0.0.1:8000 }`)
or `cloudflared tunnel --url http://127.0.0.1:8000`. The image runs as a non-root user. Endpoints:

* `POST/GET/DELETE /mcp`: MCP Streamable HTTP (Bearer auth)
* `/s/<token>/mcp`: the same endpoint via secret path (only if enabled)
* `GET /healthz`: unauthenticated liveness check

The server refuses to bind a non-localhost interface without `KAGGLE_MCP_AUTH_TOKEN`.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q
```

The tests run against a fake Kaggle API (`httpx.MockTransport`) and cover the security properties
(credential isolation on redirects, path traversal, zip slip, size caps, HTTP auth, brute-force
throttling, read-only and delete gating) as well as tool behaviour.

Project layout:

```
src/kaggle_mcp/
  cli.py          entry point, transport selection, HTTP hardening
  server.py       builds the MCPServer and registers tool groups
  core.py         tool registration policy (read-only/delete gating), error mapping, validators
  client.py       minimal async Kaggle API client (RPC calls, safe downloads/uploads)
  auth.py         Kaggle credential discovery (redacted repr)
  http_auth.py    bearer / secret-path ASGI middleware with throttling
  workspace.py    filesystem sandbox
  notebooks.py    .ipynb parsing, rendering, cell edits
  tools/          notebooks, competitions, datasets, discovery (search/fetch/models), files
```

The Kaggle API is RPC-style: every call is `POST https://api.kaggle.com/v1/<service>/<Method>` with a camelCase
JSON body, the same protocol `kagglesdk` uses. To add an endpoint, call `app.client.call(service, method, body)`
from a function decorated with `@app.tool(...)`.

## License

MIT
