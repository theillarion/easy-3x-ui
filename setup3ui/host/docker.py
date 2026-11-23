"""Prepare system Docker on Debian."""

import grp
import json
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

from setup3ui.host.clock import prepare_time_sync
from setup3ui.host.commands import run

DOCKER_SOCKET = "unix:///var/run/docker.sock"
BASE_PACKAGES = (
    "ca-certificates",
    "curl",
    "dnsutils",
    "git",
    "iproute2",
    "iputils-ping",
    "nmap",
    "openssl",
    "python3-yaml",
)
DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin")


def docker_environment() -> dict[str, str]:
    """Remove overrides that could select a different Docker daemon."""
    return {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}


def docker(
    *args: str, capture: bool = False, input_text: str | None = None, timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Docker against the local system socket."""
    return run(
        "docker", "--host", DOCKER_SOCKET, *args, capture=capture, env=docker_environment(), input_text=input_text,
        timeout=timeout,
    )


def debian_suite() -> str:
    """Return the APT suite for a supported Debian release."""
    release: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            release[key] = value.strip("\"'")
    suites = {"12": "bookworm", "13": "trixie"}
    suite = suites.get(release.get("VERSION_ID", ""))
    if release.get("ID") != "debian" or suite is None:
        raise ValueError("System setup requires Debian 12 or 13.")
    return suite


def docker_user(configured_user: str | None) -> pwd.struct_passwd:
    """Find the regular user who should get Docker access."""
    name = configured_user or os.environ.get("SUDO_USER")
    if not name:
        raise ValueError("Set docker_user to the Linux account that should use Docker.")
    try:
        user = pwd.getpwnam(name)
    except KeyError:
        raise ValueError(f"Linux user {name!r} does not exist.") from None
    if user.pw_uid == 0:
        raise ValueError("Choose a Linux user other than root for Docker access.")
    return user


def install_docker(suite: str) -> None:
    """Install Docker and Compose from Docker's APT repository."""
    architecture = run("dpkg", "--print-architecture", capture=True).stdout.strip()
    keyring = Path("/etc/apt/keyrings/docker.asc")
    source = Path("/etc/apt/sources.list.d/docker.sources")
    keyring.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if not source.exists():
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "docker.asc"
            run(
                "curl",
                "-fsSL",
                "--retry",
                "3",
                "--connect-timeout",
                "10",
                "--max-time",
                "30",
                "https://download.docker.com/linux/debian/gpg",
                "-o",
                str(key),
            )
            run("install", "-m", "0644", str(key), str(keyring))
        source.write_text(
            "Types: deb\nURIs: https://download.docker.com/linux/debian\n"
            f"Suites: {suite}\nComponents: stable\nArchitectures: {architecture}\nSigned-By: {keyring}\n"
        )
    run("apt-get", "update")
    run("apt-get", "install", "-y", "--no-remove", "--no-install-recommends", *DOCKER_PACKAGES)


def grant_docker_access(user: pwd.struct_passwd) -> bool:
    """Add the user to the Docker group if membership is missing."""
    try:
        group = grp.getgrnam("docker")
    except KeyError:
        raise ValueError("The system Docker installation did not provide a docker group.") from None
    if group.gr_gid in os.getgrouplist(user.pw_name, user.pw_gid):
        return False
    run("usermod", "-aG", "docker", user.pw_name)
    return True


def prepare_docker(configured_user: str | None) -> None:
    """Prepare system Docker and verify that the selected user can access it."""
    if os.geteuid() != 0:
        raise ValueError("Run prepare-docker with sudo.")
    suite = debian_suite()
    user = docker_user(configured_user)
    if not Path("/run/systemd/system").is_dir():
        raise ValueError("Start this command on a Debian host running systemd.")

    have_client = shutil.which("docker") is not None
    have_daemon = shutil.which("dockerd") is not None
    if have_client != have_daemon:
        raise ValueError("Found an incomplete Docker installation. Install system Docker and Compose before retrying.")
    print("Debian packages: " + ", ".join(BASE_PACKAGES + (() if have_client else DOCKER_PACKAGES)), flush=True)
    run("apt-get", "update")
    prepare_time_sync()
    run("apt-get", "install", "-y", "--no-remove", "--no-install-recommends", *BASE_PACKAGES)
    if not have_client:
        install_docker(suite)
    docker("compose", "version")
    run("systemctl", "enable", "--now", "docker.service")
    security = json.loads(docker("info", "--format", "{{json .SecurityOptions}}", capture=True).stdout)
    if any("name=rootless" in item for item in (security or [])):
        raise ValueError("The system Docker socket points to a rootless daemon.")
    changed = grant_docker_access(user)
    user_docker = ("runuser", "-u", user.pw_name, "--", "docker")
    env = docker_environment()
    run(*user_docker, "--host", DOCKER_SOCKET, "info", "--format", "Docker server: {{.ServerVersion}}", env=env)
    try:
        endpoint = run(
            *user_docker,
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
            capture=True,
            env=env,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        print("Could not check the user's default Docker context.")
    else:
        if endpoint not in (DOCKER_SOCKET, "unix:///run/docker.sock"):
            print(
                "The user's active Docker context uses another endpoint. "
                "Use 'docker context use default' to select system Docker."
            )
    print(f"System Docker is ready for {user.pw_name}.")
    if changed:
        print("Sign out and sign in again to use the new docker group in your shell.")
