"""Check and configure host clock synchronization."""

import time

from setup3ui.host.commands import run


def _time_status() -> dict[str, str]:
    properties = ("CanNTP", "NTP", "NTPSynchronized")
    output = run("timedatectl", "show", *(f"--property={name}" for name in properties), capture=True).stdout
    status = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if any(status.get(name) not in {"yes", "no"} for name in properties):
        raise ValueError("Could not read system clock synchronization status from timedatectl.")
    return status


def prepare_time_sync() -> None:
    """Set up host clock synchronization before starting the stack."""
    status = _time_status()
    if status.get("NTPSynchronized") == "yes":
        print("System clock is synchronized.")
        return

    if status.get("CanNTP") == "no":
        print("Installing systemd-timesyncd for host clock synchronization.", flush=True)
        run("apt-get", "install", "-y", "--no-remove", "--no-install-recommends", "systemd-timesyncd")
        run("systemctl", "enable", "--now", "systemd-timesyncd.service")
    elif status.get("NTP") == "no":
        run("timedatectl", "set-ntp", "true")

    print("Waiting for system clock synchronization.", flush=True)
    deadline = time.monotonic() + 60
    while _time_status().get("NTPSynchronized") != "yes":
        if time.monotonic() >= deadline:
            raise ValueError(
                "The system clock did not synchronize within 60 seconds. "
                "Check 'timedatectl status' and whether the VPS can reach its NTP server."
            )
        time.sleep(2)
    print("System clock is synchronized.")
