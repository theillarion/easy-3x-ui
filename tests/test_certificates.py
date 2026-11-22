"""Check certificate failures and timer rollback without a system service manager."""

from contextlib import ExitStack
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch

from setup3ui.certificates import acme, renewal


class CertificateTests(TestCase):
    def test_lock_rejects_concurrent_work_and_is_reusable(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (
                acme.certificate_lock(directory),
                self.assertRaisesRegex(
                    ValueError, "Another certificate operation is running"
                ),
                acme.certificate_lock(directory),
            ):
                self.fail("Two certificate operations took the same lock")
            with acme.certificate_lock(directory):
                self.assertTrue((directory / ".certificate.lock").is_file())

    def test_occupied_http_port_aborts_before_docker(self) -> None:
        with (
            TemporaryDirectory() as temporary,
            patch.object(acme.os, "geteuid", return_value=0),
            patch.object(
                acme,
                "tcp_listeners",
                return_value=[(80, "[::]:80", ["server (PID 8)"])],
            ),
            patch.object(acme, "docker") as docker,
            self.assertRaisesRegex(
                ValueError,
                r"TCP port 80 is in use at \[::\]:80 .*Certbot needs this port",
            ),
        ):
            acme.issue_certificate(
                Path(temporary),
                "master.example.org",
                "gateway.example.org",
                "fixture@example.org",
            )
        docker.assert_not_called()

    def test_invalid_certificate_is_reported_with_its_directory(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            chain, key = directory / "fullchain.pem", directory / "privkey.pem"
            chain.write_bytes(b"invalid cert")
            key.write_bytes(b"invalid key")
            with self.assertRaisesRegex(
                ValueError, "Could not load the certificate and key from"
            ):
                acme.check_certificate(chain, key)


class RenewalTimerTests(TestCase):
    domain = "master.example.org"

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.installation = self.root / "panel"
        self.unit_dir = self.root / "systemd"
        self.unit_dir.mkdir()
        self.service = self.unit_dir / f"{renewal.UNIT}.service"
        self.timer = self.unit_dir / f"{renewal.UNIT}.timer"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(renewal.os, "geteuid", return_value=0))
        stack.enter_context(patch.object(renewal, "UNIT_DIR", self.unit_dir))
        stack.enter_context(patch.object(renewal, "check_root_owned_directory"))
        stack.enter_context(patch.object(renewal, "check_certificate"))
        stack.enter_context(patch.object(renewal, "_compose"))
        self.runtime = stack.enter_context(
            patch.object(renewal, "_install_renewal_runtime")
        )
        self.systemctl = stack.enter_context(patch.object(renewal, "run"))
        self.repair = stack.enter_context(patch.object(renewal.subprocess, "run"))

    def test_create_timer_and_service(self) -> None:
        renewal.install_renewal_timer(self.installation, self.domain)
        self.assertIn(
            "ExecStart=/usr/bin/flock -n /run/setup-3x-ui.lock",
            self.service.read_text(encoding="utf-8"),
        )
        self.assertIn(
            f" {self.installation.resolve()} {self.domain}",
            self.service.read_text(encoding="utf-8"),
        )
        self.assertIn("Persistent=true", self.timer.read_text(encoding="utf-8"))
        self.systemctl.assert_has_calls(
            [
                call("systemctl", "daemon-reload"),
                call("systemctl", "enable", "--now", f"{renewal.UNIT}.timer"),
            ]
        )

    def test_existing_timer_for_other_installation_is_preserved(self) -> None:
        self.service.write_text(
            "ExecStart=/program /other master.example.org\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            ValueError, "timer already exists for a different setup"
        ):
            renewal.install_renewal_timer(self.installation, self.domain)
        self.runtime.assert_not_called()
        self.systemctl.assert_not_called()
        self.assertIn("/other", self.service.read_text(encoding="utf-8"))

    def test_failed_timer_activation_restores_existing_files(self) -> None:
        previous = f"ExecStart=/program {self.installation.resolve()} {self.domain}\n"
        self.service.write_text(previous, encoding="utf-8")
        self.timer.write_text("[Timer]\nPersistent=false\n", encoding="utf-8")
        self.systemctl.side_effect = [None, CalledProcessError(1, "systemctl")]
        with self.assertRaises(CalledProcessError):
            renewal.install_renewal_timer(self.installation, self.domain)
        self.assertEqual(self.service.read_text(encoding="utf-8"), previous)
        self.assertEqual(
            self.timer.read_text(encoding="utf-8"), "[Timer]\nPersistent=false\n"
        )
        self.repair.assert_called_once_with(
            ["systemctl", "daemon-reload"], check=False, capture_output=True
        )
