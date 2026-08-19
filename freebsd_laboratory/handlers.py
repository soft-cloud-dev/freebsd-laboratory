from __future__ import annotations

import json
from typing import Any

from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin
from tornado import web

from .service import (
    EvidenceEventLimitReached,
    EvidencePayloadTooLarge,
    LabService,
)


SERVICE_SETTINGS_KEY = "freebsd_laboratory_service"


class LaboratoryHandler(ExtensionHandlerMixin, APIHandler):
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
        except EvidencePayloadTooLarge as error:
            raise web.HTTPError(413, str(error)) from error
        except EvidenceEventLimitReached as error:
            raise web.HTTPError(429, str(error)) from error
        except ValueError as error:
            raise web.HTTPError(400, str(error)) from error

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
