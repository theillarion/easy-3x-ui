"""Check command dispatch while isolating host and deployment side effects."""

import json
from contextlib import (
    ExitStack,
    nullcontext,
    redirect_stderr,
    redirect_stdout,
)
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setup3ui.cli import main


class CliDispatchTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = self.directory / "config.yml"
        self.config.write_text(
            "domain: example.org\nemail: fixture@example.org\n", encoding="utf-8"
        )
        self.stdout = StringIO()
        self.stderr = StringIO()
        stack = ExitStack()
        self.addCleanup(stack.close)
        for context in (
            redirect_stdout(self.stdout),
            redirect_stderr(self.stderr),
            patch("setup3ui.cli.sys.stdin.isatty", return_value=False),
            patch(
                "setup3ui.deploy.managed.managed_directory", return_value=self.directory
            ),
            patch("setup3ui.deploy.managed.operation_lock", nullcontext),
        ):
            stack.enter_context(context)

    def run_command(self, *args: str) -> None:
        main([*args, "--install-dir", str(self.directory)])

    def test_validate_and_render_caddy(self) -> None:
        with self.config.open("a", encoding="utf-8") as file:
            file.write("https: caddy\nreality: true\n")
        self.run_command("validate", "--config", str(self.config))
        self.assertEqual(
            json.loads(self.stdout.getvalue())["ports"]["TCP REALITY"], 8444
        )
        self.stdout.seek(0)
        self.stdout.truncate(0)
        self.run_command("render-caddy", "--config", str(self.config))
        self.assertIn("proxy tcp/127.0.0.1:8444", self.stdout.getvalue())
        self.assertIn("reverse_proxy 127.0.0.1:2053", self.stdout.getvalue())

    def test_no_confirmation_leaves_data_and_token_alone(self) -> None:
        selectors = (
            (
                ["reset"],
                "setup3ui.deploy.lifecycle.reset",
                "Database reset was not confirmed.",
            ),
            (
                ["uninstall", "--purge-data"],
                "setup3ui.deploy.lifecycle.uninstall",
                "Data removal was not confirmed.",
            ),
            (
                ["update", "--rotate-token"],
                "setup3ui.deploy.lifecycle.update",
                "Token replacement was not confirmed.",
            ),
            (
                ["update", "--rotate-token", "--yes", "--token-name", "invalid name"],
                "setup3ui.deploy.lifecycle.update",
                "Invalid token name.",
            ),
        )
        for args, target, message in selectors:
            with (
                self.subTest(args=args),
                patch(target) as operation,
                self.assertRaises(SystemExit) as error,
            ):
                self.run_command(*args)
            self.assertEqual(error.exception.code, 1)
            self.assertIn(message, self.stderr.getvalue())
            operation.assert_not_called()

    def test_confirmed_reset_uses_auth_yaml(self) -> None:
        job = self.directory / "reset.yml"
        job.write_text(
            f"action: reset\ninstall_dir: {self.directory}\nconfirm: true\n"
            "admin_username: tester\nadmin_password: fixture-pass\n",
            encoding="utf-8",
        )
        with patch("setup3ui.deploy.lifecycle.reset") as reset:
            main(["--config", str(job)])
        directory, config, auth = reset.call_args.args
        self.assertEqual(directory, self.directory)
        self.assertEqual(config["https"], "certbot")
        self.assertEqual(auth["admin_username"], "tester")
        self.assertEqual(reset.call_args.kwargs, {"interactive": False})

    def test_update_rotates_token_only_when_requested(self) -> None:
        with (
            patch("setup3ui.deploy.lifecycle.update") as update,
            patch("setup3ui.deploy.lifecycle.rotate_token") as rotate,
        ):
            self.run_command("update", "--component", "panel")
            rotate.assert_not_called()
            self.run_command(
                "update", "--rotate-token", "--yes", "--token-name", "fixture-token"
            )
        self.assertEqual(update.call_args_list[0].args[2], "panel")
        self.assertEqual(rotate.call_args.args[2], "fixture-token")
        self.assertIn("invalidates the previous token", self.stdout.getvalue())

    def test_diagnostics_does_not_require_readable_saved_config(self) -> None:
        with (
            patch(
                "setup3ui.cli.read_config", side_effect=PermissionError("test error")
            ),
            patch("setup3ui.diagnostics.connections.test_connections") as test,
        ):
            self.run_command(
                "test",
                "--resource",
                "https://example.org/",
                "--json",
                "--download-xray",
            )
            test.assert_called_once_with(
                None, None, ["https://example.org/"], "xray", True, download=True
            )
            with self.assertRaises(SystemExit) as error:
                self.run_command("test", "--config", str(self.config))
        self.assertEqual(error.exception.code, 1)
        self.assertIn("test error", self.stderr.getvalue())
