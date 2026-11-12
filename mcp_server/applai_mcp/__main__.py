"""Entrypoint: `applai-mcp [--transport stdio|http]` or `python -m applai_mcp`."""
from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from applai_mcp.config import ENV_FILE, HttpSettings, Settings
from applai_mcp.server import create_server, http_app


def _load[T: BaseSettings](cls: type[T]) -> T:
    try:
        return cls()
    except ValidationError as exc:
        missing = ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        sys.exit(f"applai-mcp: invalid or missing settings ({missing}); set them in {ENV_FILE} or the environment")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="applai-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"),
                        help="stdio for Claude Desktop (default), http for the remote connector")
    args = parser.parse_args(argv)
    if args.transport not in ("stdio", "http"):  # an invalid MCP_TRANSPORT bypasses argparse's choices
        parser.error(f"MCP_TRANSPORT must be stdio or http, not {args.transport!r}")
    # stdout carries the stdio protocol; all logging goes to stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = _load(Settings)
    if args.transport == "stdio":
        create_server(settings).run()
        return
    http = _load(HttpSettings)
    app = http_app(create_server(settings, http=http), http)
    # Nothing depends on the client address or scheme, so proxy headers are not trusted
    uvicorn.run(app, host=http.http_host, port=http.http_port, proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
