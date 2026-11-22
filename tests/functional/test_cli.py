"""Exercise the public shell entry points in the Debian sandbox."""

import errno
import functools
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import ssl
import stat
import subprocess
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from threading import Thread
from time import monotonic
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

    @contextmanager
    def install_terminal(
        self, directory: Path, env: dict[str, str],
    ) -> Iterator[tuple[Callable[[str, str | None], None], Callable[[int], str]]]:
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(directory)
            os.execve(ROOT / "install.sh", [str(ROOT / "install.sh")], env)

        output = bytearray()
        reaped = False

        def read() -> bool:
            if not select.select([terminal], [], [], 10)[0]:
                self.fail(f"Installer stopped responding: {output.decode(errors='replace')}")
            try:
                chunk = os.read(terminal, 4096)
            except OSError as exc:
                # Linux reports EIO when the final process closes the peer terminal.
                if exc.errno != errno.EIO:
                    raise
                return False
            output.extend(chunk)
            return bool(chunk)

        def expect(prompt: str, answer: str | None = None) -> None:
            deadline = monotonic() + 15
            while prompt.encode() not in output:
                if monotonic() >= deadline or not read():
                    self.fail(f"Missing prompt {prompt!r}: {output.decode(errors='replace')}")
            if answer is not None:
                os.write(terminal, answer.encode())

        def finish(code: int) -> str:
            nonlocal reaped
            while read():
                pass
            _, status = os.waitpid(pid, 0)
            reaped = True
            transcript = output.decode(errors="replace")
            self.assertEqual(os.waitstatus_to_exitcode(status), code, transcript)
            return transcript

        try:
            yield expect, finish
        finally:
            os.close(terminal)
            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)

    def test_install_script_resumes_sni_after_ctrl_c_from_another_directory(self) -> None:
        temporary = TemporaryDirectory(dir="/var/lib")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        draft = root / "state" / "install-draft.json"
        target = root / "custom-panel"
        other = self.root / "other"
        other.mkdir()
        (self.root / "sitecustomize.py").write_text(dedent(f"""
            from pathlib import Path
            from setup3ui import install_draft, interactive
            from setup3ui import cli
            from setup3ui.deploy import install as deployment
            from setup3ui.panel.bootstrap import credentials

            install_draft.STATE_DIRECTORY = Path({str(draft.parent)!r})
            cli._managed_installation = lambda default: None

            def scan(*args):
                print("fixture scan executed")
                return ["second.example.net", "first.example.net"]

            def fail_after_credentials(config, ports, *, source, interactive):
                credentials(config, interactive=interactive)
                raise ValueError("fixture deploy failure")

            interactive.scan = scan
            deployment.install = fail_after_credentials
        """), encoding="utf-8")
        env = os.environ | {"PYTHONPATH": f"{self.root}:{ROOT}", "SUDO_USER": "fixture"}

        with self.install_terminal(self.root, env) as (expect, finish):
            expect("existing panel [install]:", "\n")
            expect("Installation directory [/opt/3x-ui]:", f"{root / 'missing' / 'panel'}\n")
            expect("The installation parent directory does not exist", f"{target}\n")
            expect("HTTPS with certbot or caddy", "caddy\n")
            expect("Main domain:", "example.org\n")
            expect("Panel domain", "\n")
            expect("Subscription domain", "\n")
            expect("ACME contact email:", "test@example.org\n")
            expect("Panel port", "\n")
            expect("Subscription port", "\n")
            expect("Use TCP REALITY", "y\n")
            expect("Use direct XHTTP REALITY", "y\n")
            expect("TCP REALITY backend port", "\n")
            expect("Direct XHTTP port", "\n")
            expect("Direct XHTTP path, or generate", "generate\n")
            expect("Add a CDN XHTTP backend", "n\n")
            expect("Find REALITY SNI automatically?", "y\n")
            expect("REALITY scan type", "top10\n")
            expect("REALITY SNI number", "\x03")
            self.assertIn("fixture scan executed", finish(130))

        state = json.loads(draft.read_text())
        self.assertEqual(state["answers"]["install_dir"], str(target))
        self.assertEqual(stat.S_IMODE(draft.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(draft.parent.stat().st_mode), 0o700)
        self.assertEqual(draft.stat().st_uid, 0)
        self.assertFalse(target.exists())

        with self.install_terminal(other, env) as (expect, finish):
            expect("Continue the unfinished installation or start over?", "continue\n")
            expect("REALITY SNI number", "2\n")
            expect("Install with caddy?", "y\n")
            expect("Panel username:", "fixture-operator\n")
            expect("Panel password:", "fixture-only-password\n")
            output = finish(1)
        self.assertIn("1. second.example.net\r\n2. first.example.net", output)
        self.assertIn("first.example.net:443", output)
        self.assertIn(state["answers"]["direct_xhttp_path"], output)
        self.assertNotIn("fixture scan executed", output)
        self.assertNotIn("HTTPS with certbot or caddy", output)
        self.assertIn("fixture deploy failure", output)
        self.assertNotIn("fixture-only-password", output)
        self.assertNotIn("fixture-only-password", draft.read_text())
        self.assertNotIn("fixture-operator", draft.read_text())

        with self.install_terminal(other, env) as (expect, finish):
            expect("Continue the unfinished installation or start over?", "start over\n")
            expect("existing panel [install]:", "\x03")
            finish(130)
        self.assertFalse(draft.exists())

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

    def test_shell_resolves_yaml_sni_before_install_or_reconfigure(self) -> None:
        directory = self.root / "install"
        saved = directory / "config.yml"
        openssl = self.root / "openssl"
        openssl.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" -servername $SNI_FIXTURE_OK "*) echo "ALPN protocol: h2";; *) exit 1;; esac\n',
            encoding="utf-8",
        )
        openssl.chmod(0o755)
        # Fixture the deployment boundary. Integration tests exercise the real panel and Caddy.
        (self.root / "sitecustomize.py").write_text(
            dedent("""
                from contextlib import nullcontext
                from pathlib import Path
                from unittest.mock import patch

                import yaml

                def save(config):
                    path = Path(config["install_dir"]) / "config.yml"
                    path.parent.mkdir(exist_ok=True)
                    path.write_text(yaml.safe_dump({
                        key: value for key, value in config.items() if not key.startswith("admin_")
                    }))

                def install(config, *args, **kwargs):
                    save(config)
                    print("Fixture installed.")

                def apply_config(directory, current, desired, auth, *, interactive):
                    save(desired)
                    print("Fixture reconfigured.")

                patch("setup3ui.deploy.install.install", install).start()
                patch("setup3ui.deploy.reconfigure.apply_config", apply_config).start()
                patch("setup3ui.deploy.managed.operation_lock", nullcontext).start()
                patch("setup3ui.deploy.managed.managed_directory", lambda path: path).start()
                patch("setup3ui.cli.deploy_when_requested").start()
            """),
            encoding="utf-8",
        )
        self.config.write_text(
            f"domain: example.org\nemail: test@example.org\nhttps: caddy\nreality: true\n"
            f"install_dir: {directory}\nreality_sni_auto: true\n"
            "reality_sni: [bad.example.org, backup.example.org]\nadmin_password: fixture-secret\n",
            encoding="utf-8",
        )

        def run(config: Path, compatible: str) -> subprocess.CompletedProcess[str]:
            env = os.environ | {
                "PATH": f"{self.root}:{os.environ['PATH']}",
                "PYTHONPATH": f"{self.root}:{ROOT}",
                "SNI_FIXTURE_OK": compatible,
            }
            return subprocess.run(
                [str(ROOT / "install.sh"), "--config", str(config)],
                cwd=self.root, env=env, text=True, capture_output=True, timeout=30, check=False,
            )

        result = run(self.config, "www.apple.com")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fixture installed.", result.stdout)
        self.assertNotIn("fixture-secret", result.stdout + result.stderr)
        config = yaml.safe_load(saved.read_text())
        self.assertEqual(config["reality_sni"], ["www.apple.com"])
        self.assertEqual(config["reality_target"], "www.apple.com:443")
        self.assertNotIn("reality_sni_auto", config)
        self.assertNotIn("admin_password", config)
        result = run(saved, "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Settings already match.", result.stdout)
        result = run(self.config, "backup.example.org")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fixture reconfigured.", result.stdout)
        self.assertEqual(yaml.safe_load(saved.read_text())["reality_sni"], ["backup.example.org"])
        before = saved.read_bytes()
        result = run(self.config, "")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("No fallback REALITY SNI in reality_sni passed", result.stderr)
        self.assertEqual(saved.read_bytes(), before)

    def test_installer_update_replaces_cli_only_after_hash_check(self) -> None:
        self.assertEqual(
            os.environ.get("SUITE"),
            "trixie",
            "Run in the Debian 13 sandbox.",
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
