from __future__ import annotations

import ctypes
import os
import platform
import socket
from dataclasses import dataclass


SOL_LOCAL = 0
LOCAL_PEERCRED = 1
XUCRED_VERSION = 0
XU_NGROUPS = 16


class _NamedGroups(ctypes.Structure):
    _fields_ = [
        ("cr_gid", ctypes.c_uint),
        ("cr_sgroups", ctypes.c_uint * (XU_NGROUPS - 1)),
    ]


class _Groups(ctypes.Union):
    _anonymous_ = ("named",)
    _fields_ = [
        ("named", _NamedGroups),
        ("cr_groups", ctypes.c_uint * XU_NGROUPS),
    ]


class _Tail(ctypes.Union):
    _fields_ = [
        ("_cr_unused1", ctypes.c_void_p),
        ("cr_pid", ctypes.c_int),
    ]


class _Xucred(ctypes.Structure):
    _anonymous_ = ("groups", "tail")
    _fields_ = [
        ("cr_version", ctypes.c_uint),
        ("cr_uid", ctypes.c_uint),
        ("cr_ngroups", ctypes.c_short),
        ("groups", _Groups),
        ("tail", _Tail),
    ]


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


def freebsd_peer_credentials(peer: socket.socket) -> PeerCredentials:
    """Return kernel-authenticated credentials for a connected FreeBSD Unix peer."""

    if not isinstance(peer, socket.socket):
        raise TypeError("peer must be a socket.socket instance")
    if platform.system() != "FreeBSD":
        raise RuntimeError("FreeBSD LOCAL_PEERCRED is unavailable on this platform")
    if peer.family != socket.AF_UNIX:
        raise RuntimeError("Runtime control peer is not a Unix-domain socket")
    if peer.fileno() == -1:
        raise RuntimeError("Socket is closed")

    libc = ctypes.CDLL(None, use_errno=True)
    getsockopt = libc.getsockopt
    getsockopt.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    getsockopt.restype = ctypes.c_int

    credentials = _Xucred()
    length = ctypes.c_uint(ctypes.sizeof(credentials))
    result = getsockopt(
        peer.fileno(),
        SOL_LOCAL,
        LOCAL_PEERCRED,
        ctypes.byref(credentials),
        ctypes.byref(length),
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if length.value < ctypes.sizeof(credentials):
        raise RuntimeError(
            f"FreeBSD LOCAL_PEERCRED returned {length.value} bytes; "
            f"expected {ctypes.sizeof(credentials)}"
        )
    if credentials.cr_version != XUCRED_VERSION:
        raise RuntimeError(
            f"Unsupported FreeBSD xucred version: {credentials.cr_version}"
        )
    if credentials.cr_ngroups < 1 or credentials.cr_ngroups > XU_NGROUPS:
        raise RuntimeError("FreeBSD peer credentials contain no effective group")
    if credentials.cr_pid <= 1:
        raise RuntimeError("FreeBSD peer credentials contain an invalid process id")

    return PeerCredentials(
        pid=int(credentials.cr_pid),
        uid=int(credentials.cr_uid),
        gid=int(credentials.cr_gid),
    )
