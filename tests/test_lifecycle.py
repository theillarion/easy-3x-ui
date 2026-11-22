"""Test lifecycle effects against disposable data and controlled Docker responses."""

import json
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch

from setup3ui.config import validate_config
from setup3ui.deploy import lifecycle
from setup3ui.deploy.compose import render_compose


class LifecycleTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = validate_config(
            {"domain": "example.org", "email": "fixture@example.org"}
        )
        self.app = self.root / "app"
        self.app.mkdir()
        self.db = self.app / "db" / "x-ui.db"
        self.db.parent.mkdir()
        self.db.write_bytes(b"existing database")
        self.compose_file = self.app / "compose.yml"
        self.compose_file.write_text(render_compose(self.config), encoding="utf-8")
        (self.app / "logs").mkdir()
        self.backup = self.root / "backup"
        self.backup.mkdir()
        (self.backup / "x-ui.db").write_bytes(self.db.read_bytes())
        self.image = "ghcr.io/mhsanaei/3x-ui:latest"
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.output = StringIO()
        stack.enter_context(redirect_stdout(self.output))
        self.compose = stack.enter_context(patch.object(lifecycle, "compose"))
        self.docker = stack.enter_context(
            patch.object(
                lifecycle, "docker", return_value=CompletedProcess([], 0, "old")
            )
        )
        self.snapshot = stack.enter_context(
            patch.object(lifecycle, "backup_installation", return_value=self.backup)
        )
        self.health = stack.enter_context(patch.object(lifecycle, "_panel_health"))

    def install_images(self) -> str:
        return json.dumps({"services": {"panel": {"image": self.image}}})

    def test_unchanged_image_keeps_the_panel_running(self) -> None:
        self.compose.side_effect = [
            CompletedProcess([], 0, self.install_images()),
            CompletedProcess([], 0, "container-id"),
        ]
        lifecycle.update(self.app, self.config, "panel")
        self.assertIn("panel is up to date.", self.output.getvalue())
        self.assertEqual(self.compose.call_count, 2)
        self.snapshot.assert_not_called()
        self.health.assert_not_called()

    def test_bad_updated_panel_restores_previous_image_and_db(self) -> None:
        self.compose.side_effect = [
            CompletedProcess([], 0, self.install_images()),
            CompletedProcess([], 0, "container-id"),
            None,
            None,
            None,
        ]
        self.docker.side_effect = [
            CompletedProcess([], 0, "old"),
            CompletedProcess([], 0),
            CompletedProcess([], 0, "new"),
            CompletedProcess([], 0),
        ]
        self.db.write_bytes(b"broken after migration")
        self.db.with_name("x-ui.db-wal").touch()
        self.health.side_effect = [ValueError("new panel is unhealthy"), None]
        with self.assertRaisesRegex(ValueError, "new panel is unhealthy"):
            lifecycle.update(self.app, self.config, "panel")
        self.snapshot.assert_called_once_with(self.app)
        self.docker.assert_any_call("image", "tag", "old", self.image)
        self.assertEqual(self.db.read_bytes(), b"existing database")
        self.assertFalse(self.db.with_name("x-ui.db-wal").exists())
        self.assertEqual(
            self.compose.call_args_list[-2:],
            [
                call(self.app, "stop", "panel"),
                call(self.app, "up", "-d", "--no-deps", "--force-recreate", "panel"),
            ],
        )

    def test_bad_caddy_update_restores_previous_image(self) -> None:
        image = "setup-3x-ui-caddy:legacy"
        services = {"services": {"caddy": {"image": image}}}
        self.compose.side_effect = [
            CompletedProcess([], 0, json.dumps(services)),
            CompletedProcess([], 0, "container-id"),
            None,
            None,
            None,
        ]
        self.docker.side_effect = [
            CompletedProcess([], 0, "old"),
            CompletedProcess([], 0, "updated"),
            CompletedProcess([], 0),
        ]
        self.health.side_effect = [ValueError("Caddy did not start"), None]
        with (
            patch.object(lifecycle, "prepare_caddy_image") as prepare,
            self.assertRaisesRegex(ValueError, "Caddy did not start"),
        ):
            lifecycle.update(self.app, self.config | {"https": "caddy"}, "caddy")
        prepare.assert_called_once_with(image, refresh=True)
        self.snapshot.assert_not_called()
        self.docker.assert_any_call("image", "tag", "old", image)
        self.assertEqual(
            self.compose.call_args,
            call(self.app, "up", "-d", "--no-deps", "--force-recreate", "caddy"),
        )

    def test_reset_rolls_back_database_when_new_db_is_unhealthy(self) -> None:
        auth = {"admin_username": "fixture", "admin_password": "fixture-password"}

        def initialize(
            directory: Path, image: str, username: str, password: str
        ) -> str:
            self.db.write_bytes(b"fresh database")
            return "fake-token"

        self.health.side_effect = ValueError("panel is unhealthy")
        with (
            patch.object(
                lifecycle, "initialize_database", side_effect=initialize
            ) as bootstrap,
            patch.object(
                lifecycle,
                "configure_panel_tls",
                return_value=("panel", "subscription", None),
            ),
            self.assertRaisesRegex(ValueError, "panel is unhealthy"),
        ):
            lifecycle.reset(self.app, self.config, auth, interactive=False)
        self.snapshot.assert_called_once_with(self.app)
        bootstrap.assert_called_once_with(
            self.app, self.image, "fixture", "fixture-password"
        )
        self.assertEqual(self.db.read_bytes(), b"existing database")
        self.assertFalse(list(self.app.glob(".reset-*")))
        self.assertNotIn("fake-token", self.output.getvalue())

    def test_uninstall_keeps_data_by_default_and_purge_keeps_unrelated_files(
        self,
    ) -> None:
        uninstall_config = self.app / "config.yml"
        uninstall_config.touch()
        unrelated = self.app / "keep.txt"
        unrelated.touch()
        with patch.object(lifecycle, "UNIT_DIR", self.root):
            lifecycle.uninstall(self.app, purge=False)
            self.assertTrue(self.db.is_file())
            self.assertTrue(self.compose_file.is_file())
            lifecycle.uninstall(self.app, purge=True)
        self.assertFalse(self.db.exists())
        self.assertFalse(uninstall_config.exists())
        self.assertTrue(unrelated.is_file())
        self.assertIn("Other files remain", self.output.getvalue())
        self.assertEqual(
            self.compose.call_args_list,
            [call(self.app, "down"), call(self.app, "down")],
        )
