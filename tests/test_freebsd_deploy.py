from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RC_SCRIPT = REPO_ROOT / "deploy/freebsd/rc.d/freebsd_lab_daemon"


def test_rc_script_preserves_daemon_wrapper_as_command() -> None:
    script = RC_SCRIPT.read_text(encoding="utf-8")

    assert 'command="/usr/sbin/daemon"' in script
    assert "freebsd_lab_daemon_runtime_program" in script
    assert "freebsd_lab_daemon_program" not in script
    assert "${freebsd_lab_daemon_runtime_program}" in script


def test_rc_script_validates_word_split_scalar_arguments() -> None:
    script = RC_SCRIPT.read_text(encoding="utf-8")

    assert "freebsd_lab_daemon_validate_scalar()" in script
    assert "whitespace and shell metacharacters are not allowed" in script
    for name in (
        "runtime_program",
        "pidfile",
        "socket",
        "group",
        "network",
        "host_address",
        "address_start",
        "address_end",
        "bridge",
    ):
        assert f"freebsd_lab_daemon_validate_scalar {name}" in script
    assert "runtime_args is deliberately root-controlled free-form" in script
