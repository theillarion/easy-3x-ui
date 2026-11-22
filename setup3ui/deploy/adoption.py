"""Move a supported legacy Compose deployment into the managed layout."""

import json
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from setup3ui.deploy.install import install
from setup3ui.host.docker import docker
from setup3ui.host.files import check_root_owned_directory
from setup3ui.panel.discovery import discover_panels, show_panels
from setup3ui.panel.tls import _backup_database


def adopt(config: Mapping[str, Any], ports: Mapping[str, int]) -> None:
    """Archive the old working directory and import its database through 3x-ui."""
    if os.geteuid() != 0:
        raise ValueError("Run adopt with sudo.")
    path = Path(config["install_dir"]).resolve()
    panels = discover_panels()
    show_panels(panels)
    if len(panels) != 1 or panels[0]["rootless"]:
        raise ValueError("Adoption requires one system Docker panel. Use a database backup to move a rootless panel.")
    panel = panels[0]
    if panel["databases"] != [str(path / "db")] or panel["compose_dir"] != str(path):
        raise ValueError(f"Adoption requires the legacy Compose project and db directory under {path}.")
    project = panel["project"]
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", project):
        raise ValueError("Could not identify the old Docker Compose project.")
    old_file = Path(panel["compose_files"] or "")
    if old_file.parent != path or not old_file.is_file() or old_file.is_symlink():
        raise ValueError("Adoption requires one legacy Compose file in the install directory.")
    if (path / "config.yml").exists():
        raise ValueError("A managed configuration already exists.")
    check_root_owned_directory(path)
    compose = ("compose", "-p", project, "--project-directory", str(path), "-f", str(old_file))
    service_names = docker(*compose, "config", "--services", capture=True).stdout.split()
    if panel["service"] not in service_names or set(service_names) - {panel["service"], "caddy", "certbot"}:
        raise ValueError("The old Compose project contains services outside this installer.")
    members = docker(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}", capture=True
    ).stdout.split()
    if members:
        images = docker("inspect", "--format", "{{json .Config.Image}}", *members, capture=True).stdout.splitlines()
        approved = ("ghcr.io/mhsanaei/3x-ui", "mhsanaei/3x-ui", "caddy", "setup-3x-ui-caddy", "certbot/certbot")
        if any(not json.loads(image).startswith(approved) for image in images):
            raise ValueError("The old Compose project contains an unknown image.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    archive = path.with_name(f"{path.name}-before-adopt-{stamp}")
    database = path / "db" / "x-ui.db"
    _backup_database(database, path)
    moved = False
    try:
        docker(*compose, "down")
        snapshot = _backup_database(database, path)
        path.rename(archive)
        moved = True
        install(config, ports, source=archive / snapshot.relative_to(path))
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt):
        try:
            if moved:
                if path.exists():
                    raise ValueError(f"The new directory needs inspection. Previous installation: {archive}.")
                archive.rename(path)
            docker(*compose, "up", "-d", panel["service"], *(["caddy"] if "caddy" in service_names else []))
        except (OSError, subprocess.CalledProcessError) as exc:
            location = archive if archive.exists() else path
            raise ValueError(f"Could not restart the old panel. Previous installation: {location}.") from exc
        raise
    print(f"Previous Compose files and data: {archive}")
