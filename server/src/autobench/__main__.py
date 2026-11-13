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
    up = sub.add_parser("state-upload", help="One-off: copy the state of a local SQLite database to Firestore "
                                              "(AUTOBENCH_STATE_PROJECT); market-data caches are not copied")
    up.add_argument("--from", dest="source", required=True, help="Path of the local autobench.db")
    up.add_argument("--yes", action="store_true", help="Write (otherwise only count what would be copied)")
    args = parser.parse_args(argv)

    from .config import load_credentials_file, get_settings

    load_credentials_file()  # Cloud Run: the autobench-credentials secret, mounted as a file
    settings = get_settings()
    if args.cmd in (None, "serve"):
        import os

        import uvicorn

        if getattr(args, "port", None):  # the app reads its own port (e.g. for the workspace .mcp.json)
            os.environ["AUTOBENCH_PORT"] = str(args.port)
            get_settings.cache_clear()
            settings = get_settings()

        logging.basicConfig(level=logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream request is noise
        uvicorn.run("autobench.app:create_app", factory=True, host=getattr(args, "host", None) or settings.autobench_host,
                    port=getattr(args, "port", None) or settings.autobench_port, reload=getattr(args, "reload", False))
    elif args.cmd == "mcp-stdio":
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is the protocol channel
        from . import db
        from .mcp_server import mcp
        from .services.events import enable_http_relay

        from .services.trading import trading

        trading.enabled_here = False  # only `autobench serve` may hold the broker connection
        db.init_db()
        enable_http_relay(args.relay or f"http://{settings.autobench_host}:{settings.autobench_port}")
        mcp.run("stdio")

    elif args.cmd == "state-upload":
        from sqlalchemy import create_engine, func, select

        from . import db, state_store

        if not settings.autobench_state_project:
            sys.exit("Set AUTOBENCH_STATE_PROJECT (e.g. vral-altos) first.")
        engine = create_engine(f"sqlite:///{args.source}")
        db.Base.metadata.create_all(engine)  # an older database may lack newer tables
        with engine.connect() as conn:
            counts = {n: conn.execute(select(func.count()).select_from(t)).scalar() for n, t in db.mirrored_tables().items()}
        print(f"{settings.autobench_state_project}/{settings.autobench_state_database}:", counts)
        if args.yes:
            mirror = state_store.Mirror(settings.autobench_state_project, settings.autobench_state_database,
                                        db.mirrored_tables())
            mirror.upload_all(engine)
            print("uploaded" if not mirror.backlog else f"FAILED: {mirror.last_error}")


if __name__ == "__main__":
    main()
