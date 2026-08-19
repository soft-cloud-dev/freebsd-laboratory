from __future__ import annotations

import json
from pathlib import Path


BOOTSTRAP = Path("deploy/freebsd/bootstrap.sh")
LABEXTENSION_PACKAGE = Path("labextension/package.json")


def bootstrap_text() -> str:
    return BOOTSTRAP.read_text(encoding="utf-8")


def test_bootstrap_keeps_root_daemon_out_of_user_checkout() -> None:
    text = bootstrap_text()
    assert "LAB_DAEMON_VENV=${LAB_DAEMON_VENV:-/usr/local/libexec/freebsd-laboratory/daemon-venv}" in text
    assert "freebsd_lab_daemon_runtime_program=$LAB_DAEMON_VENV/bin/freebsd-lab-runtime-daemon" in text
    assert "chown -R root:wheel \"$LAB_DAEMON_VENV\"" in text
    assert text.index('chown -R root:wheel "$LAB_DAEMON_VENV"') < text.index(
        'chown -R "$LAB_JUPYTER_USER:$JUPYTER_GROUP" "$LAB_REPO_DIR"'
    )


def test_bootstrap_uses_freebsd_binary_runtime_dependencies() -> None:
    text = bootstrap_text()
    assert '"${PY_TAG}-jupyterlab"' in text
    assert '"${PY_TAG}-cryptography"' in text
    assert '"${PY_TAG}-pyyaml"' in text
    assert "--no-deps" in text
    assert "--no-build-isolation" not in text
    assert 'pip install -e ".[dev]"' not in text


def test_bootstrap_honors_pyproject_build_requirements() -> None:
    text = bootstrap_text()
    assert '"$LAB_DAEMON_VENV/bin/python" -m pip install \\\n    --no-deps "$LAB_REPO_DIR"' in text
    assert '"$JUPYTER_VENV/bin/python" -m pip install \\\n    --no-deps -e "$LAB_REPO_DIR"' in text


def test_bootstrap_creates_venv_local_jupyter_entrypoints() -> None:
    text = bootstrap_text()
    assert 'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter" jupyter_core.command main' in text
    assert (
        'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-server" '
        'jupyter_server.serverapp main'
    ) in text
    assert 'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-lab" jupyterlab.labapp main' in text
    assert (
        'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-labextension" '
        'jupyterlab.labextensions main'
    ) in text


def test_bootstrap_registers_server_extension_without_broken_enable_cli() -> None:
    text = bootstrap_text()
    assert "jupyter_server_config.d" in text
    assert '"freebsd_laboratory": true' in text
    assert 'ExtensionPackage(name="freebsd_laboratory", enabled=True)' in text
    assert "server extension enable" not in text
    assert "import freebsd_laboratory, jupyter_core, jupyter_server, jupyterlab" in text


def test_bootstrap_registers_prebuilt_labextension() -> None:
    text = bootstrap_text()
    assert 'LABEXT_OUTPUT="$LAB_REPO_DIR/labextension/labextension"' in text
    assert '[ -d "$LABEXT_OUTPUT/static" ]' in text
    assert 'LABEXT_DEST="$JUPYTER_VENV/share/jupyter/labextensions/$LABEXT_NAME"' in text
    assert 'ln -s "$LABEXT_OUTPUT" "$LABEXT_DEST"' in text
    assert "labextension develop" not in text


def test_labextension_build_produces_prebuilt_bundle() -> None:
    package = json.loads(LABEXTENSION_PACKAGE.read_text(encoding="utf-8"))
    assert package["scripts"]["build:labextension"] == "jupyter-builder build ."
    assert package["jupyterlab"]["outputDir"] == "labextension"
    assert "@jupyter/builder" in package["devDependencies"]


def test_bootstrap_prepares_user_owned_jupyter_paths() -> None:
    text = bootstrap_text()
    assert '"$JUPYTER_HOME/.local/share/jupyter/kernels"' in text
    assert 'install -d -o "$LAB_JUPYTER_USER" -g "$JUPYTER_GROUP" -m 0755 "$path"' in text
    assert text.index('"$JUPYTER_HOME/.local/share/jupyter/kernels"') < text.index(
        'HOME="$JUPYTER_HOME" "$JUPYTER_VENV/bin/freebsd-lab-install-kernel"'
    )


def test_bootstrap_builds_and_proves_real_jail_boundary() -> None:
    text = bootstrap_text()
    assert 'LAB_SRC_BRANCH="releng/$RELEASE_SERIES"' in text
    assert 'make -C "$LAB_SRC_DIR" -j"$JOBS" buildworld' in text
    assert 'LAB_JAIL_PACKAGES="python3 ${PY_TAG}-ipykernel"' in text
    assert 'build-jail-template.sh' in text
    assert 'security.jail.jailed' in text
    assert '[ "$JAILED" = "1" ]' in text
    assert 'jexec "$SMOKE_NAME" ifconfig vnet0' in text


def test_bootstrap_validates_pf_before_installing_ruleset() -> None:
    text = bootstrap_text()
    assert 'pfctl -nf "$PF_TMP"' in text
    assert text.index('pfctl -nf "$PF_TMP"') < text.index(
        'install -m 0600 "$PF_TMP" "$LAB_PF_CONF"'
    )
