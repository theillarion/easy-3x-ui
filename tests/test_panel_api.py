"""Check authentication and response handling at the panel API boundary."""

import http.client
import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

from setup3ui.panel.api import PanelAPI, _LoopbackHTTPS


class PanelApiTests(TestCase):
    def setUp(self) -> None:
        connection = patch("setup3ui.panel.api._LoopbackHTTPS")
        self.connection = connection.start().return_value
        self.addCleanup(connection.stop)
        self.api = PanelAPI("master.example.org", 443, "/private/")

    def response(
        self, body: object, *, status: int = 200, cookie: str | None = None
    ) -> MagicMock:
        response = MagicMock(status=status)
        response.read.return_value = json.dumps(body).encode()
        response.getheaders.return_value = [("Set-Cookie", cookie)] if cookie else []
        return response

    def test_login_keeps_cookies_and_refreshes_csrf(self) -> None:
        self.connection.getresponse.side_effect = [
            self.response(
                {"success": True, "obj": "csrf-before"}, cookie="session=before; Secure"
            ),
            self.response({"success": True}, cookie="session=after; Secure"),
            self.response({"success": True, "obj": "csrf-after"}),
        ]
        self.api.login("fixture", "test-password", "123456")
        first, login, last = (
            call.args for call in self.connection.request.call_args_list
        )
        self.assertEqual(first[:3], ("GET", "/private/csrf-token", None))
        self.assertEqual(login[:2], ("POST", "/private/login"))
        self.assertEqual(
            json.loads(login[2]),
            {
                "username": "fixture",
                "password": "test-password",
                "twoFactorCode": "123456",
            },
        )
        self.assertEqual(login[3]["Cookie"], "session=before")
        self.assertEqual(login[3]["X-CSRF-Token"], "csrf-before")
        self.assertEqual(last[3]["Cookie"], "session=after")
        self.assertEqual(self.api.headers["X-CSRF-Token"], "csrf-after")
        self.assertEqual(self.connection.close.call_count, 3)

    def test_errors_close_connection(self) -> None:
        bad_json = self.response(None)
        bad_json.read.return_value = b"not JSON"
        cases = (
            (self.response({}, status=401), "returned HTTP 401"),
            (
                self.response({"success": False, "msg": "invalid token"}),
                "invalid token",
            ),
            (self.response([]), "unknown error"),
            (bad_json, "Could not read"),
        )
        for response, error in cases:
            with self.subTest(error=error):
                self.connection.reset_mock()
                self.connection.getresponse.return_value = response
                with self.assertRaisesRegex(ValueError, error):
                    self.api.call("panel/api/inbounds/list")
                self.connection.close.assert_called_once_with()
        self.connection.getresponse.side_effect = http.client.HTTPException(
            "connection closed"
        )
        with self.assertRaisesRegex(ValueError, "Could not read"):
            self.api.call("panel/api/inbounds/list")

    def test_invalid_csrf_does_not_send_credentials(self) -> None:
        self.connection.getresponse.return_value = self.response(
            {"success": True, "obj": None}
        )
        with self.assertRaisesRegex(ValueError, "did not return a CSRF token"):
            self.api.login("fixture", "test-password")
        self.assertEqual(self.connection.request.call_count, 1)


class PanelTlsConnectionTests(TestCase):
    def test_uses_loopback_and_keeps_domain_for_tls(self) -> None:
        with patch("setup3ui.panel.api.socket.create_connection") as connect:
            connection = _LoopbackHTTPS("master.example.org", 8443)
            with patch.object(connection.tls_context, "wrap_socket") as wrap:
                connection.connect()
            connect.assert_called_once_with(("127.0.0.1", 8443), timeout=10)
            wrap.assert_called_once_with(
                connect.return_value, server_hostname="master.example.org"
            )

    def test_closes_socket_on_tls_error(self) -> None:
        with patch("setup3ui.panel.api.socket.create_connection") as connect:
            connection = _LoopbackHTTPS("master.example.org", 443)
            with (
                patch.object(
                    connection.tls_context,
                    "wrap_socket",
                    side_effect=OSError("bad TLS"),
                ),
                self.assertRaisesRegex(OSError, "bad TLS"),
            ):
                connection.connect()
            connect.return_value.close.assert_called_once_with()
