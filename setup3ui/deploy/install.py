"""Install a panel with a new database."""

import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from setup3ui import ASSETS
from setup3ui.certificates.acme import issue_certificate
from setup3ui.certificates.renewal import install_renewal_timer
from setup3ui.deploy.caddy import prepare_caddy_image, render_caddy
from setup3ui.deploy.compose import render_compose
from setup3ui.deploy.managed import grant_installation_access
from setup3ui.host.dns import check_dns
from setup3ui.host.docker import docker, prepare_docker
from setup3ui.host.files import atomic_write, check_root_owned_directory
from setup3ui.host.preflight import check_host
from setup3ui.panel.api import PanelAPI
from setup3ui.panel.bootstrap import credentials, initialize_database
from setup3ui.panel.discovery import discover_panels, show_panels
from setup3ui.panel.health import verify_https
from setup3ui.panel.inbounds import create_inbounds, default_reality_tag
from setup3ui.panel.tls import _backup_database, configure_panel_tls


def install(
    config: Mapping[str, Any],
    ports: Mapping[str, int],
    *,
    source: Path | None = None,
    interactive: bool = True,
) -> None:
    """Set up a new panel, verify it locally, and show its connection details."""
    import yaml

    if os.geteuid() != 0:
        raise ValueError("Run install with sudo.")
    use_caddy = config["https"] == "caddy"
    directory = Path(config["install_dir"]).resolve()
    if directory.exists():
        raise ValueError(f"{directory} already exists. Fresh install needs a new directory.")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(directory)):
        raise ValueError("Use an install directory with only letters, digits, dots, underscores, and dashes.")
    existing = [panel for panel in discover_panels() if panel["running"]]
    if existing:
        show_panels(existing)
        raise ValueError("An existing panel is running. Use discover to find its database before moving it.")
    check_root_owned_directory(directory.parent)
    if source:
        from setup3ui.panel.restore import backup_file, panel_paths, stage_database

        source = backup_file(source)
    else:
        username, password = credentials(config, interactive=interactive)
    prepare_docker(config.get("docker_user"))
    check_host(ports)
    domains = [config["panel_domain"], config["subscription_domain"]]
    domains.extend(item["origin"] for item in config["cdn"])
    check_dns(domains)
    registered = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=3x-ui", capture=True)
    if registered.stdout.strip():
        raise ValueError("A Docker Compose project named 3x-ui already exists.")
    definition = render_compose(config)
    image = yaml.safe_load(definition)["services"]["panel"]["image"]
    try:
        docker("image", "inspect", image, capture=True)
    except subprocess.CalledProcessError:
        docker("pull", image)

    if use_caddy:
        prepare_caddy_image()

    directory.mkdir(mode=0o700)
    compose = ("compose", "--project-directory", str(directory), "-f", str(directory / "compose.yml"))
    compose_started = False
    try:
        for name in ("db", "logs"):
            (directory / name).mkdir(mode=0o700)
        atomic_write(directory / "compose.yml", definition.encode(), 0o600)
        token = ""
        restore_paths = None
        database = directory / "db" / "x-ui.db"
        if source:
            stage_database(source, database)
            restore_paths = panel_paths(database)
        else:
            token = initialize_database(directory, image, username, password)
        panel, subscription = config["panel_domain"], config["subscription_domain"]
        if use_caddy:
            for name in ("caddy-config", "caddy-data", "caddy-state"):
                (directory / name).mkdir(mode=0o700)
            shutil.copytree((ASSETS / "www"), directory / "www")
            atomic_write(directory / "caddy-config" / "Caddyfile", render_caddy(config).encode(), 0o644)
            docker(
                *compose,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "caddy",
                "caddy",
                "validate",
                "--config",
                "/etc/caddy/Caddyfile",
            )
        else:
            issue_certificate(directory, panel, subscription, config["email"])
        panel_url, sub_url, _ = configure_panel_tls(
            directory,
            panel,
            config["panel_port"],
            subscription,
            config["subscription_port"],
            fresh=True,
            tls_at_panel=not use_caddy,
            restore_paths=restore_paths,
            staged_database=database if source else None,
        )
        compose_started = True
        docker(*compose, "up", "-d", *("panel", "caddy") if use_caddy else ("panel",))
        verify_https(
            panel, 443 if use_caddy else config["panel_port"], "/" + panel_url.split("/", 3)[3], {200, 302, 303}
        )
        sub_path = "/" + sub_url.split("/", 3)[3] + "setup-check-" + secrets.token_hex(12)
        verify_https(subscription, 443 if use_caddy else config["subscription_port"], sub_path, {404})
        saved = {key: value for key, value in config.items() if key not in {"admin_username", "admin_password"}}
        if source is None:
            for inbound in ("reality", "direct_xhttp"):
                if config[inbound]:
                    field = f"{inbound}_tag"
                    saved.setdefault(field, default_reality_tag(config, xhttp=inbound == "direct_xhttp"))
        atomic_write(directory / "config.yml", yaml.safe_dump(saved, sort_keys=False).encode(), 0o600)
        if source is None and (config["reality"] or config["direct_xhttp"] or config["cdn"]):
            api = PanelAPI(panel, 443 if use_caddy else config["panel_port"], "/" + panel_url.split("/", 3)[3])
            api.login(username, password)
            _backup_database(database, directory)
            create_inbounds(api, config, image)
        with (directory / "logs" / "setup.log").open("a", encoding="utf-8") as log:
            log.write("Installed 3x-ui and verified HTTPS from this server.\n")
        grant_installation_access(directory, config.get("docker_user"))
        if not use_caddy:
            install_renewal_timer(directory, panel)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt):
        if compose_started:
            try:
                docker(*compose, "down")
            except subprocess.CalledProcessError as exc:
                raise ValueError(f"Could not stop the failed installation. Files kept in {directory}.") from exc
        shutil.rmtree(directory)
        raise
    print(f"Panel: {panel_url}\nSubscription base URL: {sub_url}")
    if token:
        print(f"Node API token: {token}")
    print("Public access can be checked with the test command.")
