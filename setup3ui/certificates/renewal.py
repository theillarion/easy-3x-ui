"""Renew a Certbot certificate and reload a running panel."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from setup3ui import RUNTIME_DIR
from setup3ui.certificates.acme import certificate_lock, check_certificate
from setup3ui.host.commands import run
from setup3ui.host.docker import docker
from setup3ui.host.files import atomic_write, check_root_owned_directory

UNIT = "setup-3x-ui-renew"
UNIT_DIR = Path("/etc/systemd/system")
TIMER = """[Unit]
Description=Check 3x-ui certificates twice a day

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
"""


def _compose(install_dir: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return docker(
        "compose",
        "--project-directory",
        str(install_dir),
        "-f",
        str(install_dir / "compose.yml"),
        *args,
        capture=capture,
    )


def renew_certificate(install_dir: Path, domain: str) -> bool:
    """Renew when due. Retry a failed panel restart on the next run."""
    if os.geteuid() != 0:
        raise ValueError("Run renew-cert with sudo.")
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain):
        raise ValueError("Expected a certificate domain name.")
    install_dir = install_dir.resolve()
    cert_dir = install_dir / "certs" / "live" / domain
    chain, key = cert_dir / "fullchain.pem", cert_dir / "privkey.pem"
    applied = install_dir / ".certificate-applied"
    with certificate_lock(install_dir):
        check_certificate(chain, key)
        before = hashlib.sha256(chain.read_bytes()).hexdigest()
        if not applied.exists():
            atomic_write(applied, before.encode(), 0o600)
        _compose(
            install_dir,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "certbot",
            "renew",
            "--cert-name",
            domain,
            "--non-interactive",
            "--no-random-sleep-on-renew",
        )
        check_certificate(chain, key)
        after = hashlib.sha256(chain.read_bytes()).hexdigest()
        if after == applied.read_text(encoding="ascii"):
            return False
        panel_id = _compose(install_dir, "ps", "-q", "panel", capture=True).stdout.strip()
        if not panel_id:
            print("Panel is stopped. It will load the new certificate when it starts.")
            return False
        if not re.fullmatch(r"[0-9a-f]{12,64}", panel_id):
            raise ValueError("Expected one running panel container.")
        mounts = docker("inspect", "--format", "{{json .Mounts}}", panel_id, capture=True).stdout
        if not any(
            mount.get("Destination") == "/certs" and Path(mount["Source"]).resolve() == install_dir / "certs"
            for mount in json.loads(mounts)
        ):
            raise ValueError("The running panel does not use this installation's certificate directory.")
        docker("restart", panel_id)
        atomic_write(applied, after.encode(), 0o600)
    return True


def _install_renewal_runtime(directory: Path) -> None:
    check_root_owned_directory(directory.parent.resolve())
    directory.mkdir(mode=0o755, exist_ok=True)
    if directory.is_symlink():
        raise ValueError(f"{directory} must be a directory.")
    check_root_owned_directory(directory)
    source = Path(__file__).resolve().parents[2]
    for filename in (
        "setup3ui/__init__.py",
        "setup3ui/certificates/__init__.py",
        "setup3ui/certificates/acme.py",
        "setup3ui/certificates/renewal.py",
        "setup3ui/deploy/__init__.py",
        "setup3ui/deploy/compose.py",
        "setup3ui/host/__init__.py",
        "setup3ui/host/clock.py",
        "setup3ui/host/commands.py",
        "setup3ui/host/docker.py",
        "setup3ui/host/files.py",
        "setup3ui/host/preflight.py",
    ):
        target = directory / filename
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        check_root_owned_directory(target.parent)
        atomic_write(target, (source / filename).read_bytes(), 0o644)
    launcher = b"from setup3ui.certificates.renewal import main\n\nmain()\n"
    atomic_write(directory / "certificate_renewal.py", launcher, 0o644)


def install_renewal_timer(install_dir: Path, domain: str) -> None:
    """Install the renewal timer and its root-owned Python files."""
    if os.geteuid() != 0:
        raise ValueError("Run install-renewal with sudo.")
    install_dir = install_dir.resolve()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(install_dir)):
        raise ValueError("Use an install directory with only letters, digits, dots, underscores, and dashes.")
    check_root_owned_directory(install_dir)
    cert_dir = install_dir / "certs" / "live" / domain
    check_certificate(cert_dir / "fullchain.pem", cert_dir / "privkey.pem")
    _compose(install_dir, "config", "--quiet")
    service = f"""[Unit]
Description=Renew 3x-ui certificates
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/flock -n /run/setup-3x-ui.lock /usr/bin/python3 -E -s {RUNTIME_DIR}/certificate_renewal.py {install_dir} {domain}
"""
    service_file = UNIT_DIR / f"{UNIT}.service"
    timer_file = UNIT_DIR / f"{UNIT}.timer"
    previous = service_file.read_text(encoding="utf-8") if service_file.is_file() else None
    if previous and f" {install_dir} " not in previous:
        raise ValueError("A renewal timer already exists for a different setup.")
    _install_renewal_runtime(RUNTIME_DIR)
    timer = timer_file.read_bytes() if timer_file.is_file() else None
    try:
        atomic_write(service_file, service.encode(), 0o644)
        atomic_write(timer_file, TIMER.encode(), 0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", f"{UNIT}.timer")
    except (OSError, subprocess.CalledProcessError):
        if previous is not None and timer is not None:
            atomic_write(service_file, previous.encode(), 0o644)
            atomic_write(timer_file, timer, 0o644)
        else:
            subprocess.run(["systemctl", "disable", "--now", f"{UNIT}.timer"], check=False, capture_output=True)
            service_file.unlink(missing_ok=True)
            timer_file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
        raise


def main() -> None:
    """Run a single renewal check from the systemd service."""
    parser = argparse.ArgumentParser()
    parser.add_argument("install_dir", type=Path)
    parser.add_argument("domain")
    arguments = parser.parse_args()
    try:
        if renew_certificate(arguments.install_dir, arguments.domain):
            print("Panel restarted with the renewed certificate.")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")
