from __future__ import annotations

import json
from typing import Any

from jupyter_server.base.handlers import JupyterHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin
from tornado import web

from .service import LabService


SERVICE_SETTINGS_KEY = "freebsd_laboratory_service"


class LaboratoryHandler(ExtensionHandlerMixin, JupyterHandler):
    @property
    def service(self) -> LabService:
        service = self.settings.get(SERVICE_SETTINGS_KEY)
        if not isinstance(service, LabService):
            raise RuntimeError("FreeBSD Laboratory service is not initialized")
        return service

    def finish_json(self, value: Any, status: int = 200) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps(value, sort_keys=True))


class StateHandler(LaboratoryHandler):
    @web.authenticated
    def get(self) -> None:
        self.finish_json(self.service.state())


class EventHandler(LaboratoryHandler):
    @web.authenticated
    def post(self) -> None:
        document = self.get_json_body()
        if not isinstance(document, dict):
            raise web.HTTPError(400, "JSON object required")

        kind = document.get("kind")
        payload = document.get("payload", {})
        if not isinstance(kind, str) or not kind:
            raise web.HTTPError(400, "Event kind is required")
        if not isinstance(payload, dict):
            raise web.HTTPError(400, "Event payload must be an object")

        try:
            event = self.service.record_client_event(kind, payload)
        except ValueError as exc:
            raise web.HTTPError(400, str(exc)) from exc

        self.finish_json(
            {
                "accepted": True,
                "sequence": event.sequence,
                "state": self.service.state(),
            },
            status=201,
        )


class ExportHandler(LaboratoryHandler):
    @web.authenticated
    def post(self) -> None:
        self.finish_json(self.service.export())
