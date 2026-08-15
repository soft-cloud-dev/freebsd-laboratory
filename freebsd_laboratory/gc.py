from __future__ import annotations

import argparse
import json

from .runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile stale FreeBSD Laboratory runtimes")
    parser.add_argument("--socket", default=DEFAULT_RUNTIME_SOCKET)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Destroy every laboratory runtime, including runtimes whose owner PID is still alive.",
    )
    args = parser.parse_args()

    result = RuntimeClient(args.socket).gc(stale_only=not args.all)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
