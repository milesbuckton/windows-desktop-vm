"""Loopback port probing for the RDP forward (mirrors the SSH 2222-2322 probe)."""
from __future__ import annotations

import socket

RDP_PROBE_START = 3389
RDP_PROBE_END = 3399


class PortExhausted(RuntimeError):
    pass


def probe_free_port(start: int = RDP_PROBE_START, end: int = RDP_PROBE_END,
                    host: str = "127.0.0.1") -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise PortExhausted(f"no free port in {host}:{start}-{end}")


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


# X.224 Connection Request carrying an RDP Negotiation Request (TLS).
RDP_NEG_REQUEST = bytes.fromhex("030000130ee000000000000100080003000000")


def rdp_ready(host: str, port: int, timeout: float = 5.0) -> bool:
    """True when the guest's RDP service answers, not just QEMU's forward.

    QEMU's user-mode hostfwd accepts TCP connections even while nothing listens
    in the guest, so a bare connect proves nothing. RDP only comes up from
    FirstLogonCommands, so a real X.224 Connection Confirm means the install
    has finished.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(RDP_NEG_REQUEST)
            reply = s.recv(64)
    except OSError:
        return False
    return len(reply) >= 6 and reply[0] == 0x03 and reply[5] & 0xF0 == 0xD0
