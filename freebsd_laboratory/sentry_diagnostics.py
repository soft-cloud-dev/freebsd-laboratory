from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import socket
import ssl
import time
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from . import telemetry

_DNS_ATTEMPTS = 3
_DNS_RETRY_DELAY = 0.2


@dataclass(frozen=True)
class SentryTarget:
    host: str
    port: int
    use_tls: bool


@dataclass(frozen=True)
class DiagnosticResult:
    check: str
    status: str
    detail: str


class DiagnosticFailure(RuntimeError):
    pass


def _result(check: str, status: str, detail: str) -> DiagnosticResult:
    return DiagnosticResult(check=check, status=status, detail=detail)


def parse_sentry_target(dsn: str) -> SentryTarget:
    """Parse only the network destination from a DSN without exposing credentials."""
    try:
        parsed = urlsplit(dsn)
        port = parsed.port
    except ValueError as error:
        raise DiagnosticFailure(f"invalid SENTRY_DSN: {error}") from error

    if parsed.scheme not in {"http", "https"}:
        raise DiagnosticFailure("SENTRY_DSN must use http or https")
    if not parsed.hostname:
        raise DiagnosticFailure("SENTRY_DSN does not contain an ingest hostname")
    if not parsed.username:
        raise DiagnosticFailure("SENTRY_DSN does not contain a public key")
    if not parsed.path.strip("/"):
        raise DiagnosticFailure("SENTRY_DSN does not contain a project path")

    return SentryTarget(
        host=parsed.hostname,
        port=port or (443 if parsed.scheme == "https" else 80),
        use_tls=parsed.scheme == "https",
    )


def _sdk_version() -> str:
    try:
        return importlib.metadata.version("sentry-sdk")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _proxy_detail() -> str:
    names = [
        name
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
        if os.getenv(name)
    ]
    if not names:
        return "no proxy environment variables are set"
    return "proxy environment variables set: " + ", ".join(names)


def _resolve_target(target: SentryTarget) -> tuple[list[tuple], int]:
    """Resolve through Python/libc, tolerating a short-lived resolver miss."""
    last_error: OSError | None = None
    retryable_errors = {
        code
        for code in (
            getattr(socket, "EAI_AGAIN", None),
            getattr(socket, "EAI_NONAME", None),
        )
        if code is not None
    }

    for attempt in range(1, _DNS_ATTEMPTS + 1):
        try:
            addresses = socket.getaddrinfo(
                target.host,
                target.port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            if addresses:
                return addresses, attempt
            last_error = socket.gaierror(
                getattr(socket, "EAI_NONAME", 8),
                "resolver returned no addresses",
            )
        except OSError as error:
            last_error = error

        if (
            attempt < _DNS_ATTEMPTS
            and getattr(last_error, "errno", None) in retryable_errors
        ):
            time.sleep(_DNS_RETRY_DELAY)
            continue
        break

    assert last_error is not None
    raise last_error


def _connect_resolved(addresses: list[tuple], *, timeout: float) -> socket.socket:
    """Connect to an already-resolved address without triggering a second DNS lookup."""
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        raw_socket = socket.socket(family, socktype, proto)
        raw_socket.settimeout(timeout)
        try:
            raw_socket.connect(sockaddr)
            return raw_socket
        except OSError as error:
            last_error = error
            raw_socket.close()

    if last_error is None:
        raise OSError("resolver returned no connectable addresses")
    raise last_error


def run_diagnostics(
    *,
    send_test_event: bool = False,
    sdk_debug: bool = False,
    timeout: float = 5.0,
) -> list[DiagnosticResult]:
    results: list[DiagnosticResult] = []

    if telemetry.sentry_sdk is None:
        return [
            _result(
                "sdk",
                "FAIL",
                "sentry-sdk is not importable in this Python environment",
            )
        ]
    results.append(_result("sdk", "PASS", f"sentry-sdk {_sdk_version()} is importable"))

    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        results.append(_result("dsn", "FAIL", "SENTRY_DSN is not set"))
        return results

    try:
        target = parse_sentry_target(dsn)
    except DiagnosticFailure as error:
        results.append(_result("dsn", "FAIL", str(error)))
        return results

    results.append(
        _result(
            "dsn",
            "PASS",
            f"configured for {target.host}:{target.port}; credentials are hidden",
        )
    )
    results.append(_result("proxy", "INFO", _proxy_detail()))

    try:
        addresses, attempts = _resolve_target(target)
    except OSError as error:
        results.append(
            _result(
                "dns",
                "FAIL",
                f"resolver error after {_DNS_ATTEMPTS} attempts: {error}",
            )
        )
        return results

    unique_addresses = sorted({entry[4][0] for entry in addresses})
    attempt_detail = "" if attempts == 1 else f" after {attempts} attempts"
    results.append(
        _result(
            "dns",
            "PASS",
            (
                f"resolved {target.host} to {len(unique_addresses)} address(es)"
                f"{attempt_detail}"
            ),
        )
    )

    try:
        raw_socket = _connect_resolved(addresses, timeout=timeout)
    except OSError as error:
        results.append(_result("tcp", "FAIL", f"connection error: {error}"))
        return results

    if not target.use_tls:
        raw_socket.close()
        results.append(_result("tcp", "PASS", "TCP connection succeeded"))
    else:
        results.append(_result("tcp", "PASS", "TCP connection succeeded"))
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            with context.wrap_socket(raw_socket, server_hostname=target.host) as tls_socket:
                tls_version = tls_socket.version() or "unknown TLS version"
        except (OSError, ssl.SSLError) as error:
            raw_socket.close()
            results.append(_result("tls", "FAIL", f"TLS verification error: {error}"))
            return results
        results.append(
            _result("tls", "PASS", f"certificate verification succeeded ({tls_version})")
        )

    if not send_test_event:
        results.append(
            _result(
                "event",
                "SKIP",
                "use --send-test-event to enqueue and flush a diagnostic event",
            )
        )
        return results

    initialized = telemetry.init_sentry("sentry-diagnostics", debug=sdk_debug)
    if not initialized:
        results.append(_result("event", "FAIL", "Sentry SDK initialization failed"))
        return results

    assert telemetry.sentry_sdk is not None
    with telemetry.sentry_sdk.new_scope() as scope:
        scope.set_tag("component", "sentry-diagnostics")
        scope.set_tag("project", "freebsd-laboratory")
        scope.set_tag("diagnostic", "true")
        event_id = scope.capture_event(
            {
                "level": "error",
                "message": "FreeBSD Laboratory Sentry diagnostic test event",
            }
        )

    telemetry.flush_sentry(timeout=max(timeout, 2.0))
    if event_id is None:
        results.append(_result("event", "FAIL", "SDK did not return an event ID"))
    else:
        results.append(
            _result(
                "event",
                "PASS",
                f"event queued and flushed; event_id={event_id}",
            )
        )
    return results


def _exit_code(results: list[DiagnosticResult]) -> int:
    return 1 if any(result.status == "FAIL" for result in results) else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diagnose host-side Sentry delivery without printing DSN credentials.",
    )
    parser.add_argument(
        "--send-test-event",
        action="store_true",
        help="enqueue one diagnostic error event and flush the SDK transport",
    )
    parser.add_argument(
        "--sdk-debug",
        action="store_true",
        help="enable sentry-sdk internal debug logging when initializing the SDK",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="network and flush timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")

    results = run_diagnostics(
        send_test_event=args.send_test_event,
        sdk_debug=args.sdk_debug,
        timeout=args.timeout,
    )
    if args.json:
        print(json.dumps([asdict(result) for result in results], sort_keys=True))
    else:
        for result in results:
            print(f"[{result.status}] {result.check}: {result.detail}")
    raise SystemExit(_exit_code(results))


if __name__ == "__main__":
    main()
