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


def _load_jupyter_server_extension(server_app: object) -> None:
    pass


def load_ipython_extension(ipython: object) -> None:
    """Auto-register magics when loaded via %load_ext freebsd_laboratory."""
    from .magics import FreeBSDLabMagics

    ipython.register_magics(FreeBSDLabMagics)
