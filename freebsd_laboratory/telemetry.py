from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

try:
    import sentry_sdk
except ImportError:  # pragma: no cover
    sentry_sdk = None  # type: ignore[assignment]


_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
)
_INLINE_SECRET_RE = re.compile(
    r"(?i)(authorization|password|passwd|token|secret|api[_-]?key)([=:]\s*)([^\s,;]+)"
)
_SAFE_EXCEPTION_TYPE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _INLINE_SECRET_RE.sub(r"\1\2[Filtered]", value)
    if isinstance(value, Mapping):
        return {
            key: "[Filtered]" if _is_sensitive_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    del hint
    redacted = _redact(event)
    assert isinstance(redacted, dict)
    return redacted


def init_sentry(component: str) -> bool:
    """Initialize Sentry when SENTRY_DSN is explicitly configured."""
    dsn = os.getenv("SENTRY_DSN")
    if not dsn or sentry_sdk is None:
        return False

    if not sentry_sdk.is_initialized():
        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "lab"),
            release=os.getenv("SENTRY_RELEASE"),
            server_name=os.getenv("SENTRY_SERVER_NAME", "freebsd-laboratory"),
            send_default_pii=False,
            include_local_variables=False,
            include_source_context=False,
            max_request_body_size="never",
            traces_sample_rate=None,
            before_send=_before_send,
        )

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", component)
        scope.set_tag("project", "freebsd-laboratory")
    return True


def capture_exception(
    error: BaseException,
    *,
    component: str,
    operation: str,
) -> str | None:
    """Capture one operational exception with stable component and operation tags."""
    if sentry_sdk is None:
        return None
    if not sentry_sdk.is_initialized() and not init_sentry(component):
        return None

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", component)
        scope.set_tag("operation", operation)
        scope.set_tag("project", "freebsd-laboratory")
        return scope.capture_exception(error)


def capture_kernel_error(
    error_name: object,
    error_value: object,
    *,
    kernel_name: str | None = None,
) -> str | None:
    """Report only a sanitized kernel exception class from the host Jupyter process.

    Notebook source, exception values, rendered traceback lines, kernel IDs,
    guest addresses, and connection metadata are deliberately excluded so
    notebook data and credentials cannot be copied into telemetry by default.
    """
    del error_value, kernel_name
    component = "jupyter-kernel"
    if sentry_sdk is None:
        return None
    if not sentry_sdk.is_initialized() and not init_sentry(component):
        return None

    raw_name = str(error_name or "KernelExecutionError")[:200]
    name = _SAFE_EXCEPTION_TYPE_RE.sub("_", raw_name).strip("._-") or "KernelExecutionError"
    event: dict[str, Any] = {
        "level": "error",
        "exception": {
            "values": [
                {
                    "type": name,
                    "value": "Kernel execution failed",
                    "mechanism": {
                        "type": "jupyter-kernel",
                        "handled": True,
                    },
                }
            ]
        },
    }

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("component", component)
        scope.set_tag("operation", "kernel:execute")
        scope.set_tag("project", "freebsd-laboratory")
        return scope.capture_event(event)


def flush_sentry(timeout: float = 2.0) -> None:
    if sentry_sdk is not None and sentry_sdk.is_initialized():
        sentry_sdk.flush(timeout=timeout)


__all__ = [
    "capture_exception",
    "capture_kernel_error",
    "flush_sentry",
    "init_sentry",
]
