# Security model

This server lets an AI model act on your Kaggle account and on a local directory. This document
covers what is protected, how, and what you are still responsible for.

## Threats considered

| Threat | Mitigation |
|---|---|
| **Credential leakage:** the Kaggle token ends up in logs, tool output, tracebacks or third-party requests | The token is loaded lazily and kept in memory only. `Credentials.__repr__` is redacted. The `Authorization` header goes only to `https://api.kaggle.com`. Signed storage URLs (downloads and uploads) are fetched by a separate HTTP client that never carries credentials, and redirects are followed manually. Tools never return credentials, and `kaggle_auth_status` shows only the credential *source*. Unexpected exceptions reach the model only as `Error executing tool <name>`, with no details. `httpx` logging is limited to WARNING. |
| **Unauthorized remote access** to the HTTP endpoint | HTTP mode requires `KAGGLE_MCP_AUTH_TOKEN` (≥ 32 chars). The server refuses to start on a non-loopback address without it. Tokens are compared in constant time (`hmac.compare_digest`). Each client IP is limited to 20 failed attempts per 5 minutes, after which further failures get 429; a request with the valid token is always accepted, so a shared IP cannot be used to lock you out. Secret-path tokens are redacted from the server's access log. Only `/healthz` is unauthenticated. |
| **DNS rebinding / cross-origin browser attacks** | On localhost, the MCP SDK's Host/Origin validation is on automatically. For public deployments, set `KAGGLE_MCP_ALLOWED_HOSTS` to enable it for your hostname. Requests with any other `Host` header are rejected with 421. |
| **Path traversal / arbitrary file read or write**, e.g. a prompt-injected model asking to upload `~/.ssh/id_rsa` | All local file access goes through `Workspace.resolve`. Only relative paths are accepted. Paths are resolved through symlinks and must stay inside `KAGGLE_MCP_WORKSPACE`. Absolute paths, `..`, drive letters and NUL bytes are rejected, and symlinks pointing outside are neither followed nor listed. File names supplied by the server are reduced to safe single components. |
| **Malicious archives** (zip slip, symlinks, zip bombs) | `workspace_unzip` checks every member's resolved path, skips symlink entries, limits archives to 20,000 entries, and caps the declared *and* the actually extracted size at 4× the download limit. |
| **Resource exhaustion** | Per-file download and upload caps. Downloads are streamed to a temporary file and moved into place atomically, and partial files are deleted on failure. Tool output is capped at `KAGGLE_MCP_MAX_OUTPUT_CHARS`. `notebook_wait` is capped at 15 minutes per call. Request timeouts apply throughout. |
| **Downgrade / MITM** on transfers | Every redirect and upload URL must be `https`, or the transfer is refused. TLS verification uses httpx's defaults (certifi). |
| **Unintended or destructive account actions** | Write tools carry `readOnlyHint=false` and destructive tools carry `destructiveHint=true`, so clients can require approval. `KAGGLE_MCP_READ_ONLY=true` removes every state-changing tool. Delete tools are not registered unless `KAGGLE_MCP_ALLOW_DELETE=true`, and they also require `confirm_ref` to repeat the target exactly. New notebooks and datasets default to **private**. |
| **Injection into API calls** | Every `owner/slug`, competition slug and enum value is validated against strict patterns or allow-lists *before* any network request. Request bodies are JSON-encoded by httpx and are never built by string concatenation. |

## Your responsibilities

* **Prompt injection is real.** Notebook text, dataset descriptions and forum content are untrusted, and
  a model reading them could be steered into actions you didn't intend. Keep approvals turned on in your
  client for write tools, or use `KAGGLE_MCP_READ_ONLY=true` for research-only sessions.
* **Protect the HTTP token, and the secret URL if you enable `KAGGLE_MCP_ALLOW_PATH_TOKEN`.** Anyone
  holding either can act as you on Kaggle. Rotate it (`kaggle-mcp generate-token`) if it may have leaked.
  Prefer header-based Bearer auth wherever the client supports it. kaggle-mcp redacts the token from its
  own access log, but a reverse proxy or tunnel in front of it logs the full URL unless you configure it not to.
* **Always terminate TLS** in front of the HTTP transport (reverse proxy or tunnel). Never expose plain HTTP.
  When behind a proxy, use `--behind-proxy` and set `FORWARDED_ALLOW_IPS` to the proxy's address so
  failure throttling sees real client IPs.
* **Use a dedicated workspace directory.** Don't point `KAGGLE_MCP_WORKSPACE` at your home directory or at
  anything containing secrets.
* Keep `~/.kaggle/kaggle.json` / `access_token` at `chmod 600`, and never commit `.env` files. They are
  already in `.gitignore`.
* Notebooks run on Kaggle's infrastructure under your account. Code a model writes there can use your
  GPU quota and anything attached to the notebook (datasets, and internet if enabled).

## Reporting

Please report vulnerabilities privately to the maintainer rather than in public issues.
