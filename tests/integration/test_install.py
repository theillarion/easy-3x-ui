"""Run a fresh install against a real panel and roll back on a failed check."""

import contextlib
import io
import os
import socket
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import yaml

from setup3ui import cli
from setup3ui.config import validate_config
from setup3ui.deploy import install as setup
from setup3ui.panel import tls as panel_tls


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SandboxTest(TestCase):
    def test_install(self) -> None:
        with tempfile.TemporaryDirectory(prefix="3xui-install-") as td:
            root = Path(td).resolve()
            root.chmod(0o755)
            project = root.name
            directory = root / "install"
            config = {
                "https": "certbot",
                "reality": False,
                "direct_xhttp": False,
                "cdn": [],
                "install_dir": str(directory),
                "panel_domain": "master.example.org",
                "subscription_domain": "gateway.example.org",
                "panel_port": free_port(),
                "subscription_port": free_port(),
                "email": "test@example.org",
                "docker_user": "nobody",
                "admin_username": "fixture",
                "admin_password": "a secret with $ signs",
            }
            chain = root / "tls.crt"
            key = root / "tls.key"
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
                    str(chain),
                    "-subj",
                    "/CN=master.example.org",
                    "-addext",
                    "subjectAltName=DNS:master.example.org,DNS:gateway.example.org",
                ],
                check=True,
                capture_output=True,
            )

            def docker(
                *argv: str, capture: bool = False, input_text: str | None = None
            ) -> subprocess.CompletedProcess[str]:
                args = list(argv)
                if args[0] == "compose":
                    args[1:1] = ["-p", project]
                    fileindex = args.index("-f") + 1
                    fixture = yaml.safe_load(Path(args[fileindex]).read_text())
                    panel = fixture["services"]["panel"]
                    del panel["network_mode"]
                    panel["ports"] = [
                        f"127.0.0.1:{config[f]}:{config[f]}"
                        for f in ("panel_port", "subscription_port")
                    ]
                    testfile = root / "compose-test.yml"
                    testfile.write_text(yaml.safe_dump(fixture))
                    args[fileindex] = str(testfile)
                return subprocess.run(
                    ["docker", *args],
                    check=True,
                    text=True,
                    capture_output=capture,
                    input=input_text,
                )

            def issue(path, panel, sub, email):
                target = path / "certs" / "live" / panel
                target.mkdir(parents=True)
                (target / "fullchain.pem").write_bytes(chain.read_bytes())
                (target / "privkey.pem").write_bytes(key.read_bytes())
                for name in ("certbot-work", "certbot-log"):
                    (path / name).mkdir()

            base = (
                "compose",
                "--project-directory",
                str(directory),
                "-f",
                str(directory / "compose.yml"),
            )
            with (
                patch.object(os, "geteuid", return_value=0),
                patch.dict(os.environ, {"SSL_CERT_FILE": str(chain)}),
                patch.object(setup, "prepare_docker"),
                patch.object(setup, "check_dns"),
                patch.object(setup, "check_host"),
                patch.object(setup, "check_root_owned_directory"),
                patch.object(setup, "issue_certificate", issue),
                patch.object(setup, "docker", docker),
                patch.object(panel_tls, "docker", docker),
                patch.object(setup, "install_renewal_timer") as timer,
            ):
                try:
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        setup.install(validate_config(config), {})
                    assert "Node API token: " in out.getvalue()
                    assert timer.call_count == 1
                    subprocess.run(
                        [
                            "runuser", "-u", "nobody", "--", "sh", "-ec",
                            'cd "$1"; test -r compose.yml; test -r config.yml; '
                            'test -r db/x-ui.db; test ! -w db/x-ui.db',
                            "sh", str(directory),
                        ],
                        check=True,
                    )
                    saved = yaml.safe_load((directory / "config.yml").read_text())
                    assert all(not k.startswith("admin_") for k in saved)
                    with closing(sqlite3.connect(directory / "db/x-ui.db")) as db, db:
                        rows = dict(db.execute("SELECT key, value FROM settings"))
                        assert rows["webBasePath"] not in ("/", "/admin/")
                        assert rows["subPath"] not in ("/", "/sub/")
                        assert "/var/log/x-ui/access.log" in rows["xrayTemplateConfig"]
                        assert db.execute(
                            "SELECT count(*) FROM inbounds"
                        ).fetchone() == (0,)
                        assert db.execute("SELECT username FROM users").fetchone() == (
                            "fixture",
                        )
                    job = root / "install.yml"
                    job.write_text(yaml.safe_dump(config), encoding="utf-8")
                    before = (directory / "config.yml").read_bytes()
                    with (
                        patch(
                            "setup3ui.deploy.managed.operation_lock",
                            contextlib.nullcontext,
                        ),
                        patch("setup3ui.deploy.install.install") as reinstall,
                        patch(
                            "setup3ui.deploy.reconfigure.apply_config"
                        ) as reconfigure,
                        contextlib.redirect_stdout(io.StringIO()) as repeated,
                    ):
                        cli.main(["install", "--config", str(job)])
                    self.assertIn("Settings already match.", repeated.getvalue())
                    self.assertEqual((directory / "config.yml").read_bytes(), before)
                    reinstall.assert_not_called()
                    reconfigure.assert_not_called()
                    try:
                        setup.install(config, {})
                    except ValueError as exc:
                        assert "already exists" in str(exc)
                    else:
                        raise AssertionError(
                            "Expected refusal to replace existing setup"
                        )
                    docker(*base, "down")
                    config["install_dir"] = str(root / "rollback")
                    timer.side_effect = ValueError("Injected timer failure")
                    try:
                        setup.install(config, {})
                    except ValueError as exc:
                        assert "Injected timer failure" in str(exc)
                    else:
                        raise AssertionError("Expected timer failure")
                    assert not Path(config["install_dir"]).exists()
                    assert not docker(
                        "ps",
                        "-aq",
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                        capture=True,
                    ).stdout
                    print(
                        "PASS: fresh database, node token, verified HTTPS, random paths, clean config, rollback."
                    )
                finally:
                    docker(*base, "down")
