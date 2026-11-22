"""Check how the installer handles existing and missing NTP providers."""

import subprocess
import unittest
from unittest.mock import call, patch

from setup3ui.host.clock import prepare_time_sync


def status(can_ntp: str, ntp: str, synchronized: str) -> subprocess.CompletedProcess[str]:
    output = f"CanNTP={can_ntp}\nNTP={ntp}\nNTPSynchronized={synchronized}\n"
    return subprocess.CompletedProcess([], 0, stdout=output)


def success() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout="")


class TimeSyncTest(unittest.TestCase):
    def test_synchronized_clock_leaves_provider_alone(self) -> None:
        with patch("setup3ui.host.clock.run", return_value=status("yes", "yes", "yes")) as run:
            prepare_time_sync()
        run.assert_called_once_with(
            "timedatectl", "show", "--property=CanNTP", "--property=NTP", "--property=NTPSynchronized", capture=True
        )

    def test_missing_provider_installs_and_starts_timesyncd(self) -> None:
        with patch(
            "setup3ui.host.clock.run",
            side_effect=[status("no", "no", "no"), success(), success(), status("yes", "yes", "yes")],
        ) as run:
            prepare_time_sync()

        self.assertEqual(
            run.call_args_list[1:3],
            [
                call("apt-get", "install", "-y", "--no-remove", "--no-install-recommends", "systemd-timesyncd"),
                call("systemctl", "enable", "--now", "systemd-timesyncd.service"),
            ],
        )

    def test_stopped_provider_is_enabled_without_installing_another(self) -> None:
        with patch(
            "setup3ui.host.clock.run", side_effect=[status("yes", "no", "no"), success(), status("yes", "yes", "yes")]
        ) as run:
            prepare_time_sync()
        self.assertEqual(run.call_args_list[1], call("timedatectl", "set-ntp", "true"))

    def test_active_provider_times_out_without_changes(self) -> None:
        with (
            patch("setup3ui.host.clock.run", return_value=status("yes", "yes", "no")) as run,
            patch("setup3ui.host.clock.time.monotonic", side_effect=[0, 60]),
            patch("setup3ui.host.clock.time.sleep") as sleep,
            self.assertRaisesRegex(ValueError, "did not synchronize"),
        ):
            prepare_time_sync()
        self.assertEqual(run.call_count, 2)
        sleep.assert_not_called()
