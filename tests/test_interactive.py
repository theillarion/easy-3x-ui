"""Check the installer prompts and SNI reports without external network calls."""

import os
from contextlib import redirect_stdout
from io import StringIO
from subprocess import CompletedProcess, TimeoutExpired
from unittest import TestCase
from unittest.mock import patch

from setup3ui.config import validate_config
from setup3ui.interactive import EXAMPLE, SNI_CHOICES, _sni_latency, ask_config


class InteractiveTests(TestCase):
    def test_certbot_asks_only_for_settings_used_in_direct_mode(self) -> None:
        answers = [
            "",
            "example.org",
            "",
            "",
            "fixture@example.org",
            "",
            "",
            "fixture",
            "y",
            "",
            "4443",
            "",
            "n",
        ]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("builtins.input", side_effect=answers) as prompt,
        ):
            result = validate_config(ask_config())
        self.assertEqual(result["https"], "certbot")
        self.assertEqual(result["reality_port"], 4443)
        self.assertEqual(result["docker_user"], "fixture")
        self.assertNotIn(
            "Add a CDN", " ".join(call.args[0] for call in prompt.call_args_list)
        )
        self.assertFalse(result["direct_xhttp"])

    def test_caddy_prompts_use_restore_defaults_and_can_add_two_backends(self) -> None:
        defaults = {
            "https": "caddy",
            "panel_domain": "master.example.org",
            "email": "fixture@example.org",
            "reality": True,
            "direct_xhttp": True,
            "reality_target": "tls.example.org:443",
        }
        answers = [
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "generate",
            "TLS.Example.org",
            "y",
            "OTHER.Example.org",
            "yes",
            "",
            "",
            "",
            "",
            "",
            "y",
            "yandex",
            "",
            "next.example.org",
            "",
            "generate",
            "n",
        ]
        with (
            patch.dict(os.environ, {"SUDO_USER": "fixture"}),
            patch("builtins.input", side_effect=answers) as prompt,
            patch("setup3ui.interactive._sni_latency") as latency,
            patch(
                "setup3ui.config.secrets.token_hex",
                side_effect=["01020304", "05060708"],
            ),
        ):
            result = validate_config(ask_config(defaults))
        latency.assert_called_once_with(("tls.example.org", *SNI_CHOICES))
        self.assertEqual(result["reality_sni"], ["other.example.org"])
        self.assertEqual(result["reality_target"], "tls.example.org:443")
        self.assertEqual(result["reality_backend_port"], 8444)
        self.assertEqual([entry["port"] for entry in result["cdn"]], [8080, 8081])
        self.assertEqual(result["cdn"][0]["path"], EXAMPLE)
        self.assertCountEqual(
            [result["direct_xhttp_path"], result["cdn"][1]["path"]],
            ["/media/hls/01020304/index.m3u8", "/media/hls/05060708/index.m3u8"],
        )
        self.assertNotIn("docker_user", result)
        self.assertIn(EXAMPLE, " ".join(call.args[0] for call in prompt.call_args_list))

    def test_invalid_https_and_port_fail_with_clear_messages(self) -> None:
        with (
            patch("builtins.input", return_value="unknown"),
            self.assertRaisesRegex(ValueError, "Choose certbot"),
        ):
            ask_config()
        with (
            patch(
                "builtins.input",
                side_effect=["", "example.org", "", "", "test@example.org", "four"],
            ),
            self.assertRaisesRegex(ValueError, "Panel port must be a port number"),
        ):
            ask_config()

    def test_sni_output_sorts_tls_timings_and_reports_ping(self) -> None:
        fixture = [
            CompletedProcess([], 0, "ALPN protocol: h2"),
            CompletedProcess([], 0, "ALPN protocol: h2"),
            CompletedProcess([], 0, "ALPN protocol: http/1.1"),
            TimeoutExpired("openssl", 7),
            CompletedProcess([], 0, "seq=1 time=12 ms"),
            TimeoutExpired("ping", 3),
        ]
        output = StringIO()
        with (
            patch(
                "setup3ui.interactive.shutil.which",
                side_effect=["/usr/bin/openssl", "/usr/bin/ping"],
            ),
            patch(
                "setup3ui.interactive.subprocess.run", side_effect=fixture
            ) as command,
            patch(
                "setup3ui.interactive.time.monotonic",
                side_effect=[0, 0.18, 1, 1.05, 2, 3],
            ),
            redirect_stdout(output),
        ):
            _sni_latency(
                [
                    "slow.example.org",
                    "fast.example.org",
                    "bad.example.org",
                    "slow.example.org",
                    "down.example.org",
                ]
            )
        lines = output.getvalue().splitlines()
        self.assertIn("fast.example.org: TLS 1.3", lines[0])
        self.assertIn("ping 12 ms", lines[0])
        self.assertIn("slow.example.org: TLS 1.3", lines[1])
        self.assertIn("ping unavailable", lines[1])
        self.assertIn("certificate check failed", lines[2])
        self.assertIn("down.example.org", lines[3])
        self.assertEqual(command.call_count, 6)
