#!/usr/bin/env bash
# Diagnose kaggle-mcp in a cloud (or local) session. Prints no secrets.
#   bash cloud/doctor.sh            (from the kaggle-mcp repo)
#   curl -fsSL https://raw.githubusercontent.com/timsch11/kaggle-mcp/main/cloud/doctor.sh | bash
set -uo pipefail
ok()   { printf '  [ok]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
info() { printf '  [info] %s\n' "$*"; }

echo "== Session"
info "CLAUDE_CODE_REMOTE=${CLAUDE_CODE_REMOTE:-<unset>}  user=$(id -un)  HOME=$HOME  cwd=$(pwd)"

echo "== Installation"
if command -v kaggle-mcp >/dev/null 2>&1; then ok "kaggle-mcp on PATH: $(command -v kaggle-mcp) ($(kaggle-mcp --version 2>&1))"
else bad "kaggle-mcp not on PATH (setup script did not run or failed; check the environment's setup log)"; fi
[ -x /opt/kaggle-mcp/bin/kaggle-mcp ] && ok "/opt/kaggle-mcp venv present" || info "/opt/kaggle-mcp venv missing"

echo "== MCP registration"
found=0
if [ -f .mcp.json ] && grep -q '"kaggle"' .mcp.json; then ok "./.mcp.json defines 'kaggle'"; found=1
else info "no 'kaggle' server in ./.mcp.json (cwd repo)"; fi
if [ -f "$HOME/.claude.json" ] && python3 -c 'import json,sys; sys.exit(0 if "kaggle" in json.load(open(sys.argv[1])).get("mcpServers",{}) else 1)' "$HOME/.claude.json" 2>/dev/null; then
  ok "~/.claude.json has user-scope 'kaggle'"; found=1
else info "no user-scope 'kaggle' in ~/.claude.json"; fi
[ $found -eq 1 ] || bad "kaggle is not registered anywhere Claude looks: add .mcp.json to this repo (see cloud/README.md)"

echo "== Configuration (values hidden)"
for v in KAGGLE_MCP_PROXY_AUTH KAGGLE_USERNAME KAGGLE_MCP_WORKSPACE KAGGLE_MCP_READ_ONLY; do
  if [ -n "${!v:-}" ]; then
    case $v in KAGGLE_USERNAME) info "$v is set";; *) info "$v=${!v}";; esac
  else info "$v unset"; fi
done
[ -n "${KAGGLE_API_TOKEN:-}" ] && bad "KAGGLE_API_TOKEN is set as a plain env var: move it to a network secret" || ok "no plain KAGGLE_API_TOKEN in env"
[ "${KAGGLE_MCP_PROXY_AUTH:-}" = "true" ] || bad "KAGGLE_MCP_PROXY_AUTH is not 'true' (needed with network secrets)"

echo "== Network"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -A kaggle-mcp-doctor -X POST -H 'Content-Type: application/json' -d '{}' \
  https://api.kaggle.com/v1/kernels.KernelsApiService/GetAcceleratorQuotaStatistics || echo 000)
case $code in
  200) ok "api.kaggle.com reachable and authenticated (network secret works)";;
  401) bad "api.kaggle.com reachable but UNAUTHENTICATED: network secret missing, wrong host, or bad token";;
  000) bad "api.kaggle.com unreachable: add it to Allowed domains";;
  *)   bad "api.kaggle.com returned HTTP $code (blocked by proxy? check Allowed domains)";;
esac
code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 https://storage.googleapis.com/ || echo 000)
[ "$code" != "000" ] && ok "storage.googleapis.com reachable (downloads)" || bad "storage.googleapis.com unreachable (keep 'Allow package managers' checked)"

echo "== MCP handshake"
if command -v kaggle-mcp >/dev/null 2>&1; then
  out=$( { printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"doctor","version":"1"}}}'; sleep 5; } \
    | timeout 20 kaggle-mcp 2>/dev/null | head -c 200)
  [[ "$out" == *'"result"'* ]] && ok "server starts and answers initialize" || bad "server did not answer initialize: ${out:-<no output>}"
fi
