"""Verify a release archive before replacing the local installer."""

import hashlib
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from urllib.request import urlopen

from setup3ui.deploy.release import deploy_cli


def update_installer(source: str, expected_hash: str | None) -> None:
    """Stage a local or HTTPS release archive and replace the CLI files."""
    with tempfile.TemporaryDirectory(prefix="setup-3x-ui-release-") as temporary:
        stage = Path(temporary)
        archive_path = stage / "archive.tar.gz"
        if "://" in source:
            if urlsplit(source).scheme != "https" or not expected_hash:
                raise ValueError("An HTTPS installer archive requires its SHA-256.")
            with urlopen(source, timeout=30) as response:  # noqa: S310
                if urlsplit(response.geturl()).scheme != "https":
                    raise ValueError("The archive redirected away from HTTPS.")
                payload = response.read(8_000_001)
            if len(payload) > 8_000_000:
                raise ValueError("Installer archive is too large.")
            archive_path.write_bytes(payload)
        else:
            candidate = Path(source)
            if candidate.stat().st_size > 8_000_000:
                raise ValueError("Installer archive is too large.")
            shutil.copyfile(candidate, archive_path)
        if expected_hash:
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash) or expected_hash.lower() != digest:
                raise ValueError("Installer archive SHA-256 does not match.")
        output = stage / "project"
        output.mkdir(mode=0o700)
        size = 0
        try:
            with tarfile.open(archive_path, "r:gz") as bundle:
                for member in bundle:
                    name = PurePosixPath(member.name)
                    if name.is_absolute() or ".." in name.parts or not name.parts:
                        raise ValueError("Installer archive has an invalid path.")
                    if member.isdir() and name.parts[0] in {"templates", "www", "setup3ui"}:
                        continue
                    if not member.isfile():
                        raise ValueError("Installer archive has an unsupported entry.")
                    size += member.size
                    if size > 4_000_000:
                        raise ValueError("Unpacked installer archive is too large.")
                    files = {"Caddy.Dockerfile", "xray-default.json", "install.sh", "setup-3x-ui"}
                    if name.parts[0] not in {"templates", "www", "setup3ui"} and (
                        len(name.parts) != 1 or (name.name not in files and name.suffix != ".py")
                    ):
                        raise ValueError("Installer archive contains an unexpected file.")
                    data = bundle.extractfile(member)
                    if data is None:
                        raise ValueError("An installer file could not be read.")
                    path = output.joinpath(*name.parts)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data.read())
        except (tarfile.TarError, EOFError) as exc:
            raise ValueError("Could not unpack the installer archive.") from exc
        modules = (
            ("setup3ui/__init__.py", "setup3ui/cli.py", "setup3ui/deploy/release.py")
            if (output / "setup3ui").is_dir()
            else ("deploy_cli.py", "certificate_renewal.py")
        )
        for filename in ("setup_3x_ui.py", *modules):
            if not (output / filename).is_file():
                raise ValueError(f"Installer archive is missing {filename}.")
        try:
            for file in output.rglob("*.py"):
                compile(file.read_bytes(), str(file), "exec")
        except SyntaxError as exc:
            raise ValueError(f"Could not load updated Python files: {exc.msg}.") from exc
        deploy_cli(output)
    print("Installer files updated.")
