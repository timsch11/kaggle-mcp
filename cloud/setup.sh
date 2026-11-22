#!/bin/bash
# Setup script for a Claude Code cloud environment (claude.ai/code -> environment -> Setup script).
#
# 1. Installs kaggle-mcp into an isolated venv at /opt/kaggle-mcp and exposes the
#    `kaggle-mcp` command on PATH.
# 2. Registers it as a user-scope MCP server in /root/.claude.json so sessions on ANY
#    repository can see it. (Best effort: this is not documented for cloud sessions. The
#    documented route is a `.mcp.json` in the session's repository, see cloud/README.md.)
#
# Runs as root before Claude starts; the resulting filesystem is cached and reused by
# later sessions (rebuilt when this script or the allowed hosts change, or after ~7 days).
# Environment variables are NOT available while this runs, so the source is set here.
# It must be installable without credentials (public repo, PyPI release, wheel URL).
# Setup scripts must exit 0, so failures only print warnings.
set -uo pipefail

# Pinned to a reviewed commit so sessions only run code you have checked. To upgrade, replace the
# commit (or use a release tag, e.g. ...kaggle-mcp.git@v0.1.0) after reviewing the changes.
KAGGLE_MCP_REPO="https://github.com/timsch11/kaggle-mcp.git"
KAGGLE_MCP_REF="dc6551555075153fd0da00a43fa4c100c569cecb"
KAGGLE_MCP_SOURCE="${KAGGLE_MCP_SOURCE-git+${KAGGLE_MCP_REPO}@${KAGGLE_MCP_REF}}"
REGISTER_USER_SCOPE="${REGISTER_USER_SCOPE:-true}"

VENV=/opt/kaggle-mcp

install_pkg() {
  # Prefer uv (preinstalled in cloud images; needs no python3-venv/ensurepip).
  if command -v uv >/dev/null 2>&1; then
    { [ -x "$VENV/bin/python" ] || uv venv --quiet "$VENV"; } \
      && uv pip install --quiet --python "$VENV/bin/python" --upgrade "$KAGGLE_MCP_SOURCE"
  else
    { [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"; } \
      && "$VENV/bin/pip" install --quiet --disable-pip-version-check --upgrade "$KAGGLE_MCP_SOURCE"
  fi
}

register_user_scope() {
  # Merge {"mcpServers": {"kaggle": ...}} into ~/.claude.json without touching other keys.
  python3 - "$HOME/.claude.json" <<'PY' || echo "WARNING: could not register user-scope MCP server" >&2
import json, os, sys
path = sys.argv[1]
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        sys.exit("existing ~/.claude.json is unreadable; leaving it alone")
servers = data.setdefault("mcpServers", {})
servers.setdefault("kaggle", {"type": "stdio", "command": "/usr/local/bin/kaggle-mcp", "args": [], "env": {}})
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
print(f"kaggle-mcp: registered user-scope MCP server in {path}")
PY
}

if [ -z "$KAGGLE_MCP_SOURCE" ]; then
  echo "kaggle-mcp: no install source configured; skipping (launcher installs from checkout)"
  exit 0
fi

if install_pkg; then
  ln -sf "$VENV/bin/kaggle-mcp" /usr/local/bin/kaggle-mcp
  kaggle-mcp --version
  [ "$REGISTER_USER_SCOPE" = "true" ] && register_user_scope
else
  echo "WARNING: kaggle-mcp install from '$KAGGLE_MCP_SOURCE' failed (private repo or no network?)." >&2
  echo "         Sessions on the kaggle-mcp repo fall back to installing from the checkout." >&2
fi
exit 0
