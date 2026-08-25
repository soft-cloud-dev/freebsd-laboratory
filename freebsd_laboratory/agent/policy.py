from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    authorized: bool
    reason: str = ""


class AgentPolicy:
    """Validates and constrains model-proposed actions using structural boundaries."""

    def __init__(
        self,
        max_command_bytes: int = 4096,
        max_steps: int = 16,
        max_runtime_seconds: int = 300,
    ) -> None:
        if (
            isinstance(max_command_bytes, bool)
            or not isinstance(max_command_bytes, int)
            or max_command_bytes <= 0
        ):
            raise ValueError("max_command_bytes must be a positive integer")
        if isinstance(max_steps, bool) or not isinstance(max_steps, int):
            raise ValueError("max_steps must be an integer")
        if max_steps < 1 or max_steps > 25:
            raise ValueError("max_steps must be between 1 and 25")
        if (
            isinstance(max_runtime_seconds, bool)
            or not isinstance(max_runtime_seconds, (int, float))
            or max_runtime_seconds <= 0
        ):
            raise ValueError("max_runtime_seconds must be a positive number")

        self.max_command_bytes = max_command_bytes
        self.max_steps = max_steps
        self.max_runtime_seconds = float(max_runtime_seconds)

    def authorize(self, command: str, step: int, elapsed: float) -> Decision:
        if isinstance(step, bool) or not isinstance(step, int):
            return Decision(False, "invalid step type")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            return Decision(False, "invalid elapsed time type")
        if not isinstance(command, str) or not command.strip():
            return Decision(False, "empty command")

        encoded_len = len(command.encode("utf-8"))
        if encoded_len > self.max_command_bytes:
            return Decision(
                False,
                f"command length ({encoded_len} bytes) exceeds limit ({self.max_command_bytes} bytes)",
            )
        if step >= self.max_steps:
            return Decision(False, f"step limit ({self.max_steps}) reached")
        if elapsed >= self.max_runtime_seconds:
            return Decision(
                False,
                f"session deadline ({self.max_runtime_seconds}s) reached",
            )
        return Decision(True)
