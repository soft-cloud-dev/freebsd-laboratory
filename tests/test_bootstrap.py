from __future__ import annotations

import json
from pathlib import Path


BOOTSTRAP = Path("deploy/freebsd/bootstrap.sh")
JAIL_BUILDER = Path("deploy/freebsd/images/build-jail-template.sh")
GOLDEN_BUILDER = Path("deploy/freebsd/images/build-golden-images.sh")
LABEXTENSION_PACKAGE = Path("labextension/package.json")


def bootstrap_text() -> str:
    return BOOTSTRAP.read_text(encoding="utf-8")


def jail_builder_text() -> str:
    return JAIL_BUILDER.read_text(encoding="utf-8")


def test_bootstrap_keeps_root_daemon_out_of_user_checkout() -> None:
    text = bootstrap_text()
    assert (
        "LAB_DAEMON_VENV=${LAB_DAEMON_VENV:-"
        "/usr/local/libexec/freebsd-laboratory/daemon-venv}" in text
    )
    assert (
        "freebsd_lab_daemon_runtime_program="
        "$LAB_DAEMON_VENV/bin/freebsd-lab-runtime-daemon" in text
    )
    assert 'chown -R root:wheel "$LAB_DAEMON_VENV"' in text
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
    assert (
        'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter" '
        "jupyter_core.command main" in text
    )
    assert (
        'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-server" '
        "jupyter_server.serverapp main" in text
    )
    assert (
        'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-lab" '
        "jupyterlab.labapp main" in text
    )
    assert (
        'install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-labextension" '
        "jupyterlab.labextensions main" in text
    )


def test_bootstrap_registers_server_extension_without_broken_enable_cli() -> None:
    text = bootstrap_text()
    assert "jupyter_server_config.d" in text
    assert '"freebsd_laboratory": true' in text
    assert 'ExtensionPackage(name="freebsd_laboratory", enabled=True)' in text
    assert "server extension enable" not in text
    assert (
        "import freebsd_laboratory, jupyter_builder, jupyter_core, "
        "jupyter_server, jupyterlab" in text
    )


def test_bootstrap_registers_prebuilt_labextension() -> None:
    text = bootstrap_text()
    assert 'LABEXT_OUTPUT="$LAB_REPO_DIR/labextension/labextension"' in text
    assert '[ -d "$LABEXT_OUTPUT/static" ]' in text
    assert (
        'LABEXT_DEST="$JUPYTER_VENV/share/jupyter/labextensions/$LABEXT_NAME"'
        in text
    )
    assert 'ln -s "$LABEXT_OUTPUT" "$LABEXT_DEST"' in text
    assert "labextension develop" not in text


def test_labextension_build_produces_prebuilt_bundle() -> None:
    package = json.loads(LABEXTENSION_PACKAGE.read_text(encoding="utf-8"))
    assert package["scripts"]["build:labextension"] == "jupyter-builder build ."
    assert package["jupyterlab"]["outputDir"] == "labextension"
    assert "@jupyterlab/builder" in package["devDependencies"]
    assert "@jupyter/builder" not in package["devDependencies"]


def test_bootstrap_prepares_user_owned_jupyter_paths() -> None:
    text = bootstrap_text()
    assert '"$JUPYTER_HOME/.local/share/jupyter/kernels"' in text
    assert (
        'install -d -o "$LAB_JUPYTER_USER" -g "$JUPYTER_GROUP" '
        '-m 0755 "$path"' in text
    )
    assert text.index('"$JUPYTER_HOME/.local/share/jupyter/kernels"') < text.index(
        'HOME="$JUPYTER_HOME" "$JUPYTER_VENV/bin/freebsd-lab-install-kernel"'
    )


def test_bootstrap_defaults_to_official_release_image_mode() -> None:
    text = bootstrap_text()
    assert 'LAB_JAIL_IMAGE_MODE=${LAB_JAIL_IMAGE_MODE:-release}' in text
    assert (
        "LAB_RELEASE_BASE_URL=${LAB_RELEASE_BASE_URL:-"
        "https://download.freebsd.org/releases}" in text
    )
    assert (
        'SNAPSHOT_PREFIX="${JAIL_TEMPLATE_PARENT}/'
        'freebsd-python-${LAB_JAIL_IMAGE_MODE}"' in text
    )
    assert "LAB_JAIL_IMAGE_MODE=release" in text
    assert 'LAB_RELEASE="$LAB_RELEASE"' in text


def test_release_builder_verifies_official_base_distribution() -> None:
    text = jail_builder_text()
    assert 'LAB_JAIL_IMAGE_MODE=${LAB_JAIL_IMAGE_MODE:-release}' in text
    assert 'fetch -q -o "$TMP_DIR/MANIFEST" "$RELEASE_URL/MANIFEST"' in text
    assert '$1 == "base.txz"' in text
    assert 'fetch -q -o "$TMP_DIR/base.txz" "$RELEASE_URL/base.txz"' in text
    assert 'DOWNLOADED_BASE_SHA256=$(sha256 -q "$TMP_DIR/base.txz")' in text
    assert 'if [ "$DOWNLOADED_BASE_SHA256" != "$EXPECTED_BASE_SHA256" ]' in text
    assert 'tar -xpf "$BASE_ARCHIVE" -C "$ROOT" --unlink' in text
    assert '"image_mode": "release"' in text
    assert '"base_sha256": "${BASE_SHA256}"' in text


def test_jail_builder_resolves_packages_against_target_abi() -> None:
    text = jail_builder_text()
    assert 'TARGET_ABI_FILE="$ROOT/bin/sh"' in text
    assert '-o "ABI_FILE=$TARGET_ABI_FILE"' in text
    assert (
        'TARGET_ABI=$(pkg -o "ABI_FILE=$TARGET_ABI_FILE" '
        '-r "$ROOT" config abi)' in text
    )
    assert "pkg_root update -f" in text
    assert "pkg_root install -y $LAB_JAIL_PACKAGES" in text
    assert 'chroot "$ROOT" /etc/rc.d/ldconfig onestart' in text
    assert '[ ! -s "$ROOT/var/run/ld-elf.so.hints" ]' in text
    assert 'chroot "$ROOT" /usr/bin/ldd /usr/local/bin/python3' in text
    assert text.index('chroot "$ROOT" /etc/rc.d/ldconfig onestart') < text.index(
        'chroot "$ROOT" /usr/bin/ldd /usr/local/bin/python3'
    )
    assert "Jail Python has unresolved shared libraries for target ABI" in text
    assert '"package_abi": "${TARGET_ABI}"' in text
    assert "ln -s /lib/libutil" not in text


def test_jail_builder_audit_exceptions_are_exact_and_fail_closed() -> None:
    text = jail_builder_text()
    assert 'LAB_FAIL_ON_PKG_AUDIT=${LAB_FAIL_ON_PKG_AUDIT:-YES}' in text
    assert (
        "LAB_PKG_AUDIT_ALLOWED_VULN_IDS="
        "${LAB_PKG_AUDIT_ALLOWED_VULN_IDS-}" in text
    )
    assert "AUDIT_STATUS=0" in text
    assert "AUDIT_ENFORCED=true" in text
    assert 'PROBLEM_COUNT=$(sed -n' in text
    assert "WWW_COUNT=$(grep -Ec '^[[:space:]]*WWW:[[:space:]]'" in text
    assert "https://[Vv][Uu][Xx][Mm][Ll]" in text
    assert "[Ff][Rr][Ee][Ee][Bb][Ss][Dd]" in text
    assert "Unapproved pkg audit finding: $VUXML_ID" in text
    assert "could not be reduced to exact FreeBSD VuXML findings" in text
    assert "accepting only explicitly allowlisted FreeBSD VuXML findings" in text
    assert '"pkg_audit_enforced": ${AUDIT_ENFORCED}' in text
    assert '"pkg_audit_accepted_vuln_ids": "${AUDIT_ACCEPTED_IDS}"' in text
    assert "pkg_audit_root" in text
    assert "pkg_root audit -F" in text


def test_bootstrap_forwards_audit_policy_to_both_image_modes() -> None:
    text = bootstrap_text()
    assert text.count('LAB_FAIL_ON_PKG_AUDIT="$LAB_FAIL_ON_PKG_AUDIT"') == 2
    assert (
        text.count(
            'LAB_PKG_AUDIT_ALLOWED_VULN_IDS="$LAB_PKG_AUDIT_ALLOWED_VULN_IDS"'
        )
        == 2
    )


def test_bootstrap_waits_for_daemon_readiness_and_prints_diagnostics() -> None:
    text = bootstrap_text()
    assert "wait_for_runtime_daemon()" in text
    assert 'RuntimeClient(timeout=1.0).ping()' in text
    assert 'while [ "$attempt" -lt "$LAB_DAEMON_READY_TIMEOUT" ]' in text
    assert "service freebsd_lab_daemon status" in text
    assert "tail -n 100 /var/log/messages" in text
    assert text.index("service freebsd_lab_daemon start") < text.index(
        "wait_for_runtime_daemon"
    )


def test_bhyve_backend_is_optional_for_jail_only_bootstrap() -> None:
    text = bootstrap_text()
    assert "LAB_INSTALL_BHYVE_BACKEND=${LAB_INSTALL_BHYVE_BACKEND:-NO}" in text
    assert 'if is_yes "$LAB_INSTALL_BHYVE_BACKEND"' in text
    assert "pkg install -y vm-bhyve" in text


def test_source_mode_retains_buildworld_installworld_pipeline() -> None:
    bootstrap = bootstrap_text()
    builder = jail_builder_text()
    assert "LAB_JAIL_IMAGE_MODE=source" in bootstrap
    assert 'LAB_SRC_BRANCH="releng/$RELEASE_SERIES"' in bootstrap
    assert 'make -C "$LAB_SRC_DIR" -j"$JOBS" buildworld' in bootstrap
    assert 'rmdir "$LAB_SRC_DIR"' not in bootstrap
    assert 'make -C "$SRC_DIR" installworld DESTDIR="$ROOT"' in builder
    assert 'make -C "$SRC_DIR" distribution DESTDIR="$ROOT"' in builder
    assert '"image_mode": "source"' in builder
    assert '"source_revision": "${SOURCE_REVISION}"' in builder


def test_paired_golden_images_explicitly_use_source_jail_mode() -> None:
    text = GOLDEN_BUILDER.read_text(encoding="utf-8")
    assert "LAB_JAIL_IMAGE_MODE=source" in text
    assert "buildworld buildkernel" in text


def test_bootstrap_proves_real_jail_boundary() -> None:
    text = bootstrap_text()
    assert 'LAB_JAIL_PACKAGES="python3 ${PY_TAG}-ipykernel"' in text
    assert "build-jail-template.sh" in text
    assert "security.jail.jailed" in text
    assert '[ "$JAILED" = "1" ]' in text
    assert 'jexec "$SMOKE_NAME" ifconfig vnet0' in text


def test_bootstrap_validates_pf_before_installing_ruleset() -> None:
    text = bootstrap_text()
    assert 'pfctl -nf "$PF_TMP"' in text
    assert text.index('pfctl -nf "$PF_TMP"') < text.index(
        'install -m 0600 "$PF_TMP" "$LAB_PF_CONF"'
    )
