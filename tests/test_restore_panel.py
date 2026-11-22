"""Regression checks for local database replacement."""

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from setup3ui.panel.restore import restore_panel, settings

CONFIG = {
    "https": "caddy",
    "panel_domain": "master.example.org",
    "subscription_domain": "gateway.example.org",
    "panel_port": 2053,
    "subscription_port": 2096,
}


def create_database(path: Path, username: str) -> None:
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE users (username TEXT, password TEXT);
            CREATE TABLE inbounds (id INTEGER, port INTEGER);
            CREATE TABLE settings (key TEXT, value TEXT);
        """)
        db.execute("INSERT INTO users VALUES (?, 'fixture-hash')", (username,))
        db.executemany(
            "INSERT INTO settings VALUES (?, ?)",
            [
                ("webBasePath", f"/{username}/"),
                ("subPath", f"/{username}-sub/"),
                ("panelGuid", f"{username}-guid"),
                ("twoFactorEnable", "true"),
                ("twoFactorToken", f"{username}-fixture"),
            ],
        )


class RestorePanelTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "db").mkdir()
        self.active = self.directory / "db" / "x-ui.db"
        self.source = self.directory / "source.db"
        (self.directory / "compose.yml").write_text("services: {}\n")
        (self.directory / "config.yml").write_text("https: caddy\n")
        create_database(self.active, "previous")
        create_database(self.source, "restored")
        self.addCleanup(patch.stopall)
        patch("os.geteuid", return_value=0).start()
        patch("setup3ui.panel.restore._check_database_is_offline").start()
        self.compose = patch(
            "setup3ui.panel.restore.compose", return_value=subprocess.CompletedProcess([], 0, stdout="running\n")
        ).start()
        self.check_https = patch("setup3ui.panel.restore.verify_https").start()

    def test_restore_copies_credentials_and_keeps_destination_paths(self) -> None:
        content = self.source.read_bytes()
        backup = restore_panel(self.directory, CONFIG, self.source)
        self.assertEqual(self.source.read_bytes(), content)
        self.assertEqual(settings(backup / "x-ui.db")["twoFactorToken"], "previous-fixture")
        result = settings(self.active)
        self.assertEqual(result["twoFactorToken"], "restored-fixture")
        self.assertEqual(result["panelGuid"], "restored-guid")
        self.assertEqual(result["webBasePath"], "/previous/")
        self.assertEqual(result["subURI"], "https://gateway.example.org/previous-sub/")
        with closing(sqlite3.connect(self.active)) as db:
            self.assertEqual(db.execute("SELECT username FROM users").fetchone(), ("restored",))

    def test_invalid_backup_leaves_panel_running(self) -> None:
        self.source.write_text("not SQLite")
        with self.assertRaisesRegex(ValueError, "Invalid 3x-ui backup"):
            restore_panel(self.directory, CONFIG, self.source)
        self.compose.assert_not_called()
        self.assertEqual(settings(self.active)["panelGuid"], "previous-guid")

    def test_backup_without_credentials_is_rejected(self) -> None:
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DELETE FROM users")
        with self.assertRaisesRegex(ValueError, "no 3x-ui login"):
            restore_panel(self.directory, CONFIG, self.source)
        self.compose.assert_not_called()

    def test_failed_health_check_restores_previous_database(self) -> None:
        self.check_https.side_effect = [ValueError("fixture failure"), None]
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            restore_panel(self.directory, CONFIG, self.source)
        self.assertEqual(settings(self.active)["panelGuid"], "previous-guid")
