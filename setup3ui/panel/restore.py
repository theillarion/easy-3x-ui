"""Restore a 3x-ui SQLite backup using only local files and Docker."""

import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from setup3ui.deploy.managed import backup_installation, compose
from setup3ui.panel.health import verify_https
from setup3ui.panel.tls import _check_database_is_offline, configure_panel_tls


def settings(path: Path) -> dict[str, str]:
    """Get settings from an existing database."""
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            return dict(connection.execute("SELECT key, value FROM settings"))
    except sqlite3.Error as exc:
        raise ValueError(f"Could not read 3x-ui settings from {path}: {exc}") from exc


def backup_file(source: Path) -> Path:
    """Find the standalone database or the database in a backup directory."""
    path = source / "x-ui.db" if source.is_dir() else source
    if not path.is_file():
        raise ValueError(f"Backup database file does not exist: {path}")
    return path.resolve()


def stage_database(source: Path, target: Path) -> None:
    """Copy and validate the SQLite database without changing the source."""
    source = backup_file(source)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with (
            closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as original,
            closing(sqlite3.connect(target)) as copy,
        ):
            original.backup(copy)
            copy.execute("PRAGMA journal_mode=DELETE")
            account = copy.execute(
                "SELECT 1 FROM users WHERE length(username) > 0 AND length(password) > 0 LIMIT 1"
            ).fetchone()
            if account is None:
                raise ValueError("The backup contains no 3x-ui login.")
            copy.execute("SELECT id, port FROM inbounds LIMIT 0")
            copy.execute("SELECT key, value FROM settings LIMIT 0")
            if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("The selected backup failed the SQLite integrity check.")
    except sqlite3.Error as exc:
        raise ValueError(f"Invalid 3x-ui backup: {exc}") from exc


def panel_paths(database: Path) -> tuple[str, str]:
    """Read and validate URL paths before using them for local checks."""
    values = settings(database)
    paths = (values.get("webBasePath") or "/", values.get("subPath") or "/sub/")
    if any(
        not re.fullmatch(r"/[A-Za-z0-9~_./-]*", path)
        or not path.endswith("/")
        or any(part in {".", ".."} for part in path.split("/"))
        for path in paths
    ):
        raise ValueError("Database has an invalid panel or subscription path.")
    return paths


def _replace_database(db: Path, staged: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    staged.replace(db)


def restore_panel(directory: Path, config: Mapping[str, Any], source: Path) -> Path:
    """Replace the local database and roll back if the new panel fails."""
    db = directory / "db" / "x-ui.db"
    if backup_file(source).samefile(db):
        raise ValueError("Select a backup that differs from the current database.")
    proxy = config["https"] == "caddy"
    panel_port = 443 if proxy else config["panel_port"]
    sub_port = 443 if proxy else config["subscription_port"]
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=db.parent) as stage:
        staged = Path(stage) / "x-ui.db"
        stage_database(source, staged)
        was_running = bool(compose(directory, "ps", "-q", "panel", capture=True).stdout.strip())
        backup = None
        replaced = False
        paths = None
        try:
            compose(directory, "stop", "panel")
            _check_database_is_offline(db)
            paths = panel_paths(db)
            configure_panel_tls(
                directory,
                config["panel_domain"],
                config["panel_port"],
                config["subscription_domain"],
                config["subscription_port"],
                tls_at_panel=not proxy,
                restore_paths=paths,
                staged_database=staged,
            )
            backup = backup_installation(directory)
            _replace_database(db, staged)
            replaced = True
            compose(directory, "up", "-d", "--no-deps", "panel")
            verify_https(config["panel_domain"], panel_port, paths[0], {200, 302, 303})
            verify_https(config["subscription_domain"], sub_port, paths[1] + "setup-check-missing", {404})
        except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt):
            try:
                if replaced and backup is not None:
                    compose(directory, "stop", "panel")
                    _check_database_is_offline(db)
                    original = Path(stage) / "previous.db"
                    stage_database(backup, original)
                    _replace_database(db, original)
                if was_running:
                    compose(directory, "up", "-d", "--no-deps", "panel")
                if was_running and paths:
                    verify_https(config["panel_domain"], panel_port, paths[0], {200, 302, 303})
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                location = backup / "x-ui.db" if backup else db
                raise ValueError(f"Rollback failed. Previous database: {location}.") from exc
            raise
    assert backup is not None
    return backup
