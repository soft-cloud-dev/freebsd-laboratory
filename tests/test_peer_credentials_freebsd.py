from __future__ import annotations

import ctypes
import os
import platform
import socket

import pytest

from freebsd_laboratory.peer_credentials import (
    _Xucred,
    freebsd_peer_credentials,
)


def test_native_xucred_layout_preserves_pointer_aligned_tail() -> None:
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(_Xucred) == 88
        assert _Xucred.tail.offset == 80
    else:
        assert ctypes.sizeof(_Xucred) == 80
        assert _Xucred.tail.offset == 76


@pytest.mark.skipif(platform.system() != "FreeBSD", reason="requires FreeBSD LOCAL_PEERCRED")
def test_freebsd_local_peercred_round_trip() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        credentials = freebsd_peer_credentials(left)
    finally:
        left.close()
        right.close()

    assert credentials.pid == os.getpid()
    assert credentials.uid == os.geteuid()
    assert credentials.gid == os.getegid()
