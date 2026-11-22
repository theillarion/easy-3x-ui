"""Exercise the public shell entry points in the Debian sandbox."""

import functools
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tarfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase

import yaml

ROOT = Path(__file__).resolve().parents[2]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class CliTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.yml"
        self.config.write_text(
            "domain: example.org\nemail: test@example.org\nreality: true\n",
            encoding="utf-8",
        )

    def run_cli(self, launcher: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(launcher), *args, "--config", str(self.config)],
            check=False,
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=30,
        )

    def test_validate_and_render_from_other_directory(self) -> None:
        result = self.run_cli(ROOT / "setup-3x-ui", "validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ports"]["TCP REALITY"], 443)
        result = self.run_cli(ROOT / "setup-3x-ui", "render-compose")
        self.assertEqual(result.returncode, 0, result.stderr)
        compose = yaml.safe_load(result.stdout)
        self.assertIn("certbot", compose["services"])
        self.assertEqual(compose["services"]["panel"]["network_mode"], "host")

    def test_bad_config_and_force(self) -> None:
        with self.config.open("a", encoding="utf-8") as file:
            file.write("unknown: true\n")
        result = self.run_cli(ROOT / "setup-3x-ui", "validate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown config fields: unknown.", result.stderr)
        result = self.run_cli(ROOT / "setup-3x-ui", "validate", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ignoring unknown config fields: unknown.", result.stderr)
        result = self.run_cli(ROOT / "setup-3x-ui", "render-caddy", "--force")
        self.assertEqual(result.returncode, 1)
        self.assertIn("render-caddy requires https: caddy.", result.stderr)

    def test_install_script_runs_checkout(self) -> None:
        self.assertEqual(os.geteuid(), 0, "Run these tests in the Debian sandbox.")
        result = self.run_cli(ROOT / "install.sh", "validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["https"], "certbot")

    def test_install_script_runs_from_https(self) -> None:
        self.assertEqual(os.geteuid(), 0, "Run these tests in the Debian sandbox.")
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        shutil.copyfile(ROOT / "install.sh", self.root / "install.sh")
        with tarfile.open(self.root / "source.tar.gz", "w:gz") as archive:
            for name in ("setup_3x_ui.py", "setup3ui"):
                archive.add(ROOT / name, arcname=f"easy-3x-ui-master/{name}")
        handler = functools.partial(QuietHandler, directory=self.root)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                base_url = f"https://127.0.0.1:{server.server_port}"
                env = os.environ | {
                    "CURL_CA_BUNDLE": str(cert),
                    "SETUP_3X_UI_SOURCE_URL": f"{base_url}/source.tar.gz",
                }
                result = subprocess.run(
                    [
                        "sh",
                        "-c",
                        'url=$1; shift; sh -c "$(curl -fsSL "$url/install.sh")" -- "$@"',
                        "test-install",
                        base_url,
                        "validate",
                        "--config",
                        str(self.config),
                    ],
                    check=False,
                    cwd=self.root,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=45,
                )
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ports"]["TCP REALITY"], 443)

    def test_installer_update_replaces_cli_only_after_hash_check(self) -> None:
        self.assertIn(
            os.environ.get("SUITE"),
            {"bookworm", "trixie"},
            "Run in the Debian sandbox.",
        )
        archive = self.root / "installer.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(ROOT / "setup_3x_ui.py", arcname="setup_3x_ui.py")
            bundle.add(
                ROOT / "setup3ui",
                arcname="setup3ui",
                filter=lambda entry: (
                    None if "__pycache__" in Path(entry.name).parts else entry
                ),
            )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        command = [
            str(ROOT / "setup-3x-ui"),
            "update",
            "--component",
            "installer",
            "--installer-source",
            str(archive),
            "--sha256",
        ]
        launcher = Path("/usr/local/bin/setup-3x-ui")
        before = launcher.read_bytes() if launcher.exists() else None
        result = subprocess.run(
            [*command, "0" * 64],
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256 does not match", result.stderr)
        self.assertEqual(launcher.read_bytes() if launcher.exists() else None, before)
        result = subprocess.run(
            [*command, digest],
            cwd=self.root,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installer files updated.", result.stdout)
        result = self.run_cli(launcher, "validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["ports"]["TCP REALITY"], 443)
