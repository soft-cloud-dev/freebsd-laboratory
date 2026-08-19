from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock

from freebsd_laboratory import sentry_diagnostics, telemetry


class _RawSocket:
    def close(self) -> None:
        pass


class _TLSSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        pass

    def version(self) -> str:
        return "TLSv1.3"


class _TLSContext:
    def wrap_socket(self, raw_socket, *, server_hostname: str):
        assert isinstance(raw_socket, _RawSocket)
        assert server_hostname == "o1.ingest.sentry.io"
        return _TLSSocket()


def _mock_network(monkeypatch, *, tls: bool = True) -> None:
    monkeypatch.setattr(
        sentry_diagnostics.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("203.0.113.10", 443 if tls else 80))
        ],
    )
    monkeypatch.setattr(
        sentry_diagnostics.socket,
        "create_connection",
        lambda *args, **kwargs: _RawSocket(),
    )
    if tls:
        monkeypatch.setattr(
            sentry_diagnostics.ssl,
            "create_default_context",
            lambda: _TLSContext(),
        )


def test_parse_sentry_target_drops_credentials() -> None:
    target = sentry_diagnostics.parse_sentry_target(
        "https://public-key:secret-value@o1.ingest.sentry.io/12345"
    )

    assert target.host == "o1.ingest.sentry.io"
    assert target.port == 443
    assert target.use_tls is True
    assert "public-key" not in repr(target)
    assert "secret-value" not in repr(target)


def test_diagnostics_fail_when_sdk_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "sentry_sdk", None)

    results = sentry_diagnostics.run_diagnostics()

    assert [(item.check, item.status) for item in results] == [("sdk", "FAIL")]


def test_diagnostics_fail_when_dsn_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "sentry_sdk", Mock())
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    results = sentry_diagnostics.run_diagnostics()

    assert [(item.check, item.status) for item in results] == [
        ("sdk", "PASS"),
        ("dsn", "FAIL"),
    ]


def test_read_only_diagnostics_cover_dns_tcp_and_tls(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "sentry_sdk", Mock())
    monkeypatch.setenv(
        "SENTRY_DSN",
        "https://public-key@o1.ingest.sentry.io/12345",
    )
    _mock_network(monkeypatch)

    results = sentry_diagnostics.run_diagnostics()

    statuses = {item.check: item.status for item in results}
    assert statuses == {
        "sdk": "PASS",
        "dsn": "PASS",
        "proxy": "INFO",
        "dns": "PASS",
        "tcp": "PASS",
        "tls": "PASS",
        "event": "SKIP",
    }
    rendered = "\n".join(item.detail for item in results)
    assert "public-key" not in rendered


def test_send_test_event_initializes_debug_and_flushes(monkeypatch) -> None:
    scope = Mock()
    scope.capture_message.return_value = "event-id-123"
    sdk = Mock()
    sdk.new_scope.return_value = nullcontext(scope)
    monkeypatch.setattr(telemetry, "sentry_sdk", sdk)
    monkeypatch.setenv(
        "SENTRY_DSN",
        "http://public-key@o1.ingest.sentry.io/12345",
    )
    _mock_network(monkeypatch, tls=False)

    init = Mock(return_value=True)
    flush = Mock()
    monkeypatch.setattr(telemetry, "init_sentry", init)
    monkeypatch.setattr(telemetry, "flush_sentry", flush)

    results = sentry_diagnostics.run_diagnostics(
        send_test_event=True,
        sdk_debug=True,
        timeout=7.0,
    )

    assert results[-1].status == "PASS"
    assert "event-id-123" in results[-1].detail
    init.assert_called_once_with("sentry-diagnostics", debug=True)
    flush.assert_called_once_with(timeout=7.0)
    scope.set_tag.assert_any_call("diagnostic", "true")
