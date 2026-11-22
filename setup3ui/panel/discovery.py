"""Find previous 3x-ui containers and their mounted database directories."""

import json
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from setup3ui.host.commands import run
from setup3ui.host.docker import DOCKER_SOCKET, docker_environment


def discover_panels() -> list[dict[str, Any]]:
    """List 3x-ui containers from system Docker and the installer user's rootless Docker."""
    if not shutil.which("docker"):
        return []
    host_sockets = [Path(DOCKER_SOCKET.removeprefix("unix://"))]
    user_name = os.environ.get("SUDO_USER")
    if user_name:
        user = pwd.getpwnam(user_name)
        host_sockets.append(Path(f"/run/user/{user.pw_uid}/docker.sock"))
    entries = []
    for path in host_sockets:
        if not path.exists() or not stat.S_ISSOCK(path.stat().st_mode):
            continue
        env = docker_environment()
        cmd = ("docker", "--host", f"unix://{path}")
        try:
            ids = run(*cmd, "ps", "-aq", capture=True, env=env).stdout.split()
            if not ids:
                continue
            objects = json.loads(run(*cmd, "inspect", *ids, capture=True, env=env).stdout)
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"Could not inspect Docker at {path}.") from exc
        for container in objects:
            config = container["Config"]
            if not config["Image"].lower().startswith(("ghcr.io/mhsanaei/3x-ui", "mhsanaei/3x-ui")):
                continue
            mounts = [mount.get("Source") for mount in container["Mounts"] if mount.get("Destination") == "/etc/x-ui"]
            labels = config.get("Labels") or {}
            entries.append(
                {
                    "project": labels.get("com.docker.compose.project"),
                    "service": labels.get("com.docker.compose.service"),
                    "compose_dir": labels.get("com.docker.compose.project.working_dir"),
                    "compose_files": labels.get("com.docker.compose.project.config_files"),
                    "name": container["Name"].lstrip("/"),
                    "id": container["Id"],
                    "socket": str(path),
                    "rootless": path != host_sockets[0],
                    "running": container["State"]["Running"],
                    "databases": mounts,
                }
            )
    return entries


def show_panels(entries: list[dict[str, Any]]) -> None:
    """Print the existing containers and their data locations."""
    if not entries:
        print("No 3x-ui containers found.")
    for entry in entries:
        print(f"{entry['name']} | {'rootless' if entry['rootless'] else 'system Docker'} | {entry['socket']}")
        if entry["databases"]:
            for mount in entry["databases"]:
                print(f"Database location: {mount}/x-ui.db")
        else:
            print("Could not locate a database mount.")
