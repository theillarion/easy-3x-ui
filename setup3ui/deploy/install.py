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
from setup3ui.config import NFTGUARD_DEFAULTS
from setup3ui.deploy.caddy import prepare_caddy_image, render_caddy
from setup3ui.deploy.compose import render_compose
from setup3ui.deploy.managed import grant_installation_access
from setup3ui.deploy.nftguard import STATE_DIRECTORY, TABLE_MARKER, check_available, remove_table, wait_ready
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
from setup3ui.terminal import Terminal


def fresh_install_directory(path: Path) -> Path:
    """Check that a new installation can use this directory."""
    if not path.is_absolute():
        raise ValueError("Enter an absolute installation directory.")
    try:
        directory = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Could not resolve the installation directory {path}: {exc}") from exc
    if path.is_symlink() or directory.exists():
        raise ValueError(f"{path} already exists. Fresh install needs a new directory.")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(directory)):
        raise ValueError("Use an install directory with only letters, digits, dots, underscores, and dashes.")
    try:
        check_root_owned_directory(directory.parent)
    except FileNotFoundError as exc:
        raise ValueError(f"The installation parent directory does not exist: {directory.parent}") from exc
    except OSError as exc:
        raise ValueError(f"Could not check the installation parent directory {directory.parent}: {exc}") from exc
    return directory


def install(
    config: Mapping[str, Any],
    ports: Mapping[str, int],
    *,
    source: Path | None = None,
    interactive: bool = True,
) -> None:
    """Set up a new panel, verify it locally, and show its connection details."""
    import yaml

    terminal = Terminal()

    def stage(label: str) -> None:
        if interactive:
            terminal.heading(label)

    if os.geteuid() != 0:
        raise ValueError("Run install with sudo.")
    use_caddy = config["https"] == "caddy"
    directory = fresh_install_directory(Path(config["install_dir"]).resolve())
    existing = [panel for panel in discover_panels() if panel["running"]]
    if existing:
        show_panels(existing)
        raise ValueError("An existing panel is running. Use discover to find its database before moving it.")
    if source:
        from setup3ui.panel.restore import backup_file, panel_paths, stage_database

        source = backup_file(source)
    else:
        if interactive and os.isatty(0) and any(config.get(key) is None for key in ("admin_username", "admin_password")):
            stage("Panel credentials")
        username, password = credentials(config, interactive=interactive)
    stage("Prepare Docker")
    prepare_docker(config.get("docker_user"))
    stage("Check ports and DNS")
    check_host(ports)
    domains = [config["panel_domain"], config["subscription_domain"]]
    domains.extend(item["origin"] for item in config["cdn"])
    check_dns(domains)
    registered = docker("ps", "-aq", "--filter", "label=com.docker.compose.project=3x-ui", capture=True)
    if registered.stdout.strip():
        raise ValueError("A Docker Compose project named 3x-ui already exists.")
    stage("Prepare container images")
    definition = render_compose(config)
    services = yaml.safe_load(definition)["services"]
    image = services["panel"]["image"]
    guard = config.get("nftguard", NFTGUARD_DEFAULTS)
    images = [image]
    if guard["enabled"]:
        images.append(services["nftguard"]["image"])
    for required_image in images:
        try:
            docker("image", "inspect", required_image, capture=True)
        except subprocess.CalledProcessError:
            docker("pull", required_image)

    if use_caddy:
        prepare_caddy_image(services["caddy"]["image"])

    directory.mkdir(mode=0o700)
    compose = ("compose", "--project-directory", str(directory), "-f", str(directory / "compose.yml"))
    compose_started = False
    try:
        for name in ("db", "logs"):
            (directory / name).mkdir(mode=0o700)
        atomic_write(directory / "compose.yml", definition.encode(), 0o600)
        if guard["enabled"]:
            stage("Check nftguard access")
            (directory / STATE_DIRECTORY).mkdir(mode=0o700)
            try:
                check_available(directory)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or "").strip()
                raise ValueError(f"Could not check nftguard access to the host firewall. {detail}".rstrip()) from exc
        token = ""
        restore_paths = None
        database = directory / "db" / "x-ui.db"
        if source:
            stage("Restore panel database")
            stage_database(source, database)
            restore_paths = panel_paths(database)
        else:
            stage("Create panel database")
            token = initialize_database(directory, image, username, password)
        panel, subscription = config["panel_domain"], config["subscription_domain"]
        if use_caddy:
            stage("Configure HTTPS with Caddy")
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
            stage("Request HTTPS certificates with Certbot")
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
        saved = {key: value for key, value in config.items() if key not in {"admin_username", "admin_password"}}
        if source is None:
            for inbound in ("reality", "direct_xhttp"):
                if config[inbound]:
                    field = f"{inbound}_tag"
                    saved.setdefault(field, default_reality_tag(config, xhttp=inbound == "direct_xhttp"))
        atomic_write(directory / "config.yml", yaml.safe_dump(saved, sort_keys=False).encode(), 0o600)
        if guard["enabled"] and not guard["dry_run"]:
            atomic_write(directory / TABLE_MARKER, b"inet nftguard\n", 0o600)
        stage("Start services")
        compose_started = True
        docker(*compose, "up", "-d")
        if guard["enabled"]:
            stage("Verify nftguard")
            wait_ready(directory)
        stage("Verify HTTPS")
        verify_https(
            panel, 443 if use_caddy else config["panel_port"], "/" + panel_url.split("/", 3)[3], {200, 302, 303}
        )
        sub_path = "/" + sub_url.split("/", 3)[3] + "setup-check-" + secrets.token_hex(12)
        verify_https(subscription, 443 if use_caddy else config["subscription_port"], sub_path, {404})
        if source is None and (config["reality"] or config["direct_xhttp"] or config["cdn"]):
            stage("Create inbounds")
            api = PanelAPI(panel, 443 if use_caddy else config["panel_port"], "/" + panel_url.split("/", 3)[3])
            api.login(username, password)
            _backup_database(database, directory)
            create_inbounds(api, config, image)
        stage("Finish installation")
        with (directory / "logs" / "setup.log").open("a", encoding="utf-8") as log:
            log.write("Installed 3x-ui and verified HTTPS from this server.\n")
        grant_installation_access(directory, config.get("docker_user"))
        if not use_caddy:
            install_renewal_timer(directory, panel)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as failure:
        if compose_started:
            try:
                remove_table(directory)
                docker(*compose, "down")
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                detail = getattr(exc, "stderr", None) or str(exc)
                raise ValueError(
                    f"Installation failed: {failure}. Cleanup failed: {detail.strip()} "
                    f"Files kept in {directory}."
                ) from exc
        shutil.rmtree(directory)
        raise
    lines = [f"Panel: {panel_url}", f"Subscription base URL: {sub_url}"]
    if token:
        lines.append(f"Node API token: {token}")
    lines.append("Public access can be checked with the test command.")
    if interactive:
        terminal.box("Installation complete", lines)
    else:
        print("\n".join(lines))
