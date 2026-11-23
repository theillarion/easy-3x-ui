"""Exercise the real nftguard image in the private Docker daemon's network."""

import os
import socket
import subprocess
from contextlib import ExitStack
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest import TestCase, skipUnless
from unittest.mock import patch

import yaml

from setup3ui.config import validate_config
from setup3ui.deploy import lifecycle, nftguard
from setup3ui.deploy.compose import render_compose
from setup3ui.deploy.managed import compose
from setup3ui.host.docker import docker

PYTHON_IMAGE = "python:3.13-slim-trixie"
NFTGUARD_TEST_IMAGE = (
    f"{nftguard.IMAGE_REPOSITORY}@sha256:a9588f0bb9f89485fced829e39a5ca64050b42c0452666d5771156f43a4037f3"
)


@skipUnless(
    os.environ.get("SUITE") == "trixie"
    and os.environ.get("DOCKER_HOST") == "unix:///sandbox/docker.sock"
    and Path("/var/run/docker.sock").resolve() == Path("/sandbox/docker.sock"),
    "Needs the isolated Debian Docker sandbox",
)
class GuardIntegrationTests(TestCase):
    def test_dry_run_enforcement_conflict_and_removal(self) -> None:
        with TemporaryDirectory(prefix="guard-test-") as temporary, ExitStack() as cleanup:
            directory = Path(temporary)
            image = NFTGUARD_TEST_IMAGE
            network = f"setup3ui-{directory.name}"
            feeds = directory / "feeds"
            feeds.mkdir()
            (feeds / "input-v4.txt").write_text("".join(f"192.0.2.{i}/32\n" for i in range(1, 101)))
            (feeds / "input-v6.txt").write_text("2001:db8::/32\n")
            handler = partial(SimpleHTTPRequestHandler, directory=str(feeds))
            server = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), handler))
            Thread(target=server.serve_forever, daemon=True).start()
            cleanup.callback(server.shutdown)
            port = server.server_port
            sources = [
                {
                    "name": f"fixture_v{version}",
                    "url": f"http://127.0.0.1:{port}/input-v{version}.txt",
                    "format": "txt",
                    "direction": "input",
                    "ip_version": version,
                    "required": True,
                    "min_entries": 1,
                }
                for version in (4, 6)
            ]
            (directory / "sources.yml").write_text(yaml.safe_dump({"sources": sources}))
            docker("pull", image)
            docker("network", "create", "--subnet", "192.0.2.0/24", network)
            cleanup.callback(docker, "network", "rm", network)

            (directory / nftguard.STATE_DIRECTORY).mkdir()
            (directory / "config.yml").write_text("domain: example.org\nemail: test@example.org\n")
            marker = directory / nftguard.TABLE_MARKER

            def write_definition(dry_run: bool) -> None:
                config = validate_config({
                    "domain": "example.org", "email": "test@example.org",
                    "nftguard": {"dry_run": dry_run, "allow_cidrs": "192.0.2.9"},
                })
                definition = yaml.safe_load(render_compose(config))
                guard = definition["services"]["nftguard"]
                guard["image"] = image
                guard["pull_policy"] = "never"
                guard["stop_grace_period"] = "1s"
                guard["environment"].update(BOOT_DELAY_SECONDS="0", SOURCES_FILE="/fixtures/sources.yml")
                guard["volumes"].append("./sources.yml:/fixtures/sources.yml:ro")
                definition["name"] = network
                definition["services"] = {"nftguard": guard}
                (directory / "compose.yml").write_text(yaml.safe_dump(definition))

            def host_nft(*args: str) -> str:
                return docker(
                    "run", "--rm", "--network", "host", "--cap-add", "NET_ADMIN", "--entrypoint", "nft", image,
                    *args, capture=True,
                ).stdout

            def connect(address: str, port: int) -> None:
                docker(
                    "run", "--rm", "--network", network, "--ip", address, PYTHON_IMAGE,
                    "python", "-c", f"import socket; socket.create_connection(('192.0.2.1', {port}), timeout=2)",
                    capture=True,
                )

            write_definition(True)
            host_nft("add", "table", "inet", "setup3ui_test_other")
            cleanup.callback(host_nft, "delete", "table", "inet", "setup3ui_test_other")
            cleanup.callback(compose, directory, "down")
            cleanup.callback(nftguard.remove_table, directory)
            nftguard.check_available(directory)
            compose(directory, "up", "-d")
            nftguard.wait_ready(directory, timeout=30)
            self.assertNotIn(nftguard.TABLE, host_nft("list", "tables"))
            with self.assertRaisesRegex(ValueError, "already running"):
                nftguard.check_available(directory)
            compose(directory, "down")

            write_definition(False)
            nftguard.check_available(directory)
            marker.touch()
            compose(directory, "up", "-d")
            nftguard.wait_ready(directory, timeout=30)
            self.assertIn(nftguard.TABLE, host_nft("list", "tables"))
            with socket.socket() as listener:
                listener.bind(("0.0.0.0", 0))
                listener.listen()
                port = listener.getsockname()[1]
                connect("192.0.2.9", port)
                with self.assertRaises(subprocess.CalledProcessError) as blocked:
                    connect("192.0.2.8", port)
                self.assertIn("TimeoutError", blocked.exception.stderr)

            compose(directory, "stop", "nftguard")
            with self.assertRaisesRegex(ValueError, "already has"):
                nftguard.check_available(directory)
            compose(directory, "up", "-d")
            nftguard.wait_ready(directory, timeout=30)

            with patch.object(lifecycle, "UNIT_DIR", directory):
                lifecycle.uninstall(directory, purge=False)
            self.assertFalse(marker.exists())
            self.assertTrue((directory / nftguard.STATE_DIRECTORY).is_dir())
            tables = host_nft("list", "tables")
            self.assertNotIn(nftguard.TABLE, tables)
            self.assertIn("table inet setup3ui_test_other", tables)
            nftguard.check_available(directory)
