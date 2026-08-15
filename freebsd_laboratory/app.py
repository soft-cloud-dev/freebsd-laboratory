from __future__ import annotations

from pathlib import Path

from jupyter_server.extension.application import ExtensionApp
from traitlets import Unicode

from .handlers import ExportHandler, EventHandler, SERVICE_SETTINGS_KEY, StateHandler
from .service import LabService


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

    def initialize_settings(self) -> None:
        assert self.serverapp is not None
        service = LabService(
            root_dir=Path(self.serverapp.root_dir),
            lab_path=self.lab_path,
            evidence_dir=self.evidence_dir,
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
            ]
        )
