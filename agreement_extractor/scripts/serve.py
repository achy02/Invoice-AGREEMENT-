"""Start the agreement viewer.

    python -m scripts.serve                  http://127.0.0.1:8100
    python -m scripts.serve --port 9000

A different default port from the invoice web app (8000), so both can run at once. The two
are separate applications and share nothing; see this folder's README.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Before paddle is imported anywhere: PaddlePaddle 3.3.1's oneDNN backend fails per
# inference call rather than at startup, so a server started without this looks healthy and
# returns empty pages.
os.environ.setdefault("FLAGS_use_mkldnn", "0")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import config as config_module  # noqa: E402
from src import logging_setup  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    # Deliberately not 8000: that is the invoice app's, and both are meant to run at once.
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--config", type=Path)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging_setup.setup(config.logging.level,
                        config.paths.output / Path(config.logging.file).name)

    import uvicorn

    from web.app import create_app

    print(f"Agreement viewer on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    print(f"  model {config.llm.model} at {config.llm.endpoint}")
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
