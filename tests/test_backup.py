"""Check SQLite backups and management status with real fixture data."""

import sqlite3
import stat
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setup3ui.deploy import managed


class BackupTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "db" / "x-ui.db"
        self.database.parent.mkdir()
        self.config = self.directory / "config.yml"
        self.config.write_text("https: caddy\n", encoding="utf-8")
        (self.directory / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.caddyfile = self.directory / "caddy-config" / "Caddyfile"
        self.caddyfile.parent.mkdir()

    def test_snapshot_includes_wal_data_and_config_at_backup_time(self) -> None:
        self.caddyfile.write_text("master.example.org {}\n", encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as database:
            self.assertEqual(
                database.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",)
            )
            database.execute("CREATE TABLE users (username TEXT)")
            database.execute("INSERT INTO users VALUES ('fixture')")
            database.commit()
            self.assertTrue(self.database.with_name("x-ui.db-wal").is_file())
            snapshot = managed.backup_installation(self.directory)
            database.execute("UPDATE users SET username = 'changed'")
            database.commit()
        with closing(sqlite3.connect(snapshot / "x-ui.db")) as copied:
            self.assertEqual(
                copied.execute("SELECT username FROM users").fetchall(), [("fixture",)]
            )
        self.assertEqual(
            (snapshot / "config.yml").read_bytes(), self.config.read_bytes()
        )
        self.assertEqual(
            (snapshot / "Caddyfile").read_bytes(), self.caddyfile.read_bytes()
        )
        self.assertEqual(
            (snapshot / "compose.yml").read_bytes(),
            (self.directory / "compose.yml").read_bytes(),
        )
        self.assertEqual(
            {stat.S_IMODE(file.stat().st_mode) for file in snapshot.iterdir()}, {0o600}
        )
        self.assertFalse(list(snapshot.parent.glob(".backup-*")))

    def test_invalid_db_cleans_working_directory_on_error(self) -> None:
        self.database.write_bytes(b"invalid SQLite data")
        with self.assertRaisesRegex(ValueError, "Could not back up the database"):
            managed.backup_installation(self.directory)
        self.assertFalse(list((self.directory / "backups").iterdir()))

    def test_status_reports_actual_ports_paths_and_inbound_state(self) -> None:
        with closing(sqlite3.connect(self.database)) as database, database:
            database.execute(
                "CREATE TABLE inbounds (tag, remark, protocol, listen, port, enable)"
            )
            database.executemany(
                "INSERT INTO inbounds VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("tcp-tag", "Reality", "vless", "", 443, True),
                    ("xhttp-tag", "Direct XHTTP", "vless", "127.0.0.1", 8443, False),
                ],
            )
            database.execute("CREATE TABLE settings (key, value)")
            database.executemany(
                "INSERT INTO settings VALUES (?, ?)",
                [
                    ("webDomain", "master.example.org"),
                    ("webPort", "2053"),
                    ("webBasePath", "/private/"),
                    ("subDomain", "gateway.example.org"),
                    ("subPort", "2096"),
                    ("subPath", "/subscription/"),
                ],
            )
        certificate = self.directory / "certs/live/master.example.org/fullchain.pem"
        certificate.parent.mkdir(parents=True)
        certificate.touch()
        output = StringIO()
        with (
            patch.object(managed, "docker"),
            patch.object(managed, "run") as openssl,
            redirect_stdout(output),
        ):
            managed.show_status(self.directory, "certbot")
        self.assertIn("tcp-tag | Reality | vless | *:443 | enabled", output.getvalue())
        self.assertIn(
            "xhttp-tag | Direct XHTTP | vless | 127.0.0.1:8443 | disabled",
            output.getvalue(),
        )
        self.assertIn("https://master.example.org:2053/private/", output.getvalue())
        self.assertIn(
            "https://gateway.example.org:2096/subscription/", output.getvalue()
        )
        openssl.assert_called_once_with(
            "openssl", "x509", "-in", str(certificate), "-noout", "-subject", "-enddate"
        )
