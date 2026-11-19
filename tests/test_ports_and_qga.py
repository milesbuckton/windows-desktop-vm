import base64
import io
import json
import unittest
from unittest import mock

from windows_vm import ports
from windows_vm.qga import GuestAgent, QGAError


class FakeUnixSocket:
    """Feeds scripted qemu-ga JSON responses to GuestAgent._roundtrip."""

    def __init__(self, responses):
        self.responses = responses
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def connect(self, path):
        self.path = path

    def sendall(self, data):
        self.sent = data

    def recv(self, n):
        if not self.responses:
            return b""
        return self.responses.pop(0)


class TestPorts(unittest.TestCase):
    def _ctx_mock(self, bind_effect):
        m = mock.MagicMock()
        entered = m.__enter__.return_value
        entered.bind.side_effect = bind_effect
        return m

    @mock.patch("socket.socket")
    def test_probe_skips_bound_ports(self, sock):
        sock.side_effect = [self._ctx_mock(OSError("in use")),
                            self._ctx_mock(None)]
        self.assertEqual(ports.probe_free_port(3389, 3399), 3390)
        self.assertEqual(sock.call_count, 2)

    @mock.patch("socket.socket")
    def test_probe_exhausted(self, sock):
        sock.return_value = self._ctx_mock(OSError("in use"))
        with self.assertRaises(ports.PortExhausted):
            ports.probe_free_port(3389, 3390)


class TestRdpReady(unittest.TestCase):
    def _conn(self, reply):
        conn = mock.MagicMock()
        conn.__enter__.return_value.recv.return_value = reply
        return conn

    @mock.patch("socket.create_connection")
    def test_connection_confirm(self, create):
        create.return_value = self._conn(bytes.fromhex("030000130ed00000123400"))
        self.assertTrue(ports.rdp_ready("127.0.0.1", 3389))

    @mock.patch("socket.create_connection")
    def test_forward_without_guest_listener(self, create):
        create.return_value = self._conn(b"")
        self.assertFalse(ports.rdp_ready("127.0.0.1", 3389))

    @mock.patch("socket.create_connection", side_effect=ConnectionRefusedError)
    def test_refused(self, _create):
        self.assertFalse(ports.rdp_ready("127.0.0.1", 3389))


class TestGuestAgent(unittest.TestCase):
    def test_ping_ok(self):
        ga = GuestAgent("/tmp/x")
        with mock.patch.object(ga, "_roundtrip",
                               return_value={"return": {}}):
            self.assertTrue(ga.ping())

    def test_ping_error(self):
        ga = GuestAgent("/tmp/x")
        with mock.patch.object(ga, "_roundtrip",
                               side_effect=QGAError("GenericError")):
            self.assertFalse(ga.ping())

    def _agent_with_socket(self, responses=None, exc=None):
        ga = GuestAgent("/tmp/x")
        sock = FakeUnixSocket(list(responses or []))
        if exc:
            sock.recv = mock.Mock(side_effect=exc)
        return ga, mock.patch("socket.socket", return_value=sock)

    def test_roundtrip_error_reply_raises(self):
        ga, patch = self._agent_with_socket(
            [json.dumps({"error": {"class": "GenericError"}}).encode() + b"\n"])
        with patch, self.assertRaises(QGAError):
            ga.exec_status(7)

    def test_roundtrip_timeout_raises_qga_error(self):
        ga, patch = self._agent_with_socket(exc=TimeoutError("timed out"))
        with patch, self.assertRaises(QGAError):
            ga.read_file()

    def test_roundtrip_malformed_reply_raises_qga_error(self):
        ga, patch = self._agent_with_socket([b"not json\n"])
        with patch, self.assertRaises(QGAError):
            ga.exec("powershell", [])

    def test_ping_false_on_socket_error(self):
        ga, patch = self._agent_with_socket(exc=ConnectionResetError())
        with patch:
            self.assertFalse(ga.ping())

    def test_read_file_roundtrip(self):
        out = base64.b64encode(b"first-logon finished").decode()
        ga = GuestAgent("/tmp/x")
        replies = [{"return": {"pid": 7}},
                   {"return": {"exited": True, "out-data": out}}]
        with mock.patch.object(ga, "_roundtrip", side_effect=replies):
            self.assertIn("first-logon", ga.read_file())

    @mock.patch("time.sleep")
    def test_read_file_retries_with_sleep(self, mock_sleep):
        out = base64.b64encode(b"first-logon finished").decode()
        ga = GuestAgent("/tmp/x")
        replies = [
            {"return": {"pid": 7}},
            {"return": {"exited": False}},
            {"return": {"exited": True, "out-data": out}},
        ]
        with mock.patch.object(ga, "_roundtrip", side_effect=replies):
            self.assertIn("first-logon", ga.read_file())
        mock_sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
