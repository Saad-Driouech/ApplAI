"""stdio entrypoint: `applai-mcp` or `python -m applai_mcp`."""
from __future__ import annotations

import logging
import sys

from pydantic import ValidationError

from applai_mcp.config import ENV_FILE, Settings
from applai_mcp.server import create_server


def main() -> None:
    # stdout carries the protocol; all logging goes to stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(e["loc"][0]).upper() for e in exc.errors())
        sys.exit(f"applai-mcp: invalid or missing settings ({missing}); set them in {ENV_FILE} or the environment")
    create_server(settings).run()


if __name__ == "__main__":
    main()
