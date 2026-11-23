"""Exercise draft persistence, retries, and interactive CLI dispatch."""

import json
import os
import sqlite3
import stat
from contextlib import ExitStack, closing, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setup3ui import install_draft
from setup3ui.cli import main
from setup3ui.deploy.install import fresh_install_directory
from setup3ui.install_draft import Draft, InvalidDraft


class DraftTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "install-draft.json"
        self.directory = self.root / "state"
        self.draft = Draft(self.path, {"action": "install"})

    def test_round_trip_and_protection(self) -> None:
        self.draft.answers["panel_domain"] = "panel.example.org"
        self.draft.results["sni.alternatives"] = ["first.example.org", "second.example.org"]
        self.draft.save()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with patch("setup3ui.install_draft._private"):
            self.assertEqual(Draft.load(self.path), self.draft)
            self.draft.review()
            self.assertEqual(Draft.load(self.path), self.draft)
        self.assertEqual(self.draft.answers, {"action": "install"})
        self.assertEqual(self.draft.previous["panel_domain"], "panel.example.org")
        self.assertEqual(self.draft.results, {})

    def test_corrupt_and_unsupported_drafts(self) -> None:
        with patch("setup3ui.install_draft._private", side_effect=FileNotFoundError):
            self.assertIsNone(Draft.load(self.path))
        examples = [
            ("{", "damaged"),
            ('{"version": 9}', "unsupported format"),
            ('{"version": 1}', "damaged"),
            ('{"version": 1, "answers": {"action":"install"}, "previous": {}, "results": {"scan": "a"}, "panel_installed": false}', "damaged"),
            ('{"version": 1, "answers": {"action":"install"}, "previous": {}, "results": {}, "panel_installed": true}', "damaged"),
        ]
        for content, error in examples:
            with self.subTest(content=content):
                self.path.write_text(content)
                with patch("setup3ui.install_draft._private"), self.assertRaisesRegex(InvalidDraft, error):
                    Draft.load(self.path)

    def test_lock_is_exclusive_and_refuses_an_insecure_state_directory(self) -> None:
        with (
            patch.object(install_draft, "STATE_DIRECTORY", self.directory),
            patch("setup3ui.install_draft.check_root_owned_directory"),
        ):
            with patch("setup3ui.install_draft.os.geteuid", return_value=1000):
                with self.assertRaisesRegex(ValueError, "sudo"), install_draft.draft_lock():
                    self.fail("non-root wizard acquired the lock")
            with patch("setup3ui.install_draft.os.geteuid", return_value=0):
                self.directory.mkdir(mode=0o755)
                with self.assertRaisesRegex(ValueError, "0700"), install_draft.draft_lock():
                    self.fail("insecure directory was accepted")
                self.directory.rmdir()
                with patch("setup3ui.install_draft._private"):
                    with install_draft.draft_lock() as path:
                        self.assertEqual(path.parent, self.directory)
                        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                        with self.assertRaisesRegex(ValueError, "Another interactive"), install_draft.draft_lock():
                            self.fail("second wizard acquired the lock")

    def test_install_directory_checks(self) -> None:
        with patch("setup3ui.deploy.install.check_root_owned_directory") as check:
            destination = self.root / "panel"
            self.assertEqual(fresh_install_directory(destination), destination)
            check.assert_called_once_with(self.root)
            for path, error in (
                (Path("relative"), "absolute"),
                (self.root, "already exists"),
                (self.root / "with spaces", "only letters"),
            ):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, error):
                    fresh_install_directory(path)


class WizardTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "install-draft.json"
        self.destination = self.root / "custom"
        self.output = StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for cm in (
            patch("setup3ui.cli.sys.stdin.isatty", return_value=True),
            patch("setup3ui.install_draft.draft_lock", return_value=nullcontext(self.path)),
            patch("setup3ui.install_draft._private", side_effect=lambda path, *_: path.lstat()),
            patch("setup3ui.deploy.install.check_root_owned_directory"),
            patch("setup3ui.deploy.managed.operation_lock", nullcontext),
            patch("setup3ui.panel.discovery.discover_panels", return_value=[]),
            patch.dict(os.environ, {"SUDO_USER": "fixture"}, clear=True),
            redirect_stdout(self.output),
            redirect_stderr(self.output),
        ):
            stack.enter_context(cm)
        self.install = stack.enter_context(patch("setup3ui.deploy.install.install"))
        self.defaults = [
            "", "example.org", "", "", "test@example.org", "", "", "n", "n", "n",
        ]

    def run_wizard(self, answers: list, args: list[str] | None = None) -> list[str]:
        with patch("builtins.input", side_effect=answers) as prompt:
            main(args or [])
        return [call.args[0] for call in prompt.call_args_list]

    def test_resume_cdn_at_the_unanswered_field(self) -> None:
        first = ["", str(self.destination), "caddy", *self.defaults[1:-1], "yes", "yandex", "cdn.example.org"]
        with self.assertRaises(KeyboardInterrupt):
            self.run_wizard([*first, KeyboardInterrupt])
        draft = Draft.load(self.path)
        self.assertEqual(draft.answers["cdn.0.provider"], "yandex")
        self.assertNotIn("cdn.0.origin", draft.answers)
        prompts = self.run_wizard(["", "", "", "generate", "n", "n", "yes"])
        self.assertTrue(prompts[1].startswith("Origin domain"))
        config = self.install.call_args.args[0]
        self.assertEqual(config["install_dir"], str(self.destination))
        self.assertEqual(config["cdn"][0]["edge"], "cdn.example.org")
        self.assertNotIn("admin_password", config)
        self.assertFalse(self.path.exists())

    def test_resume_nftguard_allowlist(self) -> None:
        first = ["", str(self.destination), *self.defaults[:-1], "", "", "y", "2001:db8::1", "192.0.2.5"]
        with self.assertRaises(KeyboardInterrupt):
            self.run_wizard([*first, KeyboardInterrupt])
        draft = Draft.load(self.path)
        self.assertEqual(draft.answers["nftguard.enabled"], "y")
        self.assertEqual(draft.answers["nftguard.allow_cidrs"], "192.0.2.5/32")
        self.assertIn("must contain IPv4", self.output.getvalue())
        prompts = self.run_wizard(["", "192.0.2.0/24", "2001:db8::1", "yes"])
        self.assertTrue(prompts[1].startswith("IPv6 addresses"))
        self.assertNotIn("Enable nftguard", " ".join(prompts))
        guard = self.install.call_args.args[0]["nftguard"]
        self.assertEqual(guard["allow_cidrs6"], "2001:db8::1/128")
        self.assertTrue(guard["enable_vk_output"])
        self.assertFalse(guard["dry_run"])
        self.assertIn("Enforcing", self.output.getvalue())

    def test_backup_path_is_absolute_and_reused(self) -> None:
        backup = self.root / "backup.db"
        with closing(sqlite3.connect(backup)) as connection, connection:
            connection.execute("CREATE TABLE settings (key TEXT, value TEXT)")
            connection.execute("INSERT INTO settings VALUES ('webDomain', 'master.example.org')")
        with self.assertRaises(EOFError):
            self.run_wizard(["restore", str(self.destination), "missing.db", os.path.relpath(backup), EOFError])
        self.assertEqual(Draft.load(self.path).answers["backup_source"], str(backup))
        prompts = self.run_wizard(["", "", "", "", "", "test@example.org", "", "", "n", "n", "n", "yes"])
        self.assertTrue(prompts[1].startswith("HTTPS"))
        self.assertEqual(self.install.call_args.kwargs["source"], backup)

    def test_retry_failing_deploy_then_publish_only(self) -> None:
        self.install.side_effect = [ValueError("fixture setup failure"), None]
        args = ["install", "--install-dir", str(self.destination)]
        with (
            patch.dict(os.environ, {"SETUP_3X_UI_DEPLOY_CLI": "1"}),
            patch("setup3ui.cli.deploy_when_requested", side_effect=[OSError("fixture publish failure"), None]) as publish,
            patch("setup3ui.deploy.managed.managed_directory", return_value=self.destination),
        ):
            with self.assertRaises(SystemExit):
                self.run_wizard([*self.defaults, "yes"], args)
            self.assertFalse(Draft.load(self.path).panel_installed)
            with self.assertRaises(SystemExit):
                self.run_wizard(["", "yes"], args)
            self.assertTrue(Draft.load(self.path).panel_installed)
            self.run_wizard(["continue"])
            self.assertEqual(self.install.call_count, 2)
            publish.assert_called_with(required=True)
            self.assertFalse(self.path.exists())

    def test_recovery_refuses_existing_files(self) -> None:
        Draft(self.path, {"action": "install", "install_dir": str(self.destination)}).save()
        self.destination.mkdir()
        existing = self.destination / "db.sqlite"
        existing.touch()
        with self.assertRaises(SystemExit):
            self.run_wizard([""])
        self.assertIn(str(self.destination), self.output.getvalue())
        self.assertTrue(existing.is_file())
        self.install.assert_not_called()

    def test_review_keeps_previous_answers_as_defaults(self) -> None:
        answers = [*self.defaults[:6], "2053", "n", "n", "n"]
        with self.assertRaises(EOFError):
            self.run_wizard(["", str(self.destination), *answers, "y", EOFError])
        draft = Draft.load(self.path)
        self.assertEqual(draft.previous["subscription_port"], "2053")
        self.assertNotIn("subscription_port", draft.answers)
        prompts = self.run_wizard(["", "", "", "", "", "", "", "2096", "", "", "", "yes"])
        self.assertIn("Main domain [example.org]: ", prompts)
        self.assertEqual(self.install.call_args.args[0]["subscription_port"], 2096)

    def test_yaml_skips_existing_interactive_draft(self) -> None:
        draft = Draft(self.path, {"action": "install", "install_dir": str(self.destination)})
        draft.save()
        config = self.root / "config.yml"
        config.write_text("domain: example.org\nemail: test@example.org\n")
        with patch("setup3ui.install_draft.draft_lock") as lock:
            self.assertEqual(self.run_wizard([], ["validate", "--config", str(config)]), [])
        lock.assert_not_called()
        self.assertEqual(Draft.load(self.path), draft)

    def test_corrupt_draft_requires_permission_to_restart(self) -> None:
        self.path.write_text("incomplete")
        self.run_wizard(["n"])
        self.assertEqual(self.path.read_text(), "incomplete")
        with self.assertRaises(EOFError):
            self.run_wizard(["y", "", EOFError])
        self.assertEqual(json.loads(self.path.read_text())["answers"], {"action": "install"})

    def test_manage_discovered_custom_installation(self) -> None:
        panel = {"rootless": False, "project": "3x-ui", "service": "panel", "compose_dir": str(self.destination)}
        with (
            patch("setup3ui.panel.discovery.discover_panels", return_value=[panel]),
            patch("setup3ui.deploy.managed.managed_directory", return_value=self.destination),
            patch("setup3ui.cli.main") as followup,
            patch("builtins.input", return_value="backup"),
        ):
            main([])
        followup.assert_called_once_with(["backup", "--install-dir", str(self.destination)])
