"""Local audit entry point; no implicit installation or external publication."""
from __future__ import annotations

import argparse
from pathlib import Path

from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="meow LLM Detector")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--locale", choices=["zh-CN", "en"], default="zh-CN")
    args = parser.parse_args()
    server = create_server(port=args.port, runs_root=args.data_root, locale=args.locale)
    print(f"meow LLM Detector: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
