"""Update or remove a managed installation."""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from setup3ui.certificates.acme import certificate_lock
from setup3ui.certificates.renewal import UNIT, UNIT_DIR
from setup3ui.deploy.caddy import prepare_caddy_image
from setup3ui.deploy.managed import backup_installation, compose
from setup3ui.host.commands import run
from setup3ui.host.docker import docker
from setup3ui.host.files import atomic_write
from setup3ui.panel.api import PanelAPI
from setup3ui.panel.bootstrap import credentials, initialize_database
from setup3ui.panel.health import verify_https
from setup3ui.panel.inbounds import create_inbounds
from setup3ui.panel.restore import settings
from setup3ui.panel.tls import configure_panel_tls


def _panel_health(directory: Path, config: Mapping[str, Any]) -> None:
    rows = settings(directory / "db" / "x-ui.db")
    caddy = config["https"] == "caddy"
    verify_https(
        config["panel_domain"], 443 if caddy else config["panel_port"], rows.get("webBasePath") or "/", {200, 302, 303}
    )
    verify_https(
        config["subscription_domain"],
        443 if caddy else config["subscription_port"],
        (rows.get("subPath") or "/sub/") + "setup-check-missing",
        {404},
    )


def _image_id(name: str) -> str:
    return docker("image", "inspect", "--format", "{{.Id}}", name, capture=True).stdout.strip()


def _rollback_panel(directory: Path, previous_id: str, image_name: str, backup: Path) -> None:
    compose(directory, "stop", "panel")
    db = directory / "db" / "x-ui.db"
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    atomic_write(db, (backup / "x-ui.db").read_bytes(), 0o600)
    docker("image", "tag", previous_id, image_name)
    compose(directory, "up", "-d", "--no-deps", "--force-recreate", "panel")


def update(directory: Path, config: Mapping[str, Any], component: str | None) -> None:
    """Update images explicitly, preserving the previous state before a restart."""
    import yaml

    compose_path = directory / "compose.yml"
    definition = json.loads(compose(directory, "config", "--format", "json", capture=True).stdout)
    services = definition["services"]
    active = ["panel", "caddy" if config["https"] == "caddy" else "certbot"]
    if component and component not in active:
        raise ValueError(f"This installation uses {' and '.join(active)}.")
    for service in [component] if component else active:
        if service == "certbot":
            with certificate_lock(directory):
                docker("pull", "certbot/certbot:latest")
                current = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
                current["services"]["certbot"]["image"] = "certbot/certbot:latest"
                atomic_write(compose_path, yaml.safe_dump(current, sort_keys=False).encode(), 0o600)
            print("Certbot image updated.")
            continue
        image_name = services[service]["image"]
        container = compose(directory, "ps", "-q", service, capture=True).stdout.strip()
        if not container:
            raise ValueError(f"Start the {service} container before updating it.")
        old_id = docker("inspect", "--format", "{{.Image}}", container, capture=True).stdout.strip()
        if service == "caddy":
            prepare_caddy_image(image_name, refresh=True)
        else:
            docker("pull", image_name)
        if _image_id(image_name) == old_id:
            print(f"{service} is up to date.")
            continue
        backup = backup_installation(directory) if service == "panel" else None
        try:
            if service == "caddy":
                compose(
                    directory,
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
            compose(directory, "up", "-d", "--no-deps", "--force-recreate", service)
            _panel_health(directory, config)
        except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt):
            try:
                if backup:
                    _rollback_panel(directory, old_id, image_name, backup)
                else:
                    docker("image", "tag", old_id, image_name)
                    compose(directory, "up", "-d", "--no-deps", "--force-recreate", service)
                _panel_health(directory, config)
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                raise ValueError(
                    f"{service} rollback failed. Backup: {backup or 'previous Docker image ' + old_id}."
                ) from exc
            raise
        with (directory / "logs" / "setup.log").open("a", encoding="utf-8") as log:
            log.write(f"Updated {service}.\n")
        print(f"{service} updated.")


def uninstall(directory: Path, *, purge: bool) -> None:
    """Remove the managed containers. Keep local data unless purge was confirmed."""
    if directory == Path("/") or directory in (Path("/opt"), Path("/var"), Path("/usr")):
        raise ValueError("Use a directory dedicated to this installation.")
    config = directory / "config.yml"
    timer_service = UNIT_DIR / f"{UNIT}.service"
    if timer_service.is_file() and f" {directory} " in timer_service.read_text(encoding="utf-8"):
        run("systemctl", "disable", "--now", f"{UNIT}.timer")
        run("systemctl", "stop", f"{UNIT}.service")
        timer_service.unlink()
        (UNIT_DIR / f"{UNIT}.timer").unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
    compose(directory, "down")
    if purge:
        # Only delete paths used by this installer.
        for name in ("db", "certs", "certbot-work", "certbot-log", "backups", "logs", "caddy-data", "caddy-state"):
            path = directory / name
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        for name in (".certificate-applied", ".certificate.lock"):
            (directory / name).unlink(missing_ok=True)
    for name in ("caddy-config", "www"):
        path = directory / name
        if path.is_symlink():
            path.unlink()
        elif purge and path.is_dir():
            shutil.rmtree(path)
    if purge:
        config.unlink()
        (directory / "compose.yml").unlink()
        try:
            directory.rmdir()
        except OSError:
            print(f"Other files remain in {directory}.")
    else:
        print(f"Saved data and configuration: {directory}.")


def reset(directory: Path, config: Mapping[str, Any], auth: Mapping[str, Any], *, interactive: bool = True) -> None:
    """Replace the database after backing up the current one."""
    import yaml

    username, password = credentials(auth, interactive=interactive)
    compose_config = yaml.safe_load((directory / "compose.yml").read_text(encoding="utf-8"))
    image = compose_config["services"]["panel"]["image"]
    backup = backup_installation(directory)
    proxy = config["https"] == "caddy"
    panel = config["panel_domain"]
    database_dir = directory / "db"
    with tempfile.TemporaryDirectory(prefix=".reset-", dir=directory) as stage:
        old_db = Path(stage) / "old-db"
        compose(directory, "stop", "panel")
        database_dir.rename(old_db)
        try:
            database_dir.mkdir(mode=0o700)
            token = initialize_database(directory, image, username, password)
            panel_url, sub_url, _ = configure_panel_tls(
                directory,
                panel,
                config["panel_port"],
                config["subscription_domain"],
                config["subscription_port"],
                fresh=True,
                tls_at_panel=not proxy,
            )
            compose(directory, "up", "-d", "--no-deps", "panel")
            _panel_health(directory, config)
            if config["reality"] or config["direct_xhttp"] or config["cdn"]:
                port = 443 if proxy else config["panel_port"]
                api = PanelAPI(panel, port, settings(database_dir / "x-ui.db")["webBasePath"])
                api.login(username, password)
                create_inbounds(api, config, image)
        except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt):
            try:
                compose(directory, "stop", "panel")
                if database_dir.exists():
                    shutil.rmtree(database_dir)
                old_db.rename(database_dir)
                compose(directory, "up", "-d", "--no-deps", "panel")
            except (OSError, subprocess.CalledProcessError) as exc:
                raise ValueError(f"Reset rollback failed. Database backup: {backup / 'x-ui.db'}.") from exc
            raise
    os.chmod(database_dir / "x-ui.db", 0o600)
    print(f"Previous database: {backup / 'x-ui.db'}")
    print(f"Panel: {panel_url}\nSubscription base URL: {sub_url}\nNode API token: {token}")


def rotate_token(directory: Path, config: Mapping[str, Any], name: str) -> None:
    """Replace an API token after taking a backup and stopping the panel."""
    import re

    import yaml

    definition = yaml.safe_load((directory / "compose.yml").read_text(encoding="utf-8"))
    image = definition["services"]["panel"]["image"]
    backup = backup_installation(directory)
    compose(directory, "stop", "panel")
    try:
        result = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,source={directory / 'db'},target=/etc/x-ui",
            "--entrypoint",
            "/app/x-ui",
            image,
            "setting",
            "-getApiToken",
            "-tokenName",
            name,
            "-tokenScope",
            "node-sync",
            capture=True,
        )
        token = re.search(r"(?m)^apiToken:\s*([A-Za-z0-9_-]+)\s*$", result.stdout)
        if not token:
            raise ValueError("3x-ui did not return a node token.")
        compose(directory, "up", "-d", "--no-deps", "panel")
        _panel_health(directory, config)
    except (ValueError, OSError, subprocess.CalledProcessError, KeyboardInterrupt):
        try:
            _rollback_panel(directory, _image_id(image), image, backup)
            _panel_health(directory, config)
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"Token rollback failed. Backup: {backup}.") from exc
        raise
    print(f"Node API token: {token.group(1)}")
