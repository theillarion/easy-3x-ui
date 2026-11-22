"""Check managed configuration changes and rollback against temporary files."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch

import yaml

from setup3ui.config import validate_config
from setup3ui.deploy import reconfigure
from setup3ui.deploy.caddy import render_caddy


class ReconfigureTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.current = validate_config(
            {"https": "caddy", "domain": "example.org", "email": "fixture@example.org"}
        )
        self.caddyfile = self.root / "caddy-config" / "Caddyfile"
        self.caddyfile.parent.mkdir()
        self.original_caddy = render_caddy(self.current)
        self.caddyfile.write_text(self.original_caddy, encoding="utf-8")
        self.config = self.root / "config.yml"
        self.config.write_text(yaml.safe_dump(self.current), encoding="utf-8")
        self.db = self.root / "db" / "x-ui.db"
        self.db.parent.mkdir()
        self.db.write_bytes(b"original database")
        self.backup = self.root / "backup"
        self.backup.mkdir()
        (self.backup / "Caddyfile").write_text(self.original_caddy, encoding="utf-8")
        (self.backup / "x-ui.db").write_bytes(self.db.read_bytes())
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(StringIO()))
        self.listeners = stack.enter_context(
            patch.object(reconfigure, "tcp_listeners", return_value=[])
        )
        self.dns = stack.enter_context(patch.object(reconfigure, "check_dns"))
        self.snapshot = stack.enter_context(
            patch.object(reconfigure, "backup_installation", return_value=self.backup)
        )
        self.health = stack.enter_context(patch.object(reconfigure, "_panel_health"))
        self.compose = stack.enter_context(patch.object(reconfigure, "compose"))
        self.login = stack.enter_context(patch.object(reconfigure, "_login"))

    def test_web_change_reloads_caddy_and_updates_config(self) -> None:
        desired = self.current | {"panel_domain": "other.example.org"}
        with patch.object(reconfigure, "configure_panel_tls") as tls:
            reconfigure.apply_config(
                self.root, self.current, desired, {}, interactive=False
            )
        self.snapshot.assert_called_once_with(self.root)
        self.dns.assert_called_once_with(["other.example.org", "gateway.example.org"])
        self.health.assert_called_once_with(self.root, desired)
        tls.assert_called_once_with(
            self.root,
            "other.example.org",
            2053,
            "gateway.example.org",
            2096,
            tls_at_panel=False,
        )
        self.assertIn("other.example.org", self.caddyfile.read_text(encoding="utf-8"))
        self.assertEqual(
            yaml.safe_load(self.config.read_text(encoding="utf-8")), desired
        )
        self.assertFalse(self.caddyfile.with_name("candidate.Caddyfile").exists())
        self.login.assert_not_called()

    def test_unhealthy_web_change_restores_caddy_and_database(self) -> None:
        def write_new_database(*args: object, **kwargs: object) -> None:
            self.db.write_bytes(b"new database")
            self.db.with_name("x-ui.db-wal").write_bytes(b"stale journal")

        desired = self.current | {"panel_domain": "other.example.org"}
        self.health.side_effect = ValueError("HTTPS check failed")
        with (
            patch.object(
                reconfigure, "configure_panel_tls", side_effect=write_new_database
            ),
            self.assertRaisesRegex(ValueError, "HTTPS check failed"),
        ):
            reconfigure.apply_config(self.root, self.current, desired, {})
        self.assertEqual(self.db.read_bytes(), b"original database")
        self.assertFalse(self.db.with_name("x-ui.db-wal").exists())
        self.assertEqual(
            self.caddyfile.read_text(encoding="utf-8"), self.original_caddy
        )
        self.assertEqual(
            yaml.safe_load(self.config.read_text(encoding="utf-8")), self.current
        )
        self.assertEqual(
            self.compose.call_args_list[-2:],
            [
                call(self.root, "stop", "panel"),
                call(self.root, "up", "-d", "--no-deps", "panel"),
            ],
        )

    def test_port_conflicts_and_mode_changes_stop_before_backup(self) -> None:
        with self.assertRaisesRegex(ValueError, "Changing https"):
            reconfigure.apply_config(
                self.root, self.current, self.current | {"https": "certbot"}, {}
            )
        backend = {
            "provider": "yandex",
            "edge": "edge.example.org",
            "origin": "node.example.org",
            "path": "/test",
            "port": 8080,
        }
        self.listeners.return_value = [(8080, "127.0.0.1:8080", ["something (PID 8)"])]
        with self.assertRaisesRegex(ValueError, "TCP port 8080 is in use .*PID 8"):
            reconfigure.apply_config(
                self.root, self.current, self.current | {"cdn": [backend]}, {}
            )
        with self.assertRaisesRegex(ValueError, "Keep existing CDN ports"):
            reconfigure.apply_config(
                self.root, self.current | {"cdn": [backend]}, self.current, {}
            )
        self.snapshot.assert_not_called()
        self.compose.assert_not_called()

    def test_inbound_is_selected_by_tag_and_remark_is_printed(self) -> None:
        api = self.login.return_value
        api.call.return_value = [
            {"tag": "desired", "remark": "Other inbound", "id": 1, "protocol": "vless"},
            {"tag": "other", "remark": "desired", "id": 2, "protocol": "vless"},
        ]
        output = StringIO()
        with redirect_stdout(output):
            inbound = reconfigure._inbound(api, "desired")
        self.assertEqual(inbound["id"], 1)
        self.assertIn("Inbound desired: Other inbound", output.getvalue())
        for received, error in (
            ([], "got 0"),
            ([inbound, inbound], "got 2"),
            ({}, "did not return its inbounds"),
        ):
            api.call.return_value = received
            with (
                self.subTest(received=received),
                self.assertRaisesRegex(ValueError, error),
            ):
                reconfigure._inbound(api, "desired")
