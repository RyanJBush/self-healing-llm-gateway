"""Run: python -m gateway [--port 8000]. Localhost only, one process, one worker."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys

import uvicorn

from .factory import create_default_app


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if not _is_loopback(args.host):
        print(
            "refusing to bind a non-loopback host: this gateway has no authentication "
            "and must not be exposed publicly",
            file=sys.stderr,
        )
        return 2

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    events = logging.getLogger("gateway.events")
    events.addHandler(handler)
    events.setLevel(logging.INFO)
    events.propagate = False

    uvicorn.run(
        create_default_app(),
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
