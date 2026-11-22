"""Command-line entry point.

    kaggle-mcp                       # stdio (Claude Desktop / Claude Code / local clients)
    kaggle-mcp --transport http      # Streamable HTTP on 127.0.0.1:8000/mcp (ChatGPT, remote)
    kaggle-mcp generate-token        # print a random token for KAGGLE_MCP_AUTH_TOKEN
"""

from __future__ import annotations

import argparse
import copy
import logging
import os
import secrets
import sys

from kaggle_mcp import __version__

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _uvicorn_log_config() -> dict:
    """uvicorn's default logging config plus a filter that redacts secret-path
    tokens (/s/<token>/...) from access-log lines."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["redact_path_token"] = {"()": "kaggle_mcp.http_auth.RedactPathTokenFilter"}
    for handler in cfg.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact_path_token")
    return cfg


def _http(args: argparse.Namespace) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from kaggle_mcp.http_auth import MIN_TOKEN_LENGTH, BearerAuthMiddleware
    from kaggle_mcp.server import build_server

    token = os.environ.get("KAGGLE_MCP_AUTH_TOKEN", "")
    if token and len(token) < MIN_TOKEN_LENGTH:
        sys.exit(f"KAGGLE_MCP_AUTH_TOKEN must be at least {MIN_TOKEN_LENGTH} characters (try: kaggle-mcp generate-token)")
    if not token:
        if args.host not in LOCAL_HOSTS:
            sys.exit(
                "Refusing to serve HTTP on a non-local interface without authentication.\n"
                "Set KAGGLE_MCP_AUTH_TOKEN (generate one with: kaggle-mcp generate-token)."
            )
        if not args.insecure_no_auth:
            sys.exit(
                "KAGGLE_MCP_AUTH_TOKEN is not set. Set it, or pass --insecure-no-auth to run "
                "unauthenticated on localhost only (not recommended)."
            )
        logging.warning("HTTP transport running WITHOUT authentication on %s", args.host)

    if args.host not in LOCAL_HOSTS and not args.behind_proxy:
        logging.warning(
            "Serving on %s without --behind-proxy: if a reverse proxy is in front, every client shares "
            "its IP for failed-auth throttling", args.host
        )

    allowed_hosts = [h.strip() for h in os.environ.get("KAGGLE_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = None
    if allowed_hosts:
        # DNS-rebinding protection: only accept these Host headers (e.g. "mcp.example.com").
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts] + ["https://chatgpt.com", "https://claude.ai"],
        )
    elif args.host not in LOCAL_HOSTS:
        logging.warning("KAGGLE_MCP_ALLOWED_HOSTS not set: Host header validation is disabled")
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    mcp = build_server()
    asgi = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=args.stateless,
        transport_security=security,
        host=args.host,
    )
    if token:
        allow_path = os.environ.get("KAGGLE_MCP_ALLOW_PATH_TOKEN", "").lower() in {"1", "true", "yes", "on"}
        asgi = BearerAuthMiddleware(asgi, token, allow_path_token=allow_path)
        if allow_path:
            logging.warning("Secret-path auth enabled: the endpoint URL itself is a credential; keep it private")

    uvicorn.run(
        asgi,
        host=args.host,
        port=args.port,
        log_level="info",
        log_config=_uvicorn_log_config(),
        proxy_headers=args.behind_proxy,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1") if args.behind_proxy else None,
        server_header=False,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kaggle-mcp", description="Kaggle MCP server")
    parser.add_argument("--version", action="version", version=f"kaggle-mcp {__version__}")
    parser.add_argument("command", nargs="?", choices=["serve", "generate-token"], default="serve")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("KAGGLE_MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("KAGGLE_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KAGGLE_MCP_PORT", "8000")))
    parser.add_argument("--stateless", action="store_true", help="Stateless HTTP (no session affinity needed)")
    parser.add_argument("--behind-proxy", action="store_true", help="Trust X-Forwarded-* from FORWARDED_ALLOW_IPS")
    parser.add_argument("--insecure-no-auth", action="store_true", help="Allow unauthenticated HTTP on localhost")
    args = parser.parse_args(argv)

    # Logs go to stderr: stdout is the MCP channel in stdio mode.
    logging.basicConfig(level=os.environ.get("KAGGLE_MCP_LOG_LEVEL", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log request URLs/headers verbosely

    if args.command == "generate-token":
        print(secrets.token_urlsafe(48))
        return

    if args.transport == "http":
        _http(args)
    else:
        from kaggle_mcp.server import build_server

        build_server().run("stdio")


if __name__ == "__main__":
    main()
