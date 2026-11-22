"""Check diagnostic parsing and failure handling with test-only input."""

import base64
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import urlencode

from setup3ui.diagnostics import connections


class DiagnosticTests(TestCase):
    link = "vless://test-id@example.org:443"

    def test_subscription_parses_base64_and_deduplicates(self) -> None:
        text = f"{self.link}\n\n{self.link}\nvmess://unsupported\n"
        encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        for value in (text, encoded):
            with self.subTest(value=value):
                self.assertEqual(connections._links(value), ([self.link], 1))
        with self.assertRaisesRegex(ValueError, "Could not read"):
            connections._links("a")

    def test_transport_settings(self) -> None:
        cases = (
            (
                {
                    "type": "tcp",
                    "security": "reality",
                    "pbk": "test-key",
                    "sni": "example.org",
                    "sid": "abcd",
                },
                "realitySettings",
                {
                    "serverName": "example.org",
                    "password": "test-key",
                    "shortId": "abcd",
                },
            ),
            (
                {
                    "type": "xhttp",
                    "path": "/test",
                    "mode": "packet-up",
                    "extra": '{"test": true}',
                },
                "xhttpSettings",
                {"path": "/test", "mode": "packet-up", "extra": {"test": True}},
            ),
            ({"type": "ws", "path": "/ws"}, "wsSettings", {"path": "/ws", "host": ""}),
            (
                {"type": "grpc", "serviceName": "service", "mode": "multi"},
                "grpcSettings",
                {"serviceName": "service", "multiMode": True},
            ),
            (
                {"type": "raw", "security": "tls", "alpn": "h2,http/1.1"},
                "tlsSettings",
                {"alpn": ["h2", "http/1.1"], "serverName": "example.org"},
            ),
        )
        for params, field, expected in cases:
            with self.subTest(params=params):
                result = connections._outbound(f"{self.link}?{urlencode(params)}")
                self.assertEqual(result["streamSettings"]["network"], params["type"])
                for name, value in expected.items():
                    self.assertEqual(result["streamSettings"][field][name], value)
        with_flow = connections._outbound(f"{self.link}?flow=xtls-rprx-vision")
        self.assertEqual(
            with_flow["settings"]["vnext"][0]["users"][0]["flow"], "xtls-rprx-vision"
        )

    def test_invalid_transport_links(self) -> None:
        for link, error in (
            ("vless://test@example.org:x", "Invalid server address"),
            ("vless://example.org:443", "Missing server, port or client ID"),
            (self.link + "?type=other", "Unsupported transport or TLS mode"),
            (self.link + "?security=other", "Unsupported transport or TLS mode"),
            (self.link + "?security=reality", "Missing REALITY public key or SNI"),
            (self.link + "?type=xhttp&extra=%5B%5D", "Invalid XHTTP extra settings"),
        ):
            with self.subTest(link=link), self.assertRaisesRegex(ValueError, error):
                connections._outbound(link)

    def test_probe_maps_status_and_uses_requested_proxy(self) -> None:
        url = "https://example.org/test"
        for exit_code, output, port, available, code in (
            (0, "302", None, True, 302),
            (0, "404", 8443, False, 404),
            (7, "000", None, False, None),
            (7, "", None, False, None),
            (7, "200", None, False, 200),
        ):
            with (
                self.subTest(output=output, exit_code=exit_code),
                patch.object(
                    connections.subprocess,
                    "run",
                    return_value=CompletedProcess([], exit_code, output),
                ),
            ):
                result = connections._probe(url, "test", port)
                self.assertEqual(result["available"], available)
                self.assertEqual(result["status"], code)
        with patch.object(
            connections.subprocess, "run", return_value=CompletedProcess([], 0, "200")
        ) as command:
            connections._probe(url, "test", 8443)
        self.assertIn("socks5h://127.0.0.1:8443", command.call_args.args[0])
        for invalid in ("http://example.org/", "https://", "https://user@example.org/"):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "must use HTTPS"),
            ):
                connections._probe(invalid, "test")

    def test_json_output_reports_unavailable_resources_and_bad_links(self) -> None:
        output = StringIO()
        with TemporaryDirectory() as directory:
            links = Path(directory) / "links.txt"
            links.write_text(
                self.link + "?type=unknown#label\nvmess://unsupported", encoding="utf-8"
            )
            with (
                patch.object(
                    connections.subprocess,
                    "run",
                    return_value=CompletedProcess([], 7, "000"),
                ),
                redirect_stdout(output),
            ):
                connections.test_connections(
                    None, links, ["https://example.org/"], "unused", True
                )
        report = json.loads(output.getvalue())
        self.assertFalse(report["checks"][0]["available"])
        self.assertEqual(report["unsupported_entries"], 1)
        self.assertEqual(
            report["errors"],
            [
                {
                    "connection": "1. label",
                    "error": "Could not run this VLESS connection.",
                }
            ],
        )
        self.assertNotIn("test-id", output.getvalue())
