"""Create a new panel database."""

import getpass
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from setup3ui.host.docker import docker

INIT = r"""
set -eu
IFS= read -r username
IFS= read -r password
/app/x-ui setting -username "$username" -password "$password"
unset username password
/app/x-ui setting -getApiToken -tokenName setup-3x-ui -tokenScope node-sync
"""


def credentials(config: Mapping[str, Any], *, interactive: bool = True) -> tuple[str, str]:
    """Read the credentials for a new panel."""
    name = config.get("admin_username")
    password = config.get("admin_password")
    if name is None and interactive and os.isatty(0):
        name = input("Panel username: ").strip()
    if password is None and interactive and os.isatty(0):
        password = getpass.getpass("Panel password: ")
    if not all(
        isinstance(value, str) and value.strip() and not any(c in value for c in "\0\r\n") for value in (name, password)
    ):
        raise ValueError("Set admin_username and admin_password or run install in a terminal.")
    assert isinstance(name, str) and isinstance(password, str)
    return name, password


def initialize_database(
    install_dir: Path,
    image: str,
    username: str,
    password: str,
) -> str:
    """Create a new database and return its node token."""
    result = docker(
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--mount",
        f"type=bind,source={install_dir / 'db'},target=/etc/x-ui",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        INIT,
        capture=True,
        input_text=f"{username}\n{password}\n",
    )
    os.chmod(install_dir / "db" / "x-ui.db", 0o600)
    token = re.search(r"(?m)^apiToken:\s*([A-Za-z0-9_-]+)\s*$", result.stdout)
    if not token:
        raise ValueError("Could not create a node API token.")
    return token.group(1)
