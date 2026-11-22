"""Configure HTTPS in an initialized and stopped 3x-ui panel."""

import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from setup3ui import ASSETS
from setup3ui.certificates.acme import check_certificate
from setup3ui.host.docker import docker


def _check_database_is_offline(database: Path) -> None:
    containers = docker("ps", "-q", capture=True).stdout.split()
    if not containers:
        return
    result = docker("inspect", "--format", "{{json .Mounts}}", *containers, capture=True)
    for container, line in zip(containers, result.stdout.splitlines(), strict=True):
        for mount in json.loads(line) or []:
            if mount.get("Source") and database.is_relative_to(Path(mount["Source"]).resolve()):
                raise ValueError(f"Stop container {container} before changing the panel database.")


def _backup_database(database: Path, install_dir: Path) -> Path:
    backup_dir = install_dir / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"x-ui-{timestamp}.db"
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with (
            closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as source,
            closing(sqlite3.connect(backup)) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Could not verify the database backup.")
    except (OSError, ValueError, sqlite3.Error):
        backup.unlink(missing_ok=True)
        raise
    return backup


def configure_panel_tls(
    install_dir: Path,
    panel_domain: str,
    panel_port: int,
    subscription_domain: str,
    subscription_port: int,
    *,
    fresh: bool = False,
    tls_at_panel: bool = True,
    restore_paths: tuple[str, str] | None = None,
    staged_database: Path | None = None,
) -> tuple[str, str, Path | None]:
    """Set panel and subscription HTTPS on disk and return the public URLs."""
    if os.geteuid() != 0:
        raise ValueError("Run configure-panel-tls with sudo.")
    install_dir = install_dir.resolve()
    database = (staged_database or install_dir / "db" / "x-ui.db").resolve()
    if tls_at_panel:
        cert_dir = install_dir / "certs" / "live" / panel_domain
        check_certificate(cert_dir / "fullchain.pem", cert_dir / "privkey.pem")
    if not database.is_file():
        raise ValueError(f"Initialize the 3x-ui database first: {database}")
    if staged_database is None:
        _check_database_is_offline(database)

    cert_mount = PurePosixPath("/certs/live") / panel_domain
    chain, key = str(cert_mount / "fullchain.pem"), str(cert_mount / "privkey.pem")
    if not tls_at_panel:
        chain = key = ""
    listen = "" if tls_at_panel else "127.0.0.1"
    settings = {
        "webDomain": panel_domain,
        "webPort": str(panel_port),
        "webCertFile": chain,
        "webKeyFile": key,
        "subDomain": subscription_domain,
        "subPort": str(subscription_port),
        "subCertFile": chain,
        "subKeyFile": key,
        "subEnable": "true",
    }
    if fresh or restore_paths or not tls_at_panel:
        settings.update(webListen=listen, subListen=listen)
    backup = None
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
            for setting in settings.keys() | {"webBasePath", "subPath", "subURI", "subJsonURI", "xrayTemplateConfig"}:
                if sum(row[0] == setting for row in rows) > 1:
                    raise ValueError(f"The database contains more than one value for {setting}.")
            existing = dict(rows)
            web_path = (None if fresh else existing.get("webBasePath")) or f"/{secrets.token_hex(8)}/"
            sub_path = (None if fresh else existing.get("subPath")) or f"/{secrets.token_hex(8)}/"
            if restore_paths:
                web_path, sub_path = restore_paths
            web_authority = f"{panel_domain}:{panel_port}" if tls_at_panel else panel_domain
            sub_authority = f"{subscription_domain}:{subscription_port}" if tls_at_panel else subscription_domain
            web_url = f"https://{web_authority}{web_path}"
            sub_url = f"https://{sub_authority}{sub_path}"
            settings.update(webBasePath=web_path, subPath=sub_path, subURI=sub_url)
            if restore_paths and "subJsonURI" in existing:
                settings["subJsonURI"] = ""
            if fresh and restore_paths is None:
                settings["panelGuid"] = existing.get("panelGuid") or str(uuid4())
                template_text = existing.get("xrayTemplateConfig") or (ASSETS / "xray-default.json").read_text()
                template = json.loads(template_text)
                template.setdefault("log", {})["access"] = "/var/log/x-ui/access.log"
                settings["xrayTemplateConfig"] = json.dumps(template, ensure_ascii=False)
            changes = {setting: value for setting, value in settings.items() if existing.get(setting) != value}
            if changes:
                if staged_database is None:
                    backup = _backup_database(database, install_dir)
                for setting, value in changes.items():
                    if setting in existing:
                        connection.execute("UPDATE settings SET value = ? WHERE key = ?", (value, setting))
                    else:
                        connection.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (setting, value))
    except sqlite3.Error as exc:
        raise ValueError(f"Could not configure the 3x-ui database: {exc}") from exc
    return web_url, sub_url, backup
