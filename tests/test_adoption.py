"""Check legacy deployment adoption and rollback with a real SQLite snapshot."""

import sqlite3
from contextlib import ExitStack, closing, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setup3ui.deploy import adoption


class AdoptionTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "legacy"
        self.database = self.path / "db/x-ui.db"
        self.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE users (username TEXT)")
            db.execute("INSERT INTO users VALUES ('old')")
        self.compose_file = self.path / "docker-compose.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")
        self.entry = {
            "name": "legacy-panel",
            "rootless": False,
            "socket": "/run/docker.sock",
            "databases": [str(self.database.parent)],
            "compose_dir": str(self.path),
            "compose_files": str(self.compose_file),
            "project": "legacy",
            "service": "panel",
        }
        self.config = {"install_dir": str(self.path)}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(adoption.os, "geteuid", return_value=0))
        stack.enter_context(patch.object(adoption, "check_root_owned_directory"))
        stack.enter_context(redirect_stdout(StringIO()))
        self.discover = stack.enter_context(
            patch.object(adoption, "discover_panels", return_value=[self.entry])
        )
        self.docker = stack.enter_context(
            patch.object(adoption, "docker", side_effect=self.docker_response)
        )
        self.install = stack.enter_context(patch.object(adoption, "install"))

    def docker_response(self, *args: str, **kwargs: object) -> CompletedProcess[str]:
        stdout = ""
        if args[-2:] == ("config", "--services"):
            stdout = "panel\ncaddy\n"
        elif args[0] == "ps":
            stdout = "first second"
        elif args[0] == "inspect":
            stdout = '"ghcr.io/mhsanaei/3x-ui:latest"\n"caddy:2"\n'
        return CompletedProcess(args, 0, stdout)

    def test_archives_original_directory_and_installs_from_its_snapshot(self) -> None:
        def install(config: object, ports: object, *, source: Path) -> None:
            self.assertFalse(self.path.exists())
            with closing(sqlite3.connect(source)) as db:
                self.assertEqual(
                    db.execute("SELECT username FROM users").fetchall(), [("old",)]
                )
            self.path.mkdir()
            (self.path / "config.yml").touch()

        self.install.side_effect = install
        adoption.adopt(self.config, {"Panel": 2053})
        self.assertTrue((self.path / "config.yml").is_file())
        archives = list(self.root.glob("legacy-before-adopt-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "docker-compose.yml").is_file())
        self.assertTrue((archives[0] / "db/x-ui.db").is_file())

    def test_failed_install_restores_legacy_directory_and_restarts_services(
        self,
    ) -> None:
        self.install.side_effect = ValueError("replacement failed")
        with self.assertRaisesRegex(ValueError, "replacement failed"):
            adoption.adopt(self.config, {})
        self.assertTrue(self.database.is_file())
        self.assertTrue(self.compose_file.is_file())
        self.assertEqual(list(self.root.glob("legacy-before-adopt-*")), [])
        self.assertEqual(
            self.docker.call_args.args[-4:], ("up", "-d", "panel", "caddy")
        )

    def test_unknown_layout_is_rejected_before_stopping_containers(self) -> None:
        for overrides, error in (
            ({"rootless": True}, "one system Docker panel"),
            ({"databases": []}, "project and db directory under"),
            ({"project": None}, "Could not identify"),
            (
                {"compose_files": str(self.path / "missing.yml")},
                "one legacy Compose file",
            ),
        ):
            self.discover.return_value = [self.entry | overrides]
            with (
                self.subTest(overrides=overrides),
                self.assertRaisesRegex(ValueError, error),
            ):
                adoption.adopt(self.config, {})
        self.docker.assert_not_called()
        self.install.assert_not_called()
