"""Write files safely and check ownership of host directories."""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    """Replace a file atomically with the requested permissions."""
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.chmod(temporary_path, mode)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def check_root_owned_directory(path: Path) -> None:
    """Require a directory chain writable only by root."""
    for entry in (path, *path.parents):
        status = entry.stat()
        if status.st_uid or status.st_mode & 0o022 or not entry.is_dir():
            raise ValueError(f"{entry} must be a directory writable only by root.")
