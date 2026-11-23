"""Exercise the real nftguard image in the private Docker daemon's network."""

import os
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

import yaml

from setup3ui.config import validate_config
from setup3ui.deploy import lifecycle, nftguard
from setup3ui.deploy.compose import render_compose
from setup3ui.deploy.managed import compose
from setup3ui.host.docker import docker

PYTHON_IMAGE = "python:3.13-slim-trixie"


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
            image = f"setup3ui-{directory.name}:local"
            network = f"setup3ui-{directory.name}"
            (directory / "feeds").mkdir()
            (directory / "feeds" / "c24_v4").write_text("".join(f"192.0.2.{i}/32\n" for i in range(1, 101)))
            (directory / "feeds" / "anti").write_bytes((directory / "feeds" / "c24_v4").read_bytes())
            (directory / "feeds" / "c24_v6").write_text("2001:db8::/32\n")
            (directory / "fixture.py").write_text(
                "from pathlib import Path\n"
                "p = Path('/usr/local/bin/nftguard')\n"
                "text = p.read_text()\n"
                "exec(compile(text, str(p), 'exec'), vars := {'__name__': 'fixture'})\n"
                "for name, original in vars['URLS'].items():\n"
                "    text = text.replace(original, 'file:///feeds/' + name)\n"
                "p.write_text(text)\n"
            )
            (directory / "Dockerfile").write_text(
                f"FROM {nftguard.IMAGE_REPOSITORY}:latest\n"
                "COPY feeds /feeds\n"
                "COPY fixture.py /tmp/fixture.py\n"
                "RUN python3 /tmp/fixture.py && rm /tmp/fixture.py\n"
            )
            docker("build", "-t", image, str(directory))
            cleanup.callback(docker, "image", "rm", image)
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
                guard["environment"]["BOOT_DELAY_SECONDS"] = "0"
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
