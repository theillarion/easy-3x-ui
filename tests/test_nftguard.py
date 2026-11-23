"""Cover configuration, Compose rendering and nftguard lifecycle failures."""

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch

import yaml

from setup3ui import ASSETS
from setup3ui.config import read_config, validate_config
from setup3ui.deploy import nftguard
from setup3ui.deploy.compose import render_compose


def result(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, stderr)


class GuardConfigTests(TestCase):
    base = {"domain": "example.org", "email": "test@example.org"}

    def test_defaults_and_validation(self) -> None:
        guard = validate_config(self.base)["nftguard"]
        self.assertTrue(guard["enabled"])
        self.assertFalse(guard["dry_run"])
        for invalid in (
            False, {"enabled": "true"}, {"dry_run": 0}, {"bogus": False},
            {"allow_cidrs": []}, {"allow_cidrs": "garbage"}, {"allow_cidrs": "2001:db8::1"},
            {"allow_cidrs6": "192.0.2.1"}, {"allow_cidrs6": "fe80::2%eth0"},
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "nftguard"):
                validate_config(self.base | {"nftguard": invalid})
        valid = validate_config(self.base | {
            "nftguard": {"allow_cidrs": " 192.0.2.1/24, 192.0.2.0/24 , 198.51.100.1 "},
        })
        self.assertEqual(valid["nftguard"]["allow_cidrs"], "192.0.2.0/24,198.51.100.1/32")

    def test_legacy_installed_config_keeps_guard_disabled(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yml"
            path.write_text(yaml.safe_dump(self.base))
            self.assertFalse(read_config(path, False, installed=True)["nftguard"]["enabled"])
            self.assertTrue(read_config(path, False)["nftguard"]["enabled"])

    def test_render_both_modes_and_keep_user_defined_services_and_volumes(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            base = yaml.safe_load((ASSETS / "compose.yml").read_text())
            base["services"]["extra"] = {"image": "example.org/extra", "volumes": ["my-data:/data"]}
            base["volumes"] = {"my-data": {}}
            (directory / "compose.yml").write_text(yaml.safe_dump(base))
            for mode in ("caddy", "certbot"):
                path = ASSETS / f"compose.{mode}.yml"
                (directory / path.name).write_bytes(path.read_bytes())
            with patch("setup3ui.deploy.compose.ASSETS", directory):
                for mode in ("caddy", "certbot"):
                    config = validate_config(self.base | {"https": mode, "nftguard": {"enabled": False}})
                    definition = yaml.safe_load(render_compose(config))
                    self.assertEqual(set(definition["services"]), {"panel", "extra", mode})
                    self.assertEqual(definition["volumes"], {"my-data": {}})
                    targets = [vol["target"] for vol in definition["services"]["panel"]["volumes"]]
                    self.assertEqual("/certs" in targets, mode == "certbot")
                self.assertEqual(definition["services"]["certbot"]["profiles"], ["certificates"])
                self.assertIn("nftguard", yaml.safe_load((directory / "compose.yml").read_text())["services"])

    def test_render_guard_environment(self) -> None:
        config = validate_config(self.base | {"nftguard": {
            "dry_run": True, "enable_vk_output": True, "allow_cidrs": "192.0.2.5", "allow_cidrs6": "2001:db8::/32",
        }})
        env = yaml.safe_load(render_compose(config))["services"]["nftguard"]["environment"]
        self.assertEqual([env[name] for name in ("DRY_RUN", "ENABLE_VK_OUTPUT", "ALLOW_CIDRS", "ALLOW_CIDRS6")],
                         ["true", "true", "192.0.2.5/32", "2001:db8::/32"])


class GuardLifecycleTests(TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.compose = self.enterContext(patch.object(nftguard, "compose"))
        self.docker = self.enterContext(patch.object(nftguard, "docker", return_value=result()))

    def test_running_instance_and_existing_table_fail_without_removal(self) -> None:
        for instances, images in (("abc\n", ""), ("", f"{nftguard.IMAGE_REPOSITORY}:other\n")):
            self.docker.side_effect = [result(instances), result(images)]
            with self.assertRaisesRegex(ValueError, "already running"):
                nftguard.check_available(self.directory)
        self.compose.assert_not_called()
        for table in ("table inet firewall\n", "table inet nftguard\n"):
            self.docker.side_effect = [result(), result()]
            self.compose.return_value = result(table)
            if table == "table inet nftguard\n":
                with self.assertRaisesRegex(ValueError, "already has"):
                    nftguard.check_available(self.directory)
            else:
                nftguard.check_available(self.directory)
        self.assertTrue(all(item.args[-2:] == ("list", "tables") for item in self.compose.call_args_list))

    def test_readiness_wait_and_failure_details(self) -> None:
        self.compose.return_value = result("container\n")
        self.docker.side_effect = [result("running starting"), result("running healthy")]
        with patch.object(nftguard.time, "sleep") as sleep:
            nftguard.wait_ready(self.directory)
        sleep.assert_called_once()
        for state, container in (("running starting", "container"), ("exited starting", "container"), ("", "")):
            self.compose.side_effect = [result(container), result("list download failed", "error detail")]
            self.docker.side_effect = [result(state)]
            with self.assertRaisesRegex(ValueError, "list download failed"):
                nftguard.wait_ready(self.directory, timeout=0 if state == "running starting" else 5)
        self.compose.side_effect = [subprocess.TimeoutExpired("docker", 1), result("fixture log")]
        with self.assertRaisesRegex(ValueError, "Timed out after 1 second"):
            nftguard.wait_ready(self.directory, timeout=1)
        self.compose.side_effect = [result("id"), result("fixture log")]
        self.docker.side_effect = [subprocess.CalledProcessError(1, "docker", stderr="inspect failed")]
        with self.assertRaisesRegex(ValueError, "inspect failed"):
            nftguard.wait_ready(self.directory)
        self.compose.side_effect = [result(), subprocess.CalledProcessError(1, "docker")]
        with self.assertRaisesRegex(ValueError, "Could not read nftguard logs"):
            nftguard.wait_ready(self.directory)

    def test_cleanup_stops_before_deleting_and_can_be_retried(self) -> None:
        nftguard.remove_table(self.directory)
        self.compose.assert_not_called()
        marker = self.directory / nftguard.TABLE_MARKER
        marker.touch()
        failure = subprocess.CalledProcessError(1, "nft")
        self.compose.side_effect = [result(), result("table inet nftguard\n"), failure]
        with self.assertRaises(subprocess.CalledProcessError):
            nftguard.remove_table(self.directory)
        self.assertTrue(marker.exists())
        self.assertEqual(self.compose.call_args_list[0], call(self.directory, "stop", "nftguard"))
        self.assertEqual(self.compose.call_args.args[-4:], ("delete", "table", "inet", "nftguard"))
        self.docker.side_effect = [result("another"), result()]
        self.compose.side_effect = [result(), result("table inet nftguard\n")]
        with self.assertRaisesRegex(ValueError, "table was kept"):
            nftguard.remove_table(self.directory)
        self.assertTrue(marker.exists())
        self.compose.side_effect = [result(), result()]
        nftguard.remove_table(self.directory)
        self.assertFalse(marker.exists())


class GuardInstallTests(TestCase):
    def setUp(self) -> None:
        from setup3ui.deploy import install

        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "install"
        self.config = validate_config(GuardConfigTests.base | {"install_dir": str(self.directory)})
        self.install = install
        for name in (
            "check_root_owned_directory", "prepare_docker", "check_dns", "check_host", "issue_certificate",
        ):
            self.enterContext(patch.object(install, name))
        self.enterContext(patch.object(install.os, "geteuid", return_value=0))
        self.enterContext(patch.object(install, "discover_panels", return_value=[]))
        self.enterContext(patch.object(install, "credentials", return_value=("user", "secret")))
        self.enterContext(patch.object(install, "initialize_database", return_value="fixture"))
        self.enterContext(patch.object(install, "configure_panel_tls", return_value=(
            "https://master.example.org/panel/", "https://gateway.example.org/sub/", None,
        )))
        self.docker = self.enterContext(patch.object(install, "docker", return_value=result()))
        self.check = self.enterContext(patch.object(install, "check_available"))
        self.wait = self.enterContext(patch.object(install, "wait_ready"))
        self.remove = self.enterContext(patch.object(install, "remove_table"))

    def test_guard_timeout_removes_table_and_created_files(self) -> None:
        self.wait.side_effect = ValueError("fixture readiness failure")

        def cleanup(directory: Path) -> None:
            self.assertTrue((directory / nftguard.TABLE_MARKER).is_file())
            self.assertEqual(self.docker.call_args.args[-2:], ("up", "-d"))

        self.remove.side_effect = cleanup
        with self.assertRaisesRegex(ValueError, "fixture readiness failure"):
            self.install.install(self.config, {}, interactive=False)
        self.check.assert_called_once_with(self.directory)
        self.remove.assert_called_once_with(self.directory)
        self.assertEqual(self.docker.call_args.args[-1], "down")
        self.assertFalse(self.directory.exists())

    def test_failed_cleanup_preserves_files(self) -> None:
        self.wait.side_effect = ValueError("fixture readiness failure")
        self.remove.side_effect = subprocess.CalledProcessError(1, "nft", stderr="permission denied")
        with self.assertRaisesRegex(ValueError, "permission denied.*Files kept"):
            self.install.install(self.config, {}, interactive=False)
        self.assertTrue((self.directory / "compose.yml").is_file())
        self.assertTrue((self.directory / "config.yml").is_file())
        self.assertTrue((self.directory / nftguard.TABLE_MARKER).is_file())

    def test_firewall_preflight_failure_reports_stderr_without_claiming_table(self) -> None:
        self.check.side_effect = subprocess.CalledProcessError(1, "nft", stderr="permission denied")
        with self.assertRaisesRegex(ValueError, "Could not check nftguard access.*permission denied"):
            self.install.install(self.config, {}, interactive=False)
        self.wait.assert_not_called()
        self.remove.assert_not_called()
        self.assertFalse(self.directory.exists())
