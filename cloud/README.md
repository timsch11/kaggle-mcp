# Using kaggle-mcp in Claude Code cloud sessions

Cloud sessions (claude.ai/code, `claude --cloud`, scheduled tasks) run in a sandboxed VM. The
setup below runs kaggle-mcp **inside that VM**, so nothing has to run on your own devices. Your
Kaggle token never enters the VM either: it is stored as a **network secret**, and the
environment's egress proxy attaches it to requests for `api.kaggle.com` only after they leave the
VM.

```
cloud session VM                               Anthropic egress proxy          Kaggle
┌──────────────────────────────────┐          ┌─────────────────────────┐
│ Claude ─stdio─> kaggle-mcp       │ ──────>  │ + Authorization: Bearer │ ──> api.kaggle.com
│ (KAGGLE_MCP_PROXY_AUTH=true,     │          │   <network secret>      │
│  sends no credentials)           │ ──────>  │ (no secret added)       │ ──> storage.googleapis.com
└──────────────────────────────────┘          └─────────────────────────┘     (signed URLs)
```

## 1. Put the code on GitHub

Cloud sessions clone a GitHub repository. How the server gets installed depends on whether that
repository is private or public:

* **Private repo (the default here):** the setup script can't `pip install` from a private repository,
  so it skips the install. In sessions **on the kaggle-mcp repository**, the launcher in
  `.mcp.json` installs the server from the checked-out code on first start, which took about 12 s in testing.
  Use the kaggle-mcp repo as the session's repository, or add it next to your project.
* **Public repo, or a published package:** set `KAGGLE_MCP_SOURCE` at the top of [setup.sh](setup.sh)
  (e.g. `git+https://github.com/<you>/kaggle-mcp.git@v0.1.0`). The server is then preinstalled and
  cached, and any repository with the short `.mcp.json` from step 3 can use it. Pinning a tag or
  commit means sessions only ever run code you have reviewed.

## 2. Create the environment

On claude.ai/code, open the environment selector, choose **New environment** and fill in the fields
below. Environments can only be created in the web UI. Anyone you share the environment with can
use the Kaggle access it grants.

| Field | Value |
|---|---|
| **Network access** | **Limited**, keep *Allow package managers* checked (this covers PyPI, GitHub and `storage.googleapis.com`) |
| **Allowed domains** | `api.kaggle.com` |
| **Network secrets** | Allowed websites: `api.kaggle.com` · Header name: `Authorization` · Prefix: `Bearer` · Value: your Kaggle API token (kaggle.com → Settings → API → Generate New Token) |
| **Environment variables** | see the block below (nothing secret) |
| **Setup script** | Private repo: leave it empty (or paste [setup.sh](setup.sh) unchanged; it skips cleanly). Public source: paste setup.sh with `KAGGLE_MCP_SOURCE` set |

Environment variables:

```env
KAGGLE_MCP_PROXY_AUTH=true
KAGGLE_USERNAME=your-kaggle-username
KAGGLE_MCP_WORKSPACE=/tmp/kaggle-workspace
# Optional hardening:
# KAGGLE_MCP_READ_ONLY=true
```

* `KAGGLE_MCP_PROXY_AUTH=true` tells the server not to look for a local token and not to send an
  `Authorization` header; the proxy adds one.
* `KAGGLE_USERNAME` lets bare slugs (`notebook_create(ref="my-notebook")`) resolve to your account.
  The server can't look it up itself here, because the token isn't available inside the VM.
* **Never** put `KAGGLE_API_TOKEN` in Environment variables: those are readable by anyone using the
  environment and by every process in the VM. Network secrets aren't.

## 3. Add the MCP server to the repositories you work in

`.mcp.json` from a session's repository is loaded automatically (in sessions with a single repo).

**In this repository:** already done. [/.mcp.json](../.mcp.json) runs
[kaggle-mcp-launch.sh](kaggle-mcp-launch.sh).

**In any other repository** (only with a public install source, see step 1): commit this
`.mcp.json`, which relies on the setup script having installed `kaggle-mcp`:

```json
{
  "mcpServers": {
    "kaggle": { "type": "stdio", "command": "kaggle-mcp" }
  }
}
```

## 4. Check it

Start a cloud session with the environment and ask Claude to run `kaggle_auth_status`. You should see:

```json
{"authenticated": true, "username": "your-kaggle-username",
 "credential_source": "egress proxy (KAGGLE_MCP_PROXY_AUTH) - token never present locally", ...}
```

| Symptom | Likely cause |
|---|---|
| No `kaggle` tools at all | Setup script failed (check the session's setup log), or the repo has no `.mcp.json` |
| `401 Unauthenticated` | Network secret missing, its host isn't exactly `api.kaggle.com`, or the token is wrong or expired |
| Connection or "host not allowed" errors | `api.kaggle.com` is missing from Allowed domains |
| "Username unknown in proxy-auth mode" | `KAGGLE_USERNAME` isn't set; full `owner/slug` refs still work |
| Downloads fail | *Allow package managers* was unchecked, which removes `storage.googleapis.com` from the allowlist |

## How the pieces fit

* **[setup.sh](setup.sh)** runs as root before Claude starts. It creates `/opt/kaggle-mcp` (a venv),
  installs the package from `KAGGLE_MCP_SOURCE`, and links `kaggle-mcp` into `/usr/local/bin`. The
  result is cached between sessions. It's rebuilt when the script or the allowed hosts change, or about
  every 7 days.
* **[kaggle-mcp-launch.sh](kaggle-mcp-launch.sh)** is what this repo's `.mcp.json` starts. It uses
  `.venv/bin/kaggle-mcp` if present (local development), otherwise `kaggle-mcp` on PATH (installed by
  the setup script). If neither exists it installs the checked-out code into `.venv` on first
  use. That last path needs no GitHub install, so it also covers a private repo, at the cost
  of a slower first start in each new session.
* **The workspace** (`KAGGLE_MCP_WORKSPACE`) lives inside the VM and is discarded with the session.
  Anything worth keeping should go back to Kaggle (dataset or notebook output) or into the repository.

## Security notes

* The token is only ever attached by the proxy, and only to `api.kaggle.com`. The server
  independently refuses to send credentials anywhere else, and signed download/upload URLs are
  fetched without credentials.
* All the server's other protections still apply inside the VM: workspace sandbox, size caps,
  delete tools off by default, and private-by-default creation. Consider `KAGGLE_MCP_READ_ONLY=true`
  for environments used for research or scheduled tasks.
* Rotate the token on kaggle.com and re-add the network secret if you think it leaked (values can't
  be viewed or edited after saving, only deleted and re-added).
