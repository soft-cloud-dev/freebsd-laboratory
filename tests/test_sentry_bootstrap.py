from __future__ import annotations

from pathlib import Path


BOOTSTRAP = Path("deploy/freebsd/bootstrap.sh")
PYPROJECT = Path("pyproject.toml")


def test_bootstrap_installs_isolated_sentry_sdk() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert 'LAB_SENTRY_SDK_VERSION=${LAB_SENTRY_SDK_VERSION:-2.66.1}' in text
    assert '"${PY_TAG}-sentry-sdk"' not in text
    assert '--no-deps --ignore-installed "sentry-sdk==$LAB_SENTRY_SDK_VERSION"' in text
    assert 'install_sentry_sdk "$LAB_DAEMON_VENV"' in text
    assert 'install_sentry_sdk "$JUPYTER_VENV"' in text
    assert 'sentry-sdk resolved outside venv' in text
    assert 'unexpected sentry-sdk version' in text
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
