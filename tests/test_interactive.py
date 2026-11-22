"""Check the installer prompts and SNI reports without external network calls."""

import os
from contextlib import redirect_stdout
from io import StringIO
from unittest import TestCase
from unittest.mock import ANY, patch

from setup3ui.config import validate_config
from setup3ui.host.reality import Probe, ProbeResult
from setup3ui.interactive import EXAMPLE, ask_config, ask_reality_sni
from setup3ui.terminal import ScanDisplay, Terminal


class TerminalStream(StringIO):
    def isatty(self) -> bool:
        return True


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
            "n",
            "",
        ]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("builtins.input", side_effect=answers) as prompt,
            patch("setup3ui.interactive.probe", return_value=Probe("www.cloudflare.com", 10)),
            patch("setup3ui.host.reality._ping", return_value="unavailable"),
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
            "y",
            "top10",
            "",
        ]
        with (
            patch.dict(os.environ, {"SUDO_USER": "fixture"}),
            patch("builtins.input", side_effect=answers) as prompt,
            patch("setup3ui.interactive.scan", return_value=["other.example.org"]) as scan,
            patch(
                "setup3ui.config.secrets.token_hex",
                side_effect=["01020304", "05060708"],
            ),
        ):
            result = validate_config(ask_config(defaults))
        self.assertEqual(scan.call_args.args[1], "top10")
        self.assertEqual(result["reality_sni"], ["other.example.org"])
        self.assertEqual(result["reality_target"], "other.example.org:443")
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


    def test_manual_failure_offers_alternatives_and_can_keep_unverified(self) -> None:
        config = validate_config({"domain": "example.org", "email": "test@example.org", "reality": True})
        with (
            patch("builtins.input", side_effect=["n", "bad.example.org", "9", "m", "y"]),
            patch("setup3ui.interactive.probe", return_value=Probe("bad.example.org", None, "timeout")) as probe,
            patch("setup3ui.interactive.scan", return_value=["other.example.org"]) as scan,
            redirect_stdout(StringIO()) as output,
        ):
            result = ask_reality_sni(config)
        self.assertEqual(result["reality_sni"], ["bad.example.org"])
        self.assertIn("Checking TLS for bad.example.org (bad.example.org:443)", output.getvalue())
        self.assertIn("Failed · timeout", output.getvalue())
        self.assertIn("saved as unverified", output.getvalue())
        probe.assert_called_once_with("bad.example.org", "bad.example.org:443")
        scan.assert_called_once_with(config, "top10", output=ANY)

    def test_scanner_offers_subnet_timeout_and_manual_fallback(self) -> None:
        config = validate_config({"domain": "example.org", "email": "test@example.org", "reality": True})
        with (
            patch("builtins.input", side_effect=["y", "invalid", "scanner", "", "", "manual", "", "y"]),
            patch("setup3ui.interactive.primary_subnet", return_value="192.0.2.0/28"),
            patch("setup3ui.interactive.scan", side_effect=[ValueError("offline"), []]) as scan,
            patch("setup3ui.interactive.probe", return_value=Probe("www.cloudflare.com", None, "timeout")),
            redirect_stdout(StringIO()) as output,
        ):
            result = ask_reality_sni(config)
        self.assertEqual(result["reality_target"], "www.cloudflare.com:443")
        self.assertIn("offline", output.getvalue())
        self.assertEqual(scan.call_args_list[0].args[1:], ("scanner", "192.0.2.0/28", 30))

    def test_missing_subnet_can_switch_to_top10(self) -> None:
        config = validate_config({"domain": "example.org", "email": "test@example.org", "reality": True})
        with (
            patch("builtins.input", side_effect=["y", "scanner", "", ""]),
            patch("setup3ui.interactive.primary_subnet", return_value=None),
            patch("setup3ui.interactive.scan", return_value=["tls.example.org"]) as scan,
        ):
            result = ask_reality_sni(config)
        self.assertEqual(result["reality_sni"], ["tls.example.org"])
        scan.assert_called_once_with(config, "top10", output=ANY)

    def test_caddy_conflict_and_failed_manual_probe_allow_reentry(self) -> None:
        config = validate_config({
            "domain": "example.org", "email": "test@example.org",
            "panel_domain": "www.cloudflare.com", "https": "caddy", "reality": True,
        }, pending_sni=True)
        with (
            patch("builtins.input", side_effect=["n", "", "bad.example.org", "n", "good.example.org"]),
            patch("setup3ui.interactive.scan", return_value=[]),
            patch("setup3ui.interactive.probe", side_effect=[
                Probe("bad.example.org", None, "timeout"), Probe("good.example.org", 10),
            ]) as probe,
            patch("setup3ui.host.reality._ping", return_value="unavailable"),
            redirect_stdout(StringIO()) as output,
        ):
            result = ask_reality_sni(config)
        self.assertEqual(result["reality_sni"], ["good.example.org"])
        self.assertIn("differs from the Caddy site names", output.getvalue())
        self.assertIn("good.example.org: Passed · TLS 10 ms", output.getvalue())
        self.assertEqual(probe.call_count, 2)

    def test_scanner_reprompts_for_bad_timeout_and_no_results(self) -> None:
        config = validate_config({"domain": "example.org", "email": "test@example.org", "reality": True})
        with (
            patch("builtins.input", side_effect=["y", "scanner", "", "0", "top10", "top10", ""]),
            patch("setup3ui.interactive.primary_subnet", return_value="192.0.2.0/28"),
            patch("setup3ui.interactive.scan", side_effect=[[], ["tls.example.org"]]),
            redirect_stdout(StringIO()) as output,
        ):
            result = ask_reality_sni(config)
        self.assertEqual(result["reality_sni"], ["tls.example.org"])
        self.assertIn("timeout must be positive", output.getvalue())
        self.assertIn("No suitable REALITY SNI found.", output.getvalue())

    def test_no_color_keeps_live_status_and_narrow_results_use_a_list(self) -> None:
        with (
            patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1", "COLUMNS": "36"}),
            redirect_stdout(TerminalStream()) as output,
        ):
            terminal = Terminal()
            terminal.heading("REALITY SNI")
            display = ScanDisplay(terminal)
            with display.track():
                display.status("Top 10: checking TLS", "1/5")
            terminal.probe_results([ProbeResult(Probe("long-name.example.org", 40), "12 ms")], numbered=True)
        self.assertIn("\r\x1b[2KTop 10: checking TLS  1/5", output.getvalue())
        self.assertNotIn("\x1b[1;36m", output.getvalue())
        self.assertIn("1. long-name.example.org\n   Passed · TLS 40 ms · Ping 12 ms", output.getvalue())

    def test_redirected_status_prints_each_phase_once_without_control_codes(self) -> None:
        with patch.dict(os.environ, {"TERM": "xterm"}), redirect_stdout(StringIO()) as output:
            display = ScanDisplay(Terminal())
            with display.track():
                display.status("Top 10: checking TLS", "0/2")
                display.status("Top 10: checking TLS", "1/2")
                display.status("Measuring ping")
                display.notice("Done.")
        self.assertEqual(output.getvalue(), "Top 10: checking TLS (0/2)...\nMeasuring ping...\nDone.\n")
