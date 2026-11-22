"""Install the local CLI under /usr/local."""

import os
import shutil
import tempfile
from pathlib import Path

from setup3ui import RUNTIME_DIR
from setup3ui.host.files import atomic_write, check_root_owned_directory

CLI = Path("/usr/local/bin/setup-3x-ui")
LAUNCHER = b'#!/bin/sh\nexec /usr/bin/python3 -E -s /usr/local/lib/setup-3x-ui/app/setup_3x_ui.py "$@"\n'


def deploy_cli(source: Path) -> None:
    """Copy the project files and publish a launcher."""
    if os.geteuid() != 0:
        raise ValueError("Run CLI setup with sudo.")
    for parent in (RUNTIME_DIR.parent, CLI.parent):
        check_root_owned_directory(parent.resolve())
    RUNTIME_DIR.mkdir(mode=0o755, exist_ok=True)
    if RUNTIME_DIR.is_symlink():
        raise ValueError(f"{RUNTIME_DIR} must be a directory.")
    target = RUNTIME_DIR / "app"
    if target.resolve() == source.resolve():
        return
    if target.is_symlink():
        raise ValueError(f"{target} must be a directory.")
    with tempfile.TemporaryDirectory(prefix=".app-", dir=RUNTIME_DIR) as temporary:
        stage = Path(temporary) / "app"
        stage.mkdir(mode=0o755)
        package = source / "setup3ui"
        if package.is_dir():
            shutil.copyfile(source / "setup_3x_ui.py", stage / "setup_3x_ui.py")
            shutil.copytree(package, stage / "setup3ui", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            for file in (*source.glob("*.py"), source / "Caddy.Dockerfile", source / "xray-default.json"):
                shutil.copyfile(file, stage / file.name)
            for name in ("www", "templates"):
                shutil.copytree(source / name, stage / name)
        old = Path(temporary) / "previous"
        if target.exists():
            target.rename(old)
        try:
            stage.rename(target)
        except OSError:
            if old.exists():
                old.rename(target)
            raise
    atomic_write(CLI, LAUNCHER, 0o755)
