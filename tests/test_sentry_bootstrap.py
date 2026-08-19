from __future__ import annotations

from pathlib import Path


BOOTSTRAP = Path("deploy/freebsd/bootstrap.sh")
PYPROJECT = Path("pyproject.toml")


def test_bootstrap_installs_native_sentry_sdk() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert '"${PY_TAG}-sentry-sdk"' in text
    daemon_import = (
        '"$LAB_DAEMON_VENV/bin/python" -c '
        "'import freebsd_laboratory, sentry_sdk'"
    )
    assert daemon_import in text
    assert "jupyterlab, sentry_sdk" in text


def test_project_exposes_sentry_diagnostics_command() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")

    assert (
        'freebsd-lab-sentry-diagnose = "freebsd_laboratory.sentry_diagnostics:main"'
        in text
    )
