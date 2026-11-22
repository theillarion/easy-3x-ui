"""Check REALITY policy and subprocess boundaries without internet scans."""

import hashlib
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, call, patch

from setup3ui.config import SNI_RUN_FIELDS, validate_config
from setup3ui.host import reality


class RealityTests(TestCase):
    base = {"domain": "example.org", "email": "test@example.org", "reality": True}
    auto = {"reality_sni_auto": True, "reality_sni": ["backup.example.org"]}

    def test_validate_options(self) -> None:
        self.assertEqual(validate_config(self.base | self.auto)["reality_sni_scan"], "top10")
        scanner = validate_config(self.base | self.auto | {
            "reality_sni_scan": "scanner", "reality_sni_scan_cidr": "192.0.2.2/28",
        })
        self.assertEqual(scanner["reality_sni_scan_cidr"], "192.0.2.0/28")
        self.assertEqual(scanner["reality_sni_scan_timeout"], 30)
        for options, error in (
            ({"reality": False}, "require TCP"),
            ({"reality_sni_auto": "yes"}, "true or false"),
            ({"reality_sni": None}, "non-empty list"),
            ({"reality_sni": []}, "non-empty list"),
            ({"reality_sni_scan": "unknown"}, "top10 or scanner"),
            ({"reality_target": "tls.example.org:443"}, "Remove reality_target"),
            ({"reality_sni_scan_timeout": 5}, "CIDR and timeout"),
            ({"reality_sni_scan": "scanner", "reality_sni_scan_timeout": True}, "positive integer"),
            ({"reality_sni_scan": "scanner", "reality_sni_scan_timeout": 0}, "positive integer"),
            ({"reality_sni_scan": "scanner", "reality_sni_scan_cidr": "192.0.2.1/32"}, "wider"),
            ({"reality_sni_scan": "scanner", "reality_sni_scan_cidr": "192.0.2.1"}, "IPv4 CIDR"),
            ({"reality_sni_auto": False, "reality_sni_scan": "top10"}, "require reality_sni_auto"),
        ):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, error):
                validate_config(self.base | self.auto | options)
        with self.assertRaisesRegex(ValueError, "Set reality_sni with fallback names"):
            validate_config(self.base | {"reality_sni_auto": True})
        with self.assertRaisesRegex(ValueError, "Unknown config fields: reality_sni_fallback"):
            validate_config(self.base | self.auto | {"reality_sni_fallback": "old.example.org"})
        with self.assertRaisesRegex(ValueError, "Set reality_sni"):
            validate_config(self.base | {"reality_sni_auto": False})

    def test_subnet_uses_preferred_default_route_and_source(self) -> None:
        route = [
            {"dev": "other", "metric": 100},
            {"dev": "eth0", "metric": 10, "prefsrc": "192.0.2.18"},
        ]
        addresses = [{"addr_info": [
            {"local": "10.0.0.1", "prefixlen": 24}, {"local": "192.0.2.18", "prefixlen": 28},
        ]}]
        with patch.object(reality, "_ip_json", side_effect=[route, addresses]) as ip:
            self.assertEqual(str(reality.primary_subnet()), "192.0.2.16/28")
        self.assertIn("eth0", ip.call_args.args)
        for responses in (
            [[]],
            [route, [{"addr_info": []}]],
            [route, [{"addr_info": [{"local": "192.0.2.18", "prefixlen": 32}]}]],
            [FileNotFoundError()],
        ):
            with patch.object(reality, "_ip_json", side_effect=responses):
                self.assertIsNone(reality.primary_subnet())
        with patch.object(reality.subprocess, "run", return_value=CompletedProcess([], 0, '[{"dev":"eth0"}]')):
            self.assertEqual(reality._ip_json("route"), [{"dev": "eth0"}])

    def test_probe_verifies_target_and_formats_ranking(self) -> None:
        host = "tls.example.org"
        other = "slow.example.org"
        replies = [
            CompletedProcess([], 0, "ALPN protocol: h2"),
            CompletedProcess([], 1, ""),
            CompletedProcess([], 0, "ALPN protocol: http/1.1"),
            TimeoutExpired("openssl", 5),
            FileNotFoundError(2, "missing"),
            CompletedProcess([], 0, "seq=1 time=1.28 ms"),
            CompletedProcess([], 0, ""),
        ]
        with (
            patch.object(reality.subprocess, "run", side_effect=replies) as command,
            patch.object(reality.time, "monotonic", side_effect=[0, 0.042, 1, 2, 3, 4]),
            redirect_stdout(StringIO()) as output,
        ):
            probes = [reality.probe(host, "target.example.org:8443") for _ in range(5)]
            ranked = reality.report_probes([reality.Probe(other, 50), *probes])
        self.assertEqual(command.call_args_list[0].kwargs["timeout"], 5)
        self.assertIn("target.example.org:8443", command.call_args_list[0].args[0])
        self.assertEqual(ranked, [host, other])
        for fragment in ("TLS 42 ms, ping 1.28 ms.", "ping unavailable", "certificate check failed", "HTTP/2 was not", "timed out", "missing"):
            self.assertIn(fragment, output.getvalue())
        with patch.object(reality.subprocess, "run", side_effect=TimeoutExpired("ping", 3)):
            self.assertEqual(reality._ping(host), "unavailable")

    def test_download_checks_assets_and_hash(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = b"scanner fixture"

            def download(command, **options):
                Path(command[command.index("--output") + 1]).write_bytes(payload)

            with (
                patch.object(reality.platform, "system", return_value="Linux"),
                patch.object(reality.platform, "machine", return_value="x86_64"),
                patch.object(reality.subprocess, "run", side_effect=download) as fetch,
                patch.dict(reality.SCANNER_ASSETS, {"x86_64": ("amd64", hashlib.sha256(payload).hexdigest())}),
            ):
                binary = reality._download_scanner(directory)
                self.assertEqual(binary.name, "RealiTLScanner-linux-amd64")
                self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
                with patch.dict(reality.SCANNER_ASSETS, {"x86_64": ("amd64", "wrong")}):
                    with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                        reality._download_scanner(directory)
                fetch.side_effect = CalledProcessError(1, "curl")
                with self.assertRaisesRegex(ValueError, "Could not download"):
                    reality._download_scanner(directory)
            with patch.object(reality.platform, "system", return_value="Darwin"):
                with self.assertRaisesRegex(ValueError, "supports Linux"):
                    reality._download_scanner(directory)

    def test_scanner_timeout_reads_complete_rows_only(self) -> None:
        def run(command, **options):
            output = Path(command[command.index("-out") + 1])
            output.write_text(
                "IP,CERT_DOMAIN\n192.0.2.1,TLS.example.org.\n192.0.2.2,*.example.org\n"
                "192.0.2.3,tls.example.org\n192.0.2.4,partial.example.org",
                encoding="utf-8",
            )
            options["stdout"].write(
                b'time=2026-05-30T12:00:00Z level=INFO msg="Connected to target" feasible=true '
                b'ip=192.0.2.5 cert-domain=log-only.example.org cert-issuer=Fixture\n'
                b'time=2026-05-30T12:00:00Z level=INFO msg="Connected to target" feasible=false '
                b'ip=192.0.2.6 cert-domain=unusable.example.org cert-issuer=Fixture\n'
                b'time=2026-05-30T12:00:00Z level=INFO msg="Connected to target" feasible=true '
                b'ip=192.0.2.7 cert-domain=incomplete.example.org cert-issuer=Fixture'
            )
            raise TimeoutExpired(command, options["timeout"])

        with (
            patch.object(reality, "_download_scanner", return_value=Path("/fixture/scanner")),
            patch.object(reality.subprocess, "run", side_effect=run) as command,
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(
                reality.scanner_names("192.0.2.0/28", 30), ["tls.example.org", "log-only.example.org"],
            )
            self.assertEqual(command.call_args.kwargs["timeout"], 30)
            self.assertIn("reached its deadline", output.getvalue())
            display = Mock(spec=reality.ScanOutput)
            with (
                patch.object(reality, "probe", side_effect=lambda name, target: reality.Probe(name, 10)),
                patch.object(reality, "_ping", return_value="17 ms"),
            ):
                self.assertEqual(
                    reality.scan(validate_config(self.base), "scanner", "192.0.2.0/28", 30, output=display),
                    ["log-only.example.org", "tls.example.org"],
                )
            self.assertEqual(display.status.call_args_list, [
                call("Preparing Reality Scanner"),
                call("Scanning 192.0.2.0/28 on TCP/443 with a 30s deadline"),
                call("Reality Scanner: checking candidate TLS", "0/2"),
                call("Reality Scanner: checking candidate TLS", "1/2"),
                call("Reality Scanner: checking candidate TLS", "2/2"),
                call("Measuring ping"),
            ])
            display.notice.assert_called_once_with("Reality Scanner reached its deadline. Checking completed results.")
            display.results.assert_called_once_with([
                reality.ProbeResult(reality.Probe("log-only.example.org", 10), "17 ms"),
                reality.ProbeResult(reality.Probe("tls.example.org", 10), "17 ms"),
            ])
            command.side_effect = CalledProcessError(1, "scanner")
            with self.assertRaisesRegex(ValueError, "Scanner failed"):
                reality.scanner_names("192.0.2.0/28", 30)
            command.side_effect = None
            self.assertEqual(reality.scanner_names("192.0.2.0/28", 30), [])
        with patch.object(reality, "primary_subnet", return_value=None):
            with self.assertRaisesRegex(ValueError, "Could not determine"):
                reality.scanner_names(None, 30)

    def test_scan_rechecks_names_and_excludes_caddy_routing(self) -> None:
        blocked = "master.example.org"
        name = "tls.example.org"
        config = validate_config(self.base | {"https": "caddy"})
        with (
            patch.object(reality, "scanner_names", return_value=[blocked, name]),
            patch.object(reality, "probe", return_value=reality.Probe(name, 10)) as probe,
            patch.object(reality, "_ping", return_value="unavailable"),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(reality.scan(config, "scanner"), [name])
        probe.assert_called_once_with(name, f"{name}:443")

    def test_yaml_resolves_fallback_and_strips_run_options(self) -> None:
        fallback = "backup.example.org"
        bad = "bad.example.org"
        config = validate_config(self.base | self.auto | {
            "https": "caddy", "reality_sni": ["master.example.org", bad, fallback],
        })

        probes = [
            reality.Probe(bad, None, "timeout"), reality.Probe(fallback, 10),
            reality.Probe(bad, None, "timeout"), reality.Probe(fallback, None, "timeout"),
        ]
        with (
            patch.object(reality, "scan", side_effect=[["fast.example.org"], ValueError("offline"), []]),
            patch.object(reality, "probe", side_effect=probes) as probe,
            patch.object(reality, "_ping", return_value="unavailable"),
            redirect_stdout(StringIO()) as output,
        ):
            self.assertEqual(reality.resolve_sni(config)["reality_sni"], ["fast.example.org"])
            probe.assert_not_called()
            resolved = reality.resolve_sni(config)
            self.assertEqual(resolved["reality_sni"], [fallback])
            self.assertEqual(resolved["reality_target"], f"{fallback}:443")
            self.assertEqual([call.args[0] for call in probe.call_args_list], [bad, fallback])
            self.assertFalse(SNI_RUN_FIELDS.intersection(resolved))
            with self.assertRaisesRegex(ValueError, "No fallback REALITY SNI"):
                reality.resolve_sni(config)
        self.assertIn("offline", output.getvalue())
        self.assertIn("master.example.org: conflicts with a Caddy site name", output.getvalue())

    def test_yaml_manual_checks_all_names_against_the_configured_target(self) -> None:
        raw = self.base | {"reality_sni": ["one.example.org", "two.example.org"], "reality_target": "target.example.org:8443"}
        legacy = validate_config(raw)
        manual = validate_config(raw | {"reality_sni_auto": False})
        with (
            patch.object(reality, "probe", side_effect=lambda name, _: reality.Probe(name, 7)) as probe,
            patch.object(reality, "_ping", return_value="unavailable"),
            redirect_stdout(StringIO()),
        ):
            self.assertIs(reality.resolve_sni(legacy), legacy)
            self.assertEqual(reality.resolve_sni(manual), legacy)
            self.assertEqual([call.args[1] for call in probe.call_args_list], ["target.example.org:8443"] * 2)
            probe.side_effect = None
            probe.return_value = reality.Probe("two.example.org", None, "timeout")
            with self.assertRaisesRegex(ValueError, "configured REALITY SNI"):
                reality.resolve_sni(manual)
