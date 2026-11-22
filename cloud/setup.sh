#!/bin/bash
# Setup script for a Claude Code cloud environment (claude.ai/code -> environment -> Setup script).
#
# Installs kaggle-mcp into an isolated venv at /opt/kaggle-mcp and exposes the
# `kaggle-mcp` command on PATH. Runs as root before Claude starts; the resulting
# filesystem is cached and reused by later sessions (rebuilt when this script or
# the allowed hosts change, or after ~7 days).
#
# Environment variables are NOT available while this runs, so the package source
# is set here. It must be pip-installable without credentials: a PUBLIC git repo,
# a PyPI release, or a wheel URL. If the repo is PRIVATE the install fails with a
# warning (or set KAGGLE_MCP_SOURCE="" to skip it): sessions on the kaggle-mcp repo
# itself then install from the checked-out code on first start (cloud/kaggle-mcp-launch.sh).
#
# Setup scripts must exit 0, so a failed install only prints a warning.
set -uo pipefail

# Pinned to a reviewed commit so sessions only run code you have checked. To upgrade, replace the
# commit (or use a release tag, e.g. ...kaggle-mcp.git@v0.1.0) after reviewing the changes.
KAGGLE_MCP_REPO="https://github.com/timsch11/kaggle-mcp.git"
KAGGLE_MCP_REF="dc6551555075153fd0da00a43fa4c100c569cecb"
KAGGLE_MCP_SOURCE="${KAGGLE_MCP_SOURCE-git+${KAGGLE_MCP_REPO}@${KAGGLE_MCP_REF}}"

VENV=/opt/kaggle-mcp

if [ -z "$KAGGLE_MCP_SOURCE" ]; then
  echo "kaggle-mcp: no public install source configured; skipping (launcher installs from checkout)"
  exit 0
fi

if { [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"; } \
  && "$VENV/bin/pip" install --quiet --disable-pip-version-check --upgrade "$KAGGLE_MCP_SOURCE"; then
  ln -sf "$VENV/bin/kaggle-mcp" /usr/local/bin/kaggle-mcp
  kaggle-mcp --version
else
  echo "WARNING: kaggle-mcp install from '$KAGGLE_MCP_SOURCE' failed (private repo or no network?)." >&2
  echo "         Sessions on the kaggle-mcp repo fall back to installing from the checkout." >&2
fi
exit 0
