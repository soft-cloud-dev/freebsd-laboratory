from __future__ import annotations

from .runtime_daemon import main as runtime_daemon_main
from .telemetry import capture_exception, flush_sentry, init_sentry


def main() -> None:
    init_sentry("runtime-daemon")
    try:
        runtime_daemon_main()
    except KeyboardInterrupt:
        raise
    except BaseException as error:
        capture_exception(
            error,
            component="runtime-daemon",
            operation="runtime-daemon:main",
        )
        flush_sentry()
        raise


if __name__ == "__main__":
    main()
