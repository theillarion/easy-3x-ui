"""Inject host failures without changing the machine running the tests."""

import json
import socket
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setup3ui.host import dns, preflight


class HostCheckTests(TestCase):
    def test_listeners_parse_ipv4_ipv6_and_missing_owners(self) -> None:
        result = subprocess.CompletedProcess(
            ["ss"],
            0,
            'LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("caddy",pid=7,fd=3),("caddy",pid=7,fd=4))\n'
            "LISTEN 0 128 [::]:8443 [::]:*\n",
        )
        with patch.object(preflight.subprocess, "run", return_value=result) as command:
            self.assertEqual(
                list(preflight.tcp_listeners()),
                [(443, "0.0.0.0:443", ["caddy (PID 7)"]), (8443, "[::]:8443", [])],
            )
        command.assert_called_once_with(
            ("ss", "-H", "-ltnp"), capture_output=True, text=True, check=False
        )

    def test_bad_listener_results(self) -> None:
        cases = [
            (1, "", "access denied", "access denied"),
            (2, "", "", "exit status 2"),
            (0, "LISTEN short\n", "", "TCP listener list"),
            (0, "LISTEN 0 128 [::]:* [::]:*\n", "", "TCP port from ss"),
        ]
        for status, stdout, stderr, error in cases:
            with (
                self.subTest(error=error),
                patch.object(
                    preflight.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        ["ss"],
                        status,
                        stdout,
                        stderr,
                    ),
                ),
                self.assertRaisesRegex(ValueError, error),
            ):
                list(preflight.tcp_listeners())

    def test_host_reports_conflicts_and_tun_together(self) -> None:
        listeners = [
            (443, "0.0.0.0:443", ["caddy (PID 3)"]),
            (8443, "[::]:8443", []),
            (22, "[::]:22", []),
        ]
        with (
            patch.object(preflight.os, "geteuid", return_value=0),
            patch.object(preflight, "tcp_listeners", return_value=listeners),
            patch.object(
                preflight, "check_tun", side_effect=ValueError("No TUN device.")
            ),
            self.assertRaises(ValueError) as error,
        ):
            preflight.check_host({"TLS": 443, "XHTTP": 8443})
        message = str(error.exception)
        self.assertIn("443 for TLS is in use at 0.0.0.0:443 by caddy (PID 3)", message)
        self.assertIn(
            "8443 for XHTTP is in use at [::]:8443 by owner unavailable", message
        )
        self.assertNotIn("22", message)
        self.assertIn("No TUN device.", message)

    def test_host_checks_tun_only_after_root_check(self) -> None:
        with (
            patch.object(preflight.os, "geteuid", return_value=1000),
            patch.object(preflight, "check_tun") as tun,
            self.assertRaisesRegex(ValueError, "Run preflight with sudo"),
        ):
            preflight.check_host({"TLS": 443})
        tun.assert_not_called()
        with (
            patch.object(preflight.os, "geteuid", return_value=0),
            patch.object(preflight, "tcp_listeners", return_value=[]),
            patch.object(preflight, "check_tun") as tun,
        ):
            preflight.check_host({})
        tun.assert_called_once_with()

    def test_wrong_and_missing_tun_device(self) -> None:
        with TemporaryDirectory() as directory:
            device = Path(directory) / "tun"
            with self.assertRaisesRegex(ValueError, "is missing"):
                preflight.check_tun(device)
            device.touch()
            with self.assertRaisesRegex(
                ValueError, "must be the Linux TUN character device"
            ):
                preflight.check_tun(device)


class DnsCheckTests(TestCase):
    def test_dns_accepts_known_addresses_and_deduplicates_domains(self) -> None:
        interfaces = [{"addr_info": [{"local": "1.1.1.1"}, {"local": "fd00::1"}]}]
        response = [(socket.AF_INET, 0, 0, "", ("1.1.1.1", 80))]
        result = subprocess.CompletedProcess(["ip"], 0, json.dumps(interfaces))
        output = StringIO()
        with (
            patch.object(dns, "run", return_value=result),
            patch.object(dns.socket, "getaddrinfo", return_value=response) as resolve,
            redirect_stdout(output),
        ):
            dns.check_dns(["master.example.org", "master.example.org"])
        self.assertEqual(output.getvalue(), "DNS master.example.org: 1.1.1.1\n")
        resolve.assert_called_once_with(
            "master.example.org", 80, type=socket.SOCK_STREAM
        )

    def test_dns_explains_missing_records_and_ipv6_mismatch(self) -> None:
        interfaces = [{"addr_info": [{"local": "2606:4700:4700::1111"}]}]
        result = subprocess.CompletedProcess(["ip"], 0, json.dumps(interfaces))
        with patch.object(dns, "run", return_value=result):
            with (
                patch.object(dns.socket, "getaddrinfo", side_effect=socket.gaierror()),
                self.assertRaisesRegex(
                    ValueError, "Could not resolve DNS for example.org"
                ),
            ):
                dns.check_dns(["example.org"])
            with (
                patch.object(
                    dns.socket,
                    "getaddrinfo",
                    return_value=[
                        (socket.AF_INET6, 0, 0, "", ("2606:4700:4700::1001", 80, 0, 0))
                    ],
                ),
                self.assertRaisesRegex(
                    ValueError, "IPv6 records .*::1001. Server has .*::1111"
                ),
            ):
                dns.check_dns(["example.org"])
