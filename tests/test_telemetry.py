from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import Mock

from freebsd_laboratory import telemetry


def test_init_sentry_is_disabled_without_dsn(monkeypatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    assert telemetry.init_sentry("test") is False


def test_init_sentry_disables_default_pii(monkeypatch) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "test")
    monkeypatch.setenv("SENTRY_RELEASE", "deadbeef")

    init = Mock()
    scope = Mock()
    monkeypatch.setattr(telemetry.sentry_sdk, "is_initialized", lambda: False)
    monkeypatch.setattr(telemetry.sentry_sdk, "init", init)
    monkeypatch.setattr(
        telemetry.sentry_sdk,
        "new_scope",
        lambda: nullcontext(scope),
    )

    assert telemetry.init_sentry("runtime-daemon") is True

    kwargs = init.call_args.kwargs
    assert kwargs["dsn"] == "https://public@example.invalid/1"
    assert kwargs["environment"] == "test"
    assert kwargs["release"] == "deadbeef"
    assert kwargs["send_default_pii"] is False
    assert kwargs["traces_sample_rate"] is None
    assert kwargs["before_send"] is telemetry._before_send


def test_before_send_filters_credentials() -> None:
    event = {
        "user": {
            "email": "operator@example.invalid",
            "ip_address": "192.0.2.10",
        },
        "request": {
            "headers": {
                "Authorization": "Bearer secret-value",
                "User-Agent": "FreeBSD-Laboratory-Test",
            },
        },
        "extra": {
            "message": "password=hunter2 command failed",
        },
    }

    result = telemetry._before_send(event, {})

    assert result["user"]["email"] == "operator@example.invalid"
    assert result["user"]["ip_address"] == "192.0.2.10"
    assert result["request"]["headers"]["Authorization"] == "[Filtered]"
    assert result["request"]["headers"]["User-Agent"] == "FreeBSD-Laboratory-Test"
    assert result["extra"]["message"] == "password=[Filtered] command failed"


def test_capture_kernel_error_sends_only_sanitized_class(monkeypatch) -> None:
    scope = Mock()
    scope.capture_event.return_value = "event-id"
    monkeypatch.setattr(telemetry.sentry_sdk, "is_initialized", lambda: True)
    monkeypatch.setattr(
        telemetry.sentry_sdk,
        "new_scope",
        lambda: nullcontext(scope),
    )

    result = telemetry.capture_kernel_error(
        "ValueError\nAuthorization=secret-token",
        "password=hunter2 notebook=/home/operator/private.ipynb",
        kernel_name="private-user-kernel",
    )

    assert result == "event-id"
    event = scope.capture_event.call_args.args[0]
    exception = event["exception"]["values"][0]
    assert event["level"] == "error"
    assert exception["type"] == "KernelExecutionError"
    assert exception["value"] == "Kernel execution failed"
    assert "secret-token" not in str(event)
    assert "hunter2" not in str(event)
    assert "private.ipynb" not in str(event)
    assert "traceback" not in event
    scope.set_tag.assert_any_call("component", "jupyter-kernel")
    scope.set_tag.assert_any_call("operation", "kernel:execute")
    assert all(call.args[0] != "kernel_name" for call in scope.set_tag.call_args_list)


def test_capture_kernel_error_preserves_valid_exception_class(monkeypatch) -> None:
    scope = Mock()
    scope.capture_event.return_value = "event-id"
    monkeypatch.setattr(telemetry.sentry_sdk, "is_initialized", lambda: True)
    monkeypatch.setattr(
        telemetry.sentry_sdk,
        "new_scope",
        lambda: nullcontext(scope),
    )

    telemetry.capture_kernel_error("ZeroDivisionError", "division by zero")

    event = scope.capture_event.call_args.args[0]
    assert event["exception"]["values"][0]["type"] == "ZeroDivisionError"


def test_capture_kernel_error_is_disabled_without_dsn(monkeypatch) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(telemetry.sentry_sdk, "is_initialized", lambda: False)

    assert telemetry.capture_kernel_error("ValueError", "bad value") is None
