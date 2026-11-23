"""Restore and migrate a panel database with Caddy and a real 2FA login."""

import base64
import contextlib
import hmac
import io
import os
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import yaml

from setup3ui import cli as setup_3x_ui
from setup3ui.config import port_plan, validate_config
from setup3ui.deploy import install as setup
from setup3ui.deploy import managed as managed_install
from setup3ui.deploy.caddy import render_caddy
from setup3ui.panel import inbounds as xray_inbounds
from setup3ui.panel import restore as restore_panel
from setup3ui.panel import tls as panel_tls
from setup3ui.panel.api import PanelAPI


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def code(secret: str) -> str:
    key = base64.b32decode(secret)
    mac = hmac.digest(key, struct.pack(">Q", int(time.time()) // 30), "sha1")
    index = mac[-1] & 15
    return (
        f"{(struct.unpack('>I', mac[index : index + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"
    )


class SandboxTest(TestCase):
    def test_offline_restore(self) -> None:
        with tempfile.TemporaryDirectory(prefix="3xui-caddy-") as td:
            root = Path(td).resolve()
            directory = root / "install"
            project = root.name
            external = free_port()
            config = validate_config(
                {
                    "https": "caddy",
                    "nftguard": {"enabled": False},
                    "domain": "example.org",
                    "email": "check@example.org",
                    "install_dir": str(directory),
                    "admin_username": "fixture",
                    "admin_password": "fixture auth password",
                    "reality": True,
                    "direct_xhttp": True,
                    "reality_sni": ["reality.example.org"],
                    "cdn": [
                        {"provider": "selectel", "edge": "edge1.example.org"},
                        {
                            "provider": "yandex",
                            "edge": "edge2.example.org",
                            "path": "generate",
                        },
                    ],
                }
            )
            normal = render_caddy(config)

            def docker(
                *argv: str, capture: bool = False, input_text: str | None = None, timeout: float | None = None,
            ) -> subprocess.CompletedProcess[str]:
                args = list(argv)
                if args[0] == "compose":
                    args[1:1] = ["-p", project]
                    fileindex = args.index("-f") + 1
                    fixture = yaml.safe_load(Path(args[fileindex]).read_text())
                    fixture["services"]["panel"]["network_mode"] = "service:caddy"
                    caddy = fixture["services"]["caddy"]
                    del caddy["network_mode"]
                    caddy["ports"] = [f"127.0.0.1:{external}:443"]
                    testfile = root / "compose-test.yml"
                    testfile.write_text(yaml.safe_dump(fixture))
                    args[fileindex] = str(testfile)
                return subprocess.run(
                    ["docker", *args],
                    check=True,
                    text=True,
                    capture_output=capture,
                    input=input_text,
                    timeout=timeout,
                )

            def local_cert(config):
                before, snippet = normal.split("(public_tls) {\n", 1)
                _, after = snippet.split("\n}\n", 1)
                return before + "(public_tls) {\n\ttls internal\n}\n" + after

            original_https = setup.verify_https

            def verify(domain, port, path, expected):
                ca = directory / "caddy-data/caddy/pki/authorities/local/root.crt"
                for _ in range(20):
                    if ca.is_file():
                        break
                    time.sleep(0.5)
                os.environ["SSL_CERT_FILE"] = str(ca)
                original_https(domain, external, path, expected)

            base = (
                "compose",
                "--project-directory",
                str(directory),
                "-f",
                str(directory / "compose.yml"),
            )
            with (
                patch.object(os, "geteuid", return_value=0),
                patch.object(setup, "prepare_docker"),
                patch.object(setup, "grant_installation_access"),
                patch.object(setup, "check_dns"),
                patch.object(setup, "check_host"),
                patch.object(setup, "check_root_owned_directory"),
                patch.object(setup, "docker", docker),
                patch.object(managed_install, "docker", docker),
                patch.object(restore_panel, "verify_https", verify),
                patch.object(panel_tls, "docker", docker),
                patch.object(xray_inbounds, "docker", docker),
                patch.object(setup, "render_caddy", local_cert),
                patch.object(setup, "verify_https", verify),
                patch.object(
                    setup,
                    "PanelAPI",
                    side_effect=lambda domain, port, path: PanelAPI(
                        domain, external, path
                    ),
                ),
            ):
                try:
                    with contextlib.redirect_stdout(io.StringIO()) as out:
                        setup.install(config, port_plan(config))
                    assert out.getvalue().count("Added inbound") == 4, (
                        out.getvalue().splitlines()[:4]
                    )
                    database = directory / "db/x-ui.db"
                    urls = dict(
                        line.split(": ", 1)
                        for line in out.getvalue().splitlines()
                        if line.startswith(("Panel: ", "Subscription base URL: "))
                    )
                    destination = {
                        "webBasePath": "/" + urls["Panel"].split("/", 3)[3],
                        "subPath": "/" + urls["Subscription base URL"].split("/", 3)[3],
                    }
                    source_api = PanelAPI(
                        config["panel_domain"], external, destination["webBasePath"]
                    )
                    source_api.login(config["admin_username"], config["admin_password"])
                    inbound = next(
                        item
                        for item in source_api.call("panel/api/inbounds/list")
                        if item["port"] == 8080
                    )
                    source_api.call(
                        "panel/api/clients/add",
                        {
                            "client": {
                                "id": str(uuid4()),
                                "email": "backup-client",
                                "enable": True,
                                "subId": "fixture-sub",
                            },
                            "inboundIds": [inbound["id"]],
                        },
                    )
                    docker(*base, "stop", "panel")
                    source = root / "provided.db"
                    restore_panel.stage_database(database, source)
                    secret = "JBSWY3DPEHPK3PXP"
                    with closing(sqlite3.connect(source)) as db, db:
                        db.execute("UPDATE users SET username='backup-admin'")
                        db.execute(
                            "UPDATE settings SET value='/source-panel/' WHERE key='webBasePath'"
                        )
                        db.execute(
                            "UPDATE settings SET value='/source-sub/' WHERE key='subPath'"
                        )
                        db.execute(
                            "UPDATE settings SET value='source-panel-guid' WHERE key='panelGuid'"
                        )
                        db.executemany(
                            "DELETE FROM settings WHERE key=?",
                            [("twoFactorEnable",), ("twoFactorToken",)],
                        )
                        db.executemany(
                            "INSERT INTO settings (key,value) VALUES (?,?)",
                            [("twoFactorEnable", "true"), ("twoFactorToken", secret)],
                        )
                        db.execute("DELETE FROM inbounds WHERE port=8443")
                    before = source.read_bytes()
                    dest_secret = "KRSXG5CSMFXGI33N"
                    with closing(sqlite3.connect(database)) as db, db:
                        db.executemany(
                            "DELETE FROM settings WHERE key=?",
                            [("twoFactorEnable",), ("twoFactorToken",)],
                        )
                        db.executemany(
                            "INSERT INTO settings (key,value) VALUES (?,?)",
                            [
                                ("twoFactorEnable", "true"),
                                ("twoFactorToken", dest_secret),
                            ],
                        )
                    docker(*base, "up", "-d", "--no-deps", "panel")
                    invalid = root / "bad.db"
                    invalid.write_text("not sqlite")
                    with patch.object(
                        restore_panel, "compose", wraps=restore_panel.compose
                    ) as compose:
                        try:
                            restore_panel.restore_panel(directory, config, invalid)
                            raise AssertionError("Expected bad backup error")
                        except ValueError as exc:
                            assert "Invalid 3x-ui backup" in str(exc)
                        compose.assert_not_called()
                    with patch.object(
                        setup_3x_ui.sys.stdin, "isatty", return_value=False
                    ):
                        try:
                            setup_3x_ui.restore(directory, source, False)
                            raise AssertionError("Expected explicit confirmation")
                        except ValueError as exc:
                            assert "--yes" in str(exc)

                    def fail_once(*args):
                        if not getattr(fail_once, "failed", False):
                            fail_once.failed = True
                            raise ValueError("Injected health failure")
                        return verify(*args)

                    with patch.object(restore_panel, "verify_https", fail_once):
                        try:
                            restore_panel.restore_panel(directory, config, source)
                            raise AssertionError("Expected rollback")
                        except ValueError as exc:
                            assert "Injected health" in str(exc)
                    original = PanelAPI(
                        config["panel_domain"], external, destination["webBasePath"]
                    )
                    assert original.needs_two_factor()
                    original.login(
                        config["admin_username"],
                        config["admin_password"],
                        code(dest_secret),
                    )
                    assert len(original.call("panel/api/inbounds/list")) == 4
                    with patch.object(
                        PanelAPI,
                        "login",
                        side_effect=AssertionError("Restore must not log in"),
                    ):
                        job = root / "restore.yml"
                        job.write_text(
                            yaml.safe_dump(
                                {
                                    "action": "restore",
                                    "backup_file": str(source),
                                    "install_dir": str(directory),
                                    "confirm": True,
                                }
                            )
                        )
                        with (
                            patch.object(managed_install, "check_root_owned_directory"),
                            patch.object(
                                managed_install,
                                "operation_lock",
                                side_effect=contextlib.nullcontext,
                            ),
                        ):
                            setup_3x_ui.main(["--config", str(job)])
                        backup = max(
                            item
                            for item in (directory / "backups").iterdir()
                            if item.is_dir()
                        )
                    api = PanelAPI(
                        config["panel_domain"], external, destination["webBasePath"]
                    )
                    assert api.needs_two_factor()
                    api.login("backup-admin", config["admin_password"], code(secret))
                    assert len(api.call("panel/api/inbounds/list")) == 3
                    assert "backup-client" in str(api.call("panel/api/clients/list"))
                    docker(*base, "stop", "panel")
                    assert (
                        restore_panel.settings(database)["subPath"]
                        == destination["subPath"]
                    )
                    assert (
                        restore_panel.settings(database)["panelGuid"]
                        == "source-panel-guid"
                    )
                    assert (
                        source.read_bytes() == before and (backup / "x-ui.db").is_file()
                    )
                    print(
                        "PASS: old panel replacement without login, preserved 2FA and routes, invalid backup, confirmation and rollback."
                    )
                    docker(*base, "down")
                    directory = root / "fresh"
                    base = (
                        "compose",
                        "--project-directory",
                        str(directory),
                        "-f",
                        str(directory / "compose.yml"),
                    )
                    config["install_dir"] = str(directory)
                    del config["admin_username"]
                    password = config.pop("admin_password")
                    with (
                        contextlib.redirect_stdout(io.StringIO()) as output,
                        patch.object(
                            PanelAPI,
                            "login",
                            side_effect=AssertionError(
                                "Install from backup must not log in"
                            ),
                        ),
                    ):
                        setup.install(config, port_plan(config), source=source)
                    assert "Node API token:" not in output.getvalue()
                    api = PanelAPI(config["panel_domain"], external, "/source-panel/")
                    assert api.needs_two_factor()
                    api.login("backup-admin", password, code(secret))
                    assert len(api.call("panel/api/inbounds/list")) == 3
                    assert "backup-client" in str(api.call("panel/api/clients/list"))
                    assert source.read_bytes() == before
                    print(
                        "PASS: offline source on fresh install, preserved login and TOTP, no new token."
                    )
                    legacy = root / "legacy-db"
                    legacy.mkdir()
                    docker(
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "-e",
                        "XUI_DB_FOLDER=/etc/x-ui",
                        "-v",
                        f"{legacy}:/etc/x-ui",
                        "--entrypoint",
                        "/app/x-ui",
                        "ghcr.io/mhsanaei/3x-ui:2.8.11",
                        "setting",
                        "-username",
                        "legacy-user",
                        "-password",
                        "old-backup-fixture",
                    )
                    docker(*base, "down")
                    directory = root / "legacy-restored"
                    base = (
                        "compose",
                        "--project-directory",
                        str(directory),
                        "-f",
                        str(directory / "compose.yml"),
                    )
                    config["install_dir"] = str(directory)
                    legacy_path = restore_panel.panel_paths(legacy / "x-ui.db")[0]
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        setup.install(
                            config, port_plan(config), source=legacy / "x-ui.db"
                        )
                    legacy_api = PanelAPI(config["panel_domain"], external, legacy_path)
                    legacy_api.login("legacy-user", "old-backup-fixture")
                    print(
                        "PASS: 3x-ui 2.8.11 SQLite database migrated to current image and original login works."
                    )
                finally:
                    if (directory / "compose.yml").is_file():
                        docker(*base, "down")
