from __future__ import annotations

__version__ = "0.1.0"


def _jupyter_server_extension_points() -> list[dict[str, object]]:
    from .app import FreeBSDLaboratoryApp

    return [
        {
            "module": "freebsd_laboratory",
            "app": FreeBSDLaboratoryApp,
        }
    ]
