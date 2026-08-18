from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RC_SCRIPT = REPO_ROOT / "deploy/freebsd/rc.d/freebsd_lab_daemon"


def test_rc_script_preserves_daemon_wrapper_as_command() -> None:
    script = RC_SCRIPT.read_text(encoding="utf-8")

    assert 'command="/usr/sbin/daemon"' in script
    assert 'freebsd_lab_daemon_runtime_program' in script
    assert 'freebsd_lab_daemon_program' not in script
    assert '${freebsd_lab_daemon_runtime_program}' in script
