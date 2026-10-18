"""CLI: `autobench serve` (web API + MCP over HTTP) or `autobench mcp-stdio` (MCP over stdio)."""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="autobench")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="Run the API, WebSocket and MCP (/mcp) server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    stdio = sub.add_parser("mcp-stdio", help="Run the MCP server over stdio (for clients without HTTP support)")
    stdio.add_argument("--relay", default=None,
                       help="Base URL of a running `autobench serve` to forward chart events to (default from settings)")
    args = parser.parse_args(argv)

    from .config import get_settings

    settings = get_settings()
    if args.cmd in (None, "serve"):
        import uvicorn

        logging.basicConfig(level=logging.INFO)
        uvicorn.run("autobench.app:create_app", factory=True, host=getattr(args, "host", None) or settings.autobench_host,
                    port=getattr(args, "port", None) or settings.autobench_port, reload=getattr(args, "reload", False))
    elif args.cmd == "mcp-stdio":
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the protocol channel
        from . import db
        from .mcp_server import mcp
        from .services.events import enable_http_relay

        db.init_db()
        enable_http_relay(args.relay or f"http://{settings.autobench_host}:{settings.autobench_port}")
        mcp.run("stdio")


if __name__ == "__main__":
    main()
