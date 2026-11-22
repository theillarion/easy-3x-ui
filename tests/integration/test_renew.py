"""Issue and renew certificates against a local Pebble ACME server."""

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import yaml

from setup3ui.certificates import acme as certificates
from setup3ui.certificates import renewal
from setup3ui.deploy.compose import render_compose


class SandboxTest(TestCase):
    def test_renew(self) -> None:
        with tempfile.TemporaryDirectory(prefix="3xui-renew-") as td:
            root = Path(td).resolve()
            project = root.name
            network = project + "-net"
            pebble = project + "-pebble"
            master, sub = "master.example.org", "gateway.example.org"
            for name in ("certs", "certbot-log", "certbot-work", "db", "logs"):
                (root / name).mkdir()
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
                    str(root / "tls.key"),
                    "-out",
                    str(root / "tls.crt"),
                    "-subj",
                    "/CN=pebble",
                    "-addext",
                    "subjectAltName=DNS:pebble",
                ],
                check=True,
                capture_output=True,
            )
            (root / "pebble.json").write_text(
                json.dumps(
                    {
                        "pebble": {
                            "listenAddress": "0.0.0.0:14000",
                            "managementListenAddress": "0.0.0.0:15000",
                            "certificate": "/fixture/tls.crt",
                            "privateKey": "/fixture/tls.key",
                            "httpPort": 80,
                            "tlsPort": 443,
                        }
                    }
                )
            )
            force = False
            failed = False
            restarts = []

            def docker(*args, capture=False):
                args = list(args)
                if args[0] == "compose":
                    args[1:1] = ["-p", project]
                    if "run" in args:
                        compose = yaml.safe_load(
                            Path(args[args.index("-f") + 1]).read_text()
                        )
                        certbot = compose["services"]["certbot"]
                        del certbot["network_mode"]
                        certbot["networks"] = {"test": {"aliases": [master, sub]}}
                        certbot["environment"] = {
                            "REQUESTS_CA_BUNDLE": "/fixture-cert.pem"
                        }
                        certbot["volumes"].append("./tls.crt:/fixture-cert.pem:ro")
                        compose["networks"] = {
                            "test": {"external": True, "name": network}
                        }
                        adapted = root / "compose-test.yml"
                        adapted.write_text(yaml.safe_dump(compose))
                        args[args.index("-f") + 1] = str(adapted)
                        args.insert(args.index("-T"), "--use-aliases")
                        if "certonly" in args:
                            args.extend(["--server", "https://pebble:14000/dir"])
                        if force and "renew" in args:
                            args.append("--force-renewal")
                if args[0] == "restart":
                    restarts.append(args[1])
                    if failed:
                        raise subprocess.CalledProcessError(1, args)
                return subprocess.run(
                    ["docker", *args], check=True, text=True, capture_output=capture
                )

            with (
                patch.object(os, "geteuid", return_value=0),
                patch.object(certificates, "docker", docker),
                patch.object(certificates, "tcp_listeners", return_value=[]),
                patch.object(renewal, "docker", docker),
            ):
                docker("network", "create", "--internal", network)
                try:
                    docker(
                        "run",
                        "-d",
                        "--name",
                        pebble,
                        "--network",
                        network,
                        "--network-alias",
                        "pebble",
                        "-e",
                        "PEBBLE_VA_NOSLEEP=1",
                        "-e",
                        "PEBBLE_WFE_NONCEREJECT=0",
                        "-v",
                        f"{root}:/fixture:ro",
                        "ghcr.io/letsencrypt/pebble:latest",
                        "-config",
                        "/fixture/pebble.json",
                    )
                    time.sleep(2)
                    chain, _key = certificates.issue_certificate(
                        root, master, sub, "test@example.org"
                    )
                    (root / "compose.yml").write_text(
                        render_compose({"https": "certbot"})
                    )
                    base = [
                        "compose",
                        "--project-directory",
                        str(root),
                        "-f",
                        str(root / "compose.yml"),
                    ]
                    assert renewal.renew_certificate(root, master) is False
                    before = hashlib.sha256(chain.read_bytes()).hexdigest()
                    assert (
                        root / ".certificate-applied"
                    ).read_text() == before and not restarts
                    # Run a dedicated container under the panel service with the same real certificate bind mount.
                    fixture = yaml.safe_load((root / "compose.yml").read_text())
                    fixture["services"]["panel"] = {
                        "image": "python:3.13-slim-trixie",
                        "network_mode": "none",
                        "command": ["sleep", "infinity"],
                        "volumes": ["./certs:/certs:ro"],
                    }
                    (root / "compose.yml").write_text(yaml.safe_dump(fixture))
                    docker(*base, "up", "-d", "panel")
                    force = failed = True
                    try:
                        renewal.renew_certificate(root, master)
                    except subprocess.CalledProcessError:
                        pass
                    else:
                        raise AssertionError("Expected a failed restart")
                    after = hashlib.sha256(chain.read_bytes()).hexdigest()
                    assert (
                        after != before
                        and (root / ".certificate-applied").read_text() == before
                    )
                    force = failed = False
                    assert renewal.renew_certificate(root, master) is True
                    assert (root / ".certificate-applied").read_text() == after
                    assert len(restarts) == 2 and restarts[0] == restarts[1]
                    assert (
                        renewal.renew_certificate(root, master) is False
                        and len(restarts) == 2
                    )
                    print(
                        "PASS: real Certbot renewal with Pebble, no-op renewal, and restart retry."
                    )
                finally:
                    if (root / "compose.yml").exists():
                        docker(
                            "compose",
                            "--project-directory",
                            str(root),
                            "-f",
                            str(root / "compose.yml"),
                            "down",
                        )
                    docker("rm", "-f", pebble)
                    docker("network", "rm", network)
