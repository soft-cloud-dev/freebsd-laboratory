from __future__ import annotations

from freebsd_laboratory.kernel_telemetry import (
    SentryKernelWebsocketConnection,
    _kernel_error_values,
)


def test_kernel_error_values_accepts_only_error_messages() -> None:
    assert _kernel_error_values(
        {"msg_type": "error"},
        {"ename": "ZeroDivisionError", "evalue": "division by zero"},
    ) == ("ZeroDivisionError", "division by zero")

    assert _kernel_error_values(
        {"msg_type": "stream"},
        {"ename": "ZeroDivisionError", "evalue": "division by zero"},
    ) is None


def test_kernel_error_deduplicates_websocket_observations() -> None:
    cls = SentryKernelWebsocketConnection
    cls._recent_error_ids.clear()
    cls._recent_error_id_set.clear()

    assert cls._first_observation("message-1") is True
    assert cls._first_observation("message-1") is False
    assert cls._first_observation("message-2") is True
