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
        data = (json.dumps(obj) + "\n").encode()
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
        line = buf.decode(errors="replace").strip()
        if not line:
            raise QGAError("empty response from qemu-ga")
        return json.loads(line)

    def ping(self) -> bool:
        try:
            resp = self._roundtrip({"execute": "guest-ping"})
        except (OSError, QGAError, json.JSONDecodeError):
            return False
        return "error" not in resp

    def exec(self, path: str, args: list, capture: bool = True) -> int:
        resp = self._roundtrip({
            "execute": "guest-exec",
            "arguments": {
                "path": path,
                "arg": args,
                "capture-output": capture,
            },
        })
        if "error" in resp:
            raise QGAError(str(resp["error"]))
        return int(resp["return"]["pid"])

    def exec_status(self, pid: int) -> dict:
        resp = self._roundtrip({
            "execute": "guest-exec-status",
            "arguments": {"pid": pid},
        })
        return resp["return"]

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
                return base64.b64decode(data).decode(errors="replace")[:limit]
            time.sleep(0.5)
        raise QGAError("guest-exec did not exit in time")
