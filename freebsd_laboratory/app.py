from __future__ import annotations

import platform
from pathlib import Path

from jupyter_server.extension.application import ExtensionApp
from traitlets import Bool, Int, Unicode

from .handlers import (
    AIAgentHandler,
    AIGenerateHandler,
    AIModelsHandler,
    EventHandler,
    ExportHandler,
    SERVICE_SETTINGS_KEY,
    StateHandler,
)
from .kernel_telemetry import SentryKernelWebsocketConnection
from .runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient, RuntimeControlError
from .service import LabService
from .telemetry import init_sentry


class FreeBSDLaboratoryApp(ExtensionApp):
    name = "freebsd_laboratory"
    extension_url = "/freebsd-lab"
    load_other_extensions = True

    lab_path = Unicode(
        "lab.yaml",
        help="Laboratory definition relative to the Jupyter Server root.",
    ).tag(config=True)
    evidence_dir = Unicode(
        ".freebsd-lab/evidence",
        help="Directory used for server-owned evidence sessions.",
    ).tag(config=True)
    max_evidence_events = Int(
        10_000,
        min=1,
        help="Maximum number of evidence events retained in one server session.",
    ).tag(config=True)
    max_event_payload_bytes = Int(
        1024 * 1024,
        min=1024,
        help="Maximum canonical JSON size of one redacted evidence payload.",
    ).tag(config=True)
    fsync_evidence_events = Bool(
        True,
        help="Flush every accepted JSONL evidence event to stable storage.",
    ).tag(config=True)
    runtime_socket = Unicode(
        DEFAULT_RUNTIME_SOCKET,
        help="Unix-domain socket exposed by freebsd-lab-runtime-daemon.",
    ).tag(config=True)
    reconcile_runtimes_on_start = Bool(
        True,
        help="Ask the runtime daemon to remove stale runtimes.",
    ).tag(config=True)

    def initialize_settings(self) -> None:
        init_sentry("jupyter-server")
        # Kernel runtimes remain network-isolated. Their IOPub error messages
        # already pass through the host Jupyter Server, so capture errors here
        # instead of granting the jail/VM outbound access to sentry.io.
        self.settings["kernel_websocket_connection_class"] = SentryKernelWebsocketConnection

        assert self.serverapp is not None
        if self.reconcile_runtimes_on_start and platform.system() == "FreeBSD":
            try:
                result = RuntimeClient(self.runtime_socket).gc(stale_only=True)
                cleaned = result.get("cleaned", [])
                if cleaned:
                    self.log.warning(
                        "FreeBSD Laboratory reconciled stale runtimes: %s",
                        ", ".join(str(item) for item in cleaned),
                    )
            except RuntimeControlError as error:
                self.log.warning(
                    "FreeBSD Laboratory runtime reconciliation skipped: %s",
                    error,
                )

        service = LabService(
            root_dir=Path(self.serverapp.root_dir),
            lab_path=self.lab_path,
            evidence_dir=self.evidence_dir,
            max_events=self.max_evidence_events,
            max_event_payload_bytes=self.max_event_payload_bytes,
            fsync_events=self.fsync_evidence_events,
        )
        self.settings[SERVICE_SETTINGS_KEY] = service
        self.log.info(
            "FreeBSD Laboratory session %s initialized for %s",
            service.session_id,
            service.spec["id"],
        )

    def initialize_handlers(self) -> None:
        self.handlers.extend(
            [
                (r"/freebsd-lab/api/state", StateHandler),
                (r"/freebsd-lab/api/events", EventHandler),
                (r"/freebsd-lab/api/export", ExportHandler),
                (r"/freebsd-lab/api/ai/models", AIModelsHandler),
                (r"/freebsd-lab/api/ai/generate", AIGenerateHandler),
                (r"/freebsd-lab/api/ai/agent", AIAgentHandler),
            ]
        )
