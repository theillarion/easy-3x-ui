"""Check and clean up the host firewall table owned by an installation."""

import subprocess
import time
from pathlib import Path

from setup3ui.deploy.managed import compose
from setup3ui.host.docker import docker

STATE_DIRECTORY = "nftguard-state"
TABLE_MARKER = ".nftguard-table"
TABLE = "table inet nftguard"
IMAGE_REPOSITORY = "ghcr.io/theillarion/anti-scan-govno"


def _host_nft(directory: Path, *args: str) -> str:
    result = compose(directory, "run", "--rm", "--no-deps", "-T", "--entrypoint", "nft", "nftguard", *args, capture=True)
    return result.stdout


def _table_exists(directory: Path) -> bool:
    return TABLE in _host_nft(directory, "list", "tables").splitlines()


def _instance_running() -> bool:
    instances = docker(
        "ps", "--filter", "label=com.docker.compose.service=nftguard", "--quiet", capture=True,
    )
    images = docker("ps", "--format", "{{.Image}}", capture=True).stdout.splitlines()
    return bool(instances.stdout.strip()) or any(
        image == IMAGE_REPOSITORY or image.startswith((f"{IMAGE_REPOSITORY}:", f"{IMAGE_REPOSITORY}@"))
        for image in images
    )


def check_available(directory: Path) -> None:
    """Refuse to start a second instance or take over a pre-existing table."""
    if _instance_running():
        raise ValueError("Another nftguard container is already running.")
    if _table_exists(directory):
        raise ValueError("The host already has an inet nftguard table. Remove it through its existing installation.")


def wait_ready(directory: Path, timeout: float = 600) -> None:
    """Wait for the first successful image health check."""
    deadline = time.monotonic() + timeout
    reason = ""
    try:
        container = compose(directory, "ps", "-aq", "nftguard", capture=True, timeout=timeout).stdout.strip()
        while container:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = f"Timed out after {timeout:g} seconds."
                break
            state = docker(
                "inspect", "--format", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                container, capture=True, timeout=remaining,
            ).stdout.strip()
            if state == "running healthy":
                return
            if state.startswith(("exited", "dead", "running unhealthy")):
                reason = f"Container state: {state}."
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        reason = f"Timed out after {timeout:g} seconds."
    except subprocess.CalledProcessError as exc:
        reason = (exc.stderr or str(exc)).strip()
    try:
        logs = compose(directory, "logs", "--tail", "20", "nftguard", capture=True, timeout=5)
        detail = (logs.stdout + logs.stderr).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        detail = "Could not read nftguard logs."
    message = "nftguard did not pass its startup health check."
    raise ValueError("\n".join(part for part in (message, reason, detail) if part))


def remove_table(directory: Path) -> None:
    """Stop the table updater and remove the table claimed during installation."""
    marker = directory / TABLE_MARKER
    if not marker.is_file():
        return
    compose(directory, "stop", "nftguard")
    if _table_exists(directory):
        if _instance_running():
            raise ValueError("Another nftguard container is running. The firewall table was kept.")
        _host_nft(directory, "delete", "table", "inet", "nftguard")
    marker.unlink()
