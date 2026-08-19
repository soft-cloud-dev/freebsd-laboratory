from __future__ import annotations

from pathlib import Path


BOOTSTRAP = Path("deploy/freebsd/bootstrap.sh")
PF_ANCHOR = Path("deploy/freebsd/pf.anchors/freebsd-lab")


def test_bootstrap_never_falls_back_to_root_jupyter() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert "LAB_JUPYTER_USER=root" not in text
    assert "Jupyter must run as a non-root account" in text
    assert 'if [ "$JUPYTER_UID" -eq 0 ]' in text
    assert "Set LAB_JUPYTER_USER to an existing non-root account" in text


def test_bootstrap_does_not_install_shared_runtime_private_key() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")

    assert "/usr/local/etc/freebsd-laboratory/id_ed25519" not in text
    assert "--ssh-public-key=" not in text
    assert 'SMOKE_KEY_DIR=$(mktemp -d ' in text


def test_pf_anchor_fails_closed_for_ipv6() -> None:
    text = PF_ANCHOR.read_text(encoding="utf-8")

    assert "block in log quick on $lab_if inet6 all" in text
    assert "block out log quick on $lab_if inet6 all" in text
