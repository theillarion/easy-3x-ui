"""Verify the Docker setup decisions without running APT or systemd."""

import os
import pwd
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from subprocess import CalledProcessError, CompletedProcess
from unittest import TestCase
from unittest.mock import patch

from setup3ui.host import docker as host


class DockerSetupTests(TestCase):
    def setUp(self) -> None:
        self.user = pwd.struct_passwd(
            ("fixture", "x", 1000, 1000, "", "/home/fixture", "/bin/sh")
        )
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.uid = stack.enter_context(patch.object(host.os, "geteuid", return_value=0))
        self.systemd = stack.enter_context(
            patch("setup3ui.host.docker.Path.is_dir", return_value=True)
        )
        self.which = stack.enter_context(
            patch.object(host.shutil, "which", return_value="/usr/bin/docker")
        )
        self.suite = stack.enter_context(
            patch.object(host, "debian_suite", return_value="trixie")
        )
        self.account = stack.enter_context(
            patch.object(host, "docker_user", return_value=self.user)
        )
        self.command = stack.enter_context(patch.object(host, "run"))
        self.sync = stack.enter_context(patch.object(host, "prepare_time_sync"))
        self.install = stack.enter_context(patch.object(host, "install_docker"))
        self.docker = stack.enter_context(
            patch.object(host, "docker", return_value=CompletedProcess([], 0, "[]"))
        )
        self.grant = stack.enter_context(
            patch.object(host, "grant_docker_access", return_value=False)
        )

    def test_existing_daemon_is_enabled_without_installing_docker_again(self) -> None:
        self.command.return_value = CompletedProcess([], 0, host.DOCKER_SOCKET)
        output = StringIO()
        with redirect_stdout(output):
            host.prepare_docker("fixture")
        self.install.assert_not_called()
        self.sync.assert_called_once_with()
        self.grant.assert_called_once_with(self.user)
        self.command.assert_any_call("systemctl", "enable", "--now", "docker.service")
        self.docker.assert_any_call("compose", "version")
        self.assertIn("System Docker is ready for fixture.", output.getvalue())
        self.assertNotIn("another endpoint", output.getvalue())
        self.assertNotIn("Sign out", output.getvalue())

    def test_clean_install_and_another_active_context(self) -> None:
        self.which.return_value = None
        self.grant.return_value = True
        self.command.return_value = CompletedProcess(
            [], 0, "unix:///run/user/1000/docker.sock"
        )
        output = StringIO()
        with redirect_stdout(output):
            host.prepare_docker("fixture")
        self.install.assert_called_once_with("trixie")
        self.assertIn("active Docker context uses another endpoint", output.getvalue())
        self.assertIn("Sign out and sign in again", output.getvalue())

    def test_context_query_failure_keeps_docker_ready(self) -> None:
        def run(*args: str, **kwargs: object) -> CompletedProcess[str]:
            if "context" in args:
                raise CalledProcessError(1, args)
            return CompletedProcess(args, 0, "")

        self.command.side_effect = run
        output = StringIO()
        with redirect_stdout(output):
            host.prepare_docker("fixture")
        self.assertIn("Could not check", output.getvalue())
        self.assertIn("Docker is ready", output.getvalue())

    def test_wrong_daemon_is_rejected_before_changing_the_group(self) -> None:
        self.docker.return_value = CompletedProcess([], 0, '["name=rootless"]')
        with (
            redirect_stdout(StringIO()),
            self.assertRaisesRegex(ValueError, "points to a rootless daemon"),
        ):
            host.prepare_docker("fixture")
        self.grant.assert_not_called()

    def test_partial_install_and_missing_systemd_stop_before_apt(self) -> None:
        self.systemd.return_value = False
        with self.assertRaisesRegex(ValueError, "running systemd"):
            host.prepare_docker("fixture")
        self.systemd.return_value = True
        self.which.side_effect = ["/usr/bin/docker", None]
        with self.assertRaisesRegex(ValueError, "incomplete Docker installation"):
            host.prepare_docker("fixture")
        self.uid.return_value = 1000
        with self.assertRaisesRegex(ValueError, "with sudo"):
            host.prepare_docker("fixture")
        self.command.assert_not_called()


class DockerUserTests(TestCase):
    def test_system_socket_clears_docker_environment_overrides(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "DOCKER_HOST": "unix:///tmp/fixture.sock",
                    "KEEP_TEST_VARIABLE": "yes",
                },
            ),
            patch.object(host, "run") as run,
        ):
            host.docker("ps", capture=True, input_text="fixture")
        self.assertEqual(
            run.call_args.args, ("docker", "--host", host.DOCKER_SOCKET, "ps")
        )
        self.assertFalse(
            any(name.startswith("DOCKER_") for name in run.call_args.kwargs["env"])
        )
        self.assertEqual(run.call_args.kwargs["env"]["KEEP_TEST_VARIABLE"], "yes")
        self.assertTrue(run.call_args.kwargs["capture"])

    def test_user_defaults_and_errors(self) -> None:
        with (
            patch.dict(os.environ, {"SUDO_USER": "fixture"}),
            patch.object(host.pwd, "getpwnam") as lookup,
        ):
            lookup.return_value.pw_uid = 1000
            self.assertEqual(host.docker_user(None), lookup.return_value)
            lookup.assert_called_once_with("fixture")
            lookup.return_value.pw_uid = 0
            with self.assertRaisesRegex(ValueError, "other than root"):
                host.docker_user("root")
            lookup.side_effect = KeyError
            with self.assertRaisesRegex(ValueError, "does not exist"):
                host.docker_user("missing")
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "Set docker_user"),
        ):
            host.docker_user(None)
