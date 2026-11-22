"""Send a test HTTPS request through each Xray transport and Caddy."""

import contextlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path
from textwrap import dedent
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import yaml

from setup3ui import ASSETS
from setup3ui.config import port_plan, validate_config
from setup3ui.deploy import install as setup
from setup3ui.deploy.caddy import render_caddy
from setup3ui.diagnostics.connections import _probe
from setup3ui.panel import inbounds as xray_inbounds
from setup3ui.panel import tls as panel_tls
from setup3ui.panel.api import PanelAPI


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TransportTests(TestCase):
    def test_authenticated_transports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="3xui-caddy-") as td:
            root = Path(td).resolve()
            directory = root / "install"
            project = root.name
            external = free_port()
            socks_external = [free_port() for _ in range(4)]
            custom_template = root / "custom-xhttp.json"
            template = json.loads(
                (ASSETS / "templates" / "direct-xhttp.json").read_text(encoding="utf-8")
            )
            template["remark"] = "Custom XHTTP fixture"
            custom_template.write_text(json.dumps(template), encoding="utf-8")
            config = validate_config(
                {
                    "https": "caddy",
                    "domain": "example.org",
                    "email": "check@example.org",
                    "install_dir": str(directory),
                    "admin_username": "fixture",
                    "admin_password": "fixture auth password",
                    "reality": True,
                    "direct_xhttp": True,
                    "inbound_templates": {"direct_xhttp": str(custom_template)},
                    "reality_sni": ["reality.example.org"],
                    "cdn": [
                        {"provider": "selectel", "edge": "edge1.example.org"},
                        {
                            "provider": "yandex",
                            "edge": "edge2.example.org",
                            "path": "generate",
                            "tag": "cdn-test-yandex",
                        },
                    ],
                }
            )
            config["reality_target"] = "target.example.org:18445"
            normal = render_caddy(config)
            cert, key = root / "fixture.crt", root / "fixture.key"
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "ec",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=reality.example.org",
                    "-addext",
                    "subjectAltName=DNS:reality.example.org",
                    "-out",
                    str(cert),
                    "-keyout",
                    str(key),
                ],
                check=True,
                capture_output=True,
            )
            (root / "target.py").write_text(
                dedent("""
                    import http.server
                    import ssl

                    server = http.server.HTTPServer(("0.0.0.0", 18445), http.server.SimpleHTTPRequestHandler)
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain("/fixture/fixture.crt", "/fixture/fixture.key")
                    context.set_alpn_protocols(["h2", "http/1.1"])
                    context.set_ecdh_curve("X25519")
                    server.socket = context.wrap_socket(server.socket, server_side=True)
                    server.serve_forever()
                    """),
                encoding="utf-8",
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
                    panel["network_mode"] = "service:caddy"
                    panel["volumes"].append("./caddy-data:/test-ca:ro")
                    caddy = fixture["services"]["caddy"]
                    caddy["extra_hosts"] = ["node.example.org:127.0.0.1"]

                    del caddy["network_mode"]
                    caddy["ports"] = [
                        f"127.0.0.1:{external}:443",
                        *[
                            f"127.0.0.1:{port}:{18080 + i}"
                            for i, port in enumerate(socks_external)
                        ],
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

            actual_configure = setup.configure_panel_tls

            def configure(*args, **kwargs):
                result = actual_configure(*args, **kwargs)
                with closing(sqlite3.connect(directory / "db/x-ui.db")) as db, db:
                    original = json.loads(
                        db.execute(
                            "SELECT value FROM settings WHERE key='xrayTemplateConfig'"
                        ).fetchone()[0]
                    )
                    for outbound in original["outbounds"]:
                        if outbound["protocol"] == "freedom":
                            outbound.setdefault("settings", {})["finalRules"] = [
                                {"action": "allow", "ip": ["127.0.0.1"], "port": "443"}
                            ]
                            break
                    db.execute(
                        "UPDATE settings SET value=? WHERE key='xrayTemplateConfig'",
                        (json.dumps(original),),
                    )
                return result

            base = (
                "compose",
                "--project-directory",
                str(directory),
                "-f",
                str(directory / "compose.yml"),
            )
            with (
                patch.dict(os.environ),
                patch.object(os, "geteuid", return_value=0),
                patch.object(setup, "prepare_docker"),
                patch.object(setup, "grant_installation_access"),
                patch.object(setup, "configure_panel_tls", configure),
                patch.object(setup, "check_dns"),
                patch.object(setup, "check_host"),
                patch.object(setup, "check_root_owned_directory"),
                patch.object(setup, "docker", docker),
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
                    verify("node.example.org", 443, "/", {200})
                    verify("node.example.org", 443, "/no-such-file", {404})
                    ca = directory / "caddy-data/caddy/pki/authorities/local/root.crt"
                    os.environ["CURL_CA_BUNDLE"] = str(ca)
                    with closing(sqlite3.connect(directory / "db/x-ui.db")) as db, db:
                        panel_path = db.execute(
                            "SELECT value FROM settings WHERE key='webBasePath'"
                        ).fetchone()[0]
                    api = PanelAPI(config["panel_domain"], external, panel_path)
                    api.login(config["admin_username"], config["admin_password"])
                    inbounds = sorted(
                        api.call("panel/api/inbounds/list"),
                        key=lambda entry: entry["port"],
                    )
                    direct = next(item for item in inbounds if item["port"] == 8443)
                    self.assertEqual(direct["remark"], "Custom XHTTP fixture")
                    for inbound in (direct, next(item for item in inbounds if item["port"] == 8444)):
                        stream = inbound["streamSettings"]
                        if isinstance(stream, str):
                            stream = json.loads(stream)
                        reality = stream["realitySettings"]
                        self.assertEqual(reality["serverNames"], ["reality.example.org"])
                        self.assertEqual(reality["target"], "target.example.org:18445")
                        self.assertEqual(reality["settings"]["serverName"], "reality.example.org")
                    self.assertIn(
                        "@reality tls sni reality.example.org",
                        (directory / "caddy-config" / "Caddyfile").read_text(),
                    )
                    cdn_tags = {
                        item["port"]: item["tag"]
                        for item in inbounds
                        if item["port"] in (8080, 8081)
                    }
                    self.assertEqual(cdn_tags[8080], "cdn-selectel-443")
                    self.assertEqual(cdn_tags[8081], "cdn-test-yandex")
                    client = str(uuid4())
                    api.call(
                        "panel/api/clients/add",
                        {
                            "client": {
                                "id": client,
                                "email": "transports-fixture",
                                "enable": True,
                                "subId": "xtest",
                            },
                            "inboundIds": [item["id"] for item in inbounds],
                        },
                    )
                    api.call("panel/api/server/restartXrayService", {})
                    docker(
                        "run",
                        "--rm",
                        "-d",
                        "--name",
                        project + "-target",
                        "--network",
                        project + "_default",
                        "--network-alias",
                        "target.example.org",
                        "-v",
                        f"{root}:/fixture:ro",
                        "python:3.13-slim-trixie",
                        "python",
                        "/fixture/target.py",
                    )
                    client_config = {
                        "log": {"loglevel": "debug"},
                        "inbounds": [],
                        "outbounds": [],
                        "routing": {"rules": []},
                    }
                    for index, item in enumerate(inbounds):
                        settings = item["streamSettings"]
                        stream = {"network": settings["network"]}
                        if "xhttpSettings" in settings:
                            stream["xhttpSettings"] = settings["xhttpSettings"].copy()
                        if settings["security"] == "reality":
                            stream["security"] = "reality"
                            rs = settings["realitySettings"]
                            stream["realitySettings"] = {
                                "fingerprint": "chrome",
                                "serverName": "reality.example.org",
                                "password": rs["settings"]["publicKey"],
                                "shortId": rs["shortIds"][0],
                                "spiderX": "/",
                            }
                        else:
                            stream["security"] = "tls"
                            stream["xhttpSettings"]["host"] = "node.example.org"
                            stream["tlsSettings"] = {
                                "serverName": "node.example.org",
                                "certificates": [
                                    {
                                        "usage": "verify",
                                        "certificateFile": "/test-ca/caddy/pki/authorities/local/root.crt",
                                    }
                                ],
                            }
                        port = 8443 if item["port"] == 8443 else 443
                        label = str(item["port"])
                        client_config["inbounds"].append(
                            {
                                "tag": "input" + label,
                                "listen": "0.0.0.0",
                                "port": 18080 + index,
                                "protocol": "socks",
                                "settings": {"auth": "noauth"},
                            }
                        )
                        client_config["outbounds"].append(
                            {
                                "tag": "output" + label,
                                "protocol": "vless",
                                "settings": {
                                    "vnext": [
                                        {
                                            "address": "127.0.0.1",
                                            "port": port,
                                            "users": [
                                                {"id": client, "encryption": "none"}
                                            ],
                                        }
                                    ]
                                },
                                "streamSettings": stream,
                            }
                        )
                        client_config["routing"]["rules"].append(
                            {
                                "type": "field",
                                "inboundTag": ["input" + label],
                                "outboundTag": "output" + label,
                            }
                        )
                    panel_id = docker(
                        *base, "ps", "-q", "panel", capture=True
                    ).stdout.strip()
                    bin_path = docker(
                        "exec",
                        panel_id,
                        "sh",
                        "-c",
                        'printf "%s" /app/bin/xray-linux-*',
                        capture=True,
                    ).stdout
                    with subprocess.Popen(
                        [
                            "docker",
                            "exec",
                            "-i",
                            "-e",
                            "SSL_CERT_FILE=/test-ca/caddy/pki/authorities/local/root.crt",
                            panel_id,
                            bin_path,
                            "run",
                            "-config",
                            "stdin:",
                        ],
                        stdin=subprocess.PIPE,
                    ) as process:
                        try:
                            assert process.stdin is not None
                            with process.stdin:
                                process.stdin.write(json.dumps(client_config).encode())
                            time.sleep(1)
                            for inbound, port in zip(
                                inbounds, socks_external, strict=True
                            ):
                                label = str(inbound["port"])
                                result = _probe(
                                    "https://node.example.org/", label, port
                                )
                                assert (
                                    result["available"] and result["status"] == 200
                                ), result
                                print(
                                    "PASS: authenticated VLESS route for inbound port "
                                    + label
                                )
                        finally:
                            process.terminate()
                    print(
                        "PASS: Caddy TLS, origin fallback, node panel API and four inbounds with no clients."
                    )
                finally:
                    subprocess.run(
                        ["docker", "rm", "-f", project + "-target"],
                        capture_output=True,
                        check=False,
                    )
                    if (directory / "compose.yml").is_file():
                        docker(*base, "down")
