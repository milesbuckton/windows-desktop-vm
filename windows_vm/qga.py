"""Minimal qemu-guest-agent client over the per-VM virtio-serial socket.

Enough for the verify gate and the monitor: guest-ping, guest-exec with
captured output (used to read the ground-truth marker file).
"""
from __future__ import annotations

import base64
import json
import socket
import time

MARKER = r"C:\Windows\Setup\windows-vm-setup.log"


class QGAError(RuntimeError):
    pass


class GuestAgent:
    def __init__(self, sock_path: str, timeout: float = 5.0):
        self.sock_path = sock_path
        self.timeout = timeout

    def _roundtrip(self, obj: dict) -> dict:
        """Send one command; any transport or protocol failure is a QGAError."""
        data = (json.dumps(obj) + "\n").encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.sock_path)
                s.sendall(data)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as exc:
            raise QGAError(f"qemu-ga socket: {exc}") from exc
        line = buf.decode(errors="replace").strip()
        if not line:
            raise QGAError("empty response from qemu-ga")
        try:
            resp = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QGAError(f"malformed qemu-ga reply: {line[:200]!r}") from exc
        if not isinstance(resp, dict):
            raise QGAError(f"unexpected qemu-ga reply: {line[:200]!r}")
        if "error" in resp:
            raise QGAError(str(resp["error"]))
        if "return" not in resp:
            raise QGAError(f"qemu-ga reply without return: {line[:200]!r}")
        return resp

    def ping(self) -> bool:
        try:
            self._roundtrip({"execute": "guest-ping"})
        except QGAError:
            return False
        return True

    def exec(self, path: str, args: list, capture: bool = True) -> int:
        resp = self._roundtrip({
            "execute": "guest-exec",
            "arguments": {
                "path": path,
                "arg": args,
                "capture-output": capture,
            },
        })
        try:
            return int(resp["return"]["pid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QGAError(f"guest-exec reply without pid: {resp!r}") from exc

    def exec_status(self, pid: int) -> dict:
        resp = self._roundtrip({
            "execute": "guest-exec-status",
            "arguments": {"pid": pid},
        })
        status = resp["return"]
        if not isinstance(status, dict):
            raise QGAError(f"unexpected guest-exec-status reply: {resp!r}")
        return status

    def read_file(self, path: str = MARKER, limit: int = 4096) -> str:
        """Return file contents via a PowerShell guest-exec, or '' if absent."""
        ps = (f"if (Test-Path '{path}') "
              f"{{ Get-Content -Raw '{path}' | Select-Object -First 1 }}")
        pid = self.exec("powershell",
                        ["-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-Command", ps])
        for _ in range(30):
            st = self.exec_status(pid)
            if st.get("exited"):
                data = st.get("out-data") or ""
                try:
                    raw = base64.b64decode(data)
                except ValueError as exc:
                    raise QGAError("guest-exec returned invalid base64") from exc
                return raw.decode(errors="replace")[:limit]
            time.sleep(0.5)
        raise QGAError("guest-exec did not exit in time")
