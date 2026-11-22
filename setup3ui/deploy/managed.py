"""Read a managed installation and take a consistent backup."""

import fcntl
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from setup3ui.host.commands import run
from setup3ui.host.docker import docker
from setup3ui.host.files import check_root_owned_directory

DEFAULT_DIRECTORY = Path("/opt/3x-ui")


@contextmanager
def operation_lock() -> Iterator[None]:
    """Keep two management commands from changing the installation together."""
    if os.geteuid() != 0:
        raise ValueError("Run this command with sudo.")
    fd = os.open("/run/setup-3x-ui.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another setup operation is running.") from None
        yield


def managed_directory(directory: Path) -> Path:
    """Locate the project files before operating on the installation."""
    if os.geteuid() != 0:
        raise ValueError("Run this command with sudo.")
    directory = directory.resolve()
    if not all((directory / item).is_file() for item in ("config.yml", "compose.yml", "db/x-ui.db")):
        raise ValueError(f"No managed installation at {directory}.")
    check_root_owned_directory(directory)
    return directory


def compose(directory: Path, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run Compose for this installation."""
    return docker(
        "compose", "--project-directory", str(directory), "-f", str(directory / "compose.yml"), *args, capture=capture
    )


def show_status(directory: Path, https: str) -> None:
    """Print container, certificate and inbound status."""
    docker("info", "--format", "System Docker {{.ServerVersion}}")
    compose(directory, "ps", "--all")
    database = directory / "db" / "x-ui.db"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            rows = db.execute(
                "SELECT tag, remark, protocol, listen, port, enable FROM inbounds ORDER BY port"
            ).fetchall()
            web = dict(db.execute("SELECT key, value FROM settings"))
    except sqlite3.Error as exc:
        raise ValueError(f"Could not read inbounds: {exc}") from exc
    for tag, remark, protocol, listen, port, enable in rows:
        state = "enabled" if enable else "disabled"
        print(f"Inbound {tag} | {remark} | {protocol} | {listen or '*'}:{port} | {state}")
    if not rows:
        print("No inbounds.")
    for label, prefix, path_key in (("Panel", "web", "webBasePath"), ("Subscription", "sub", "subPath")):
        if web.get(prefix + "Domain") and web.get(path_key):
            port = ":" + web[prefix + "Port"] if https == "certbot" and web.get(prefix + "Port") else ""
            print(f"{label}: https://{web[prefix + 'Domain']}{port}{web[path_key]}")
    certificates = (
        (directory / "certs" / "live").glob("*/fullchain.pem")
        if https == "certbot"
        else (directory / "caddy-data" / "caddy" / "certificates").glob("*/*/*.crt")
    )
    for certificate in sorted(certificates):
        run("openssl", "x509", "-in", str(certificate), "-noout", "-subject", "-enddate")


def backup_installation(directory: Path) -> Path:
    """Copy a consistent SQLite snapshot and the matching config files."""
    backup_root = directory / "backups"
    backup_root.mkdir(mode=0o700, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_root / timestamp
    database = directory / "db" / "x-ui.db"
    try:
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=backup_root) as stage:
            staging = Path(stage)
            with (
                closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as source,
                closing(sqlite3.connect(staging / "x-ui.db")) as snapshot,
            ):
                source.backup(snapshot)
                if snapshot.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ValueError("Could not verify the database backup.")
            for name in ("config.yml", "compose.yml"):
                shutil.copyfile(directory / name, staging / name)
            caddyfile = directory / "caddy-config" / "Caddyfile"
            if caddyfile.is_file():
                shutil.copyfile(caddyfile, staging / "Caddyfile")
            for file in staging.iterdir():
                file.chmod(0o600)
            staging.rename(target)
    except sqlite3.Error as exc:
        raise ValueError(f"Could not back up the database: {exc}") from exc
    return target
