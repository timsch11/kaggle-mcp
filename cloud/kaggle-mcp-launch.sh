#!/usr/bin/env bash
# Launcher used by .mcp.json. Starts the kaggle-mcp stdio server, preferring:
#   1. this checkout's local venv (.venv/bin/kaggle-mcp) - local development
#   2. a kaggle-mcp already on PATH (installed by cloud/setup.sh)
#   3. otherwise installs this checkout into .venv on first use (cloud fallback)
# stdout is the MCP channel, so all install output goes to stderr.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$ROOT/.venv/bin/kaggle-mcp" ]; then
  exec "$ROOT/.venv/bin/kaggle-mcp" "$@"
fi
if command -v kaggle-mcp >/dev/null 2>&1; then
  exec kaggle-mcp "$@"
fi
if [ -f "$ROOT/pyproject.toml" ]; then
  echo "kaggle-mcp: installing from $ROOT (first run)..." >&2
  python3 -m venv "$ROOT/.venv" >&2
  "$ROOT/.venv/bin/pip" install --quiet --disable-pip-version-check "$ROOT" >&2
  exec "$ROOT/.venv/bin/kaggle-mcp" "$@"
fi
echo "kaggle-mcp is not installed (see cloud/README.md)" >&2
exit 1
