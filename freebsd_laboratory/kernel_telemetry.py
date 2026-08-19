from __future__ import annotations

from collections import deque
from typing import Any

from jupyter_server.services.kernels.connection.channels import ZMQChannelsWebsocketConnection

from .telemetry import capture_kernel_error


def _kernel_error_values(
    header: object,
    content: object,
) -> tuple[object, object] | None:
    if not isinstance(header, dict) or header.get("msg_type") != "error":
        return None
    if not isinstance(content, dict):
        return None
    return content.get("ename"), content.get("evalue")


class SentryKernelWebsocketConnection(ZMQChannelsWebsocketConnection):
    """Capture kernel execution errors in the host-side Jupyter process."""

    _recent_error_ids: deque[str] = deque(maxlen=1024)
    _recent_error_id_set: set[str] = set()

    @classmethod
    def _first_observation(cls, message_id: object) -> bool:
        if not isinstance(message_id, str) or not message_id:
            return True
        if message_id in cls._recent_error_id_set:
            return False
        if len(cls._recent_error_ids) == cls._recent_error_ids.maxlen:
            expired = cls._recent_error_ids.popleft()
            cls._recent_error_id_set.discard(expired)
        cls._recent_error_ids.append(message_id)
        cls._recent_error_id_set.add(message_id)
        return True

    def _on_error(self, channel: str | None, msg: dict[str, Any], msg_list: list[Any]) -> None:
        # Preserve Jupyter Server's native error/traceback policy first. If
        # allow_tracebacks=False, the content we inspect below is already masked.
        super()._on_error(channel, msg, msg_list)

        if channel != "iopub":
            return

        header = self.get_part("header", msg.get("header"), msg_list)
        content = self.get_part("content", msg.get("content"), msg_list)
        values = _kernel_error_values(header, content)
        if values is None:
            return
        if not self._first_observation(header.get("msg_id")):
            return

        kernel_name = getattr(self.kernel_manager, "kernel_name", None)
        capture_kernel_error(
            values[0],
            values[1],
            kernel_name=kernel_name if isinstance(kernel_name, str) else None,
        )


__all__ = ["SentryKernelWebsocketConnection"]
