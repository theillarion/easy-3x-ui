"""Store the interactive installation answers between invocations."""

import fcntl
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from setup3ui.host.files import atomic_write, check_root_owned_directory

STATE_DIRECTORY = Path("/var/lib/setup-3x-ui")
VERSION = 1


def _private(path: Path, kind: int, mode: int) -> None:
    status = path.lstat()
    if status.st_uid != 0 or status.st_mode != (kind | mode):
        raise ValueError(f"{path} must belong to root and have permissions {mode:04o}.")


@contextmanager
def draft_lock() -> Iterator[Path]:
    """Allow a single installation wizard to use the draft at a time."""
    if os.geteuid() != 0:
        raise ValueError("Run install with sudo.")
    directory = STATE_DIRECTORY
    check_root_owned_directory(directory.parent)
    directory.mkdir(mode=0o700, exist_ok=True)
    _private(directory, stat.S_IFDIR, 0o700)
    fd = os.open(directory / "install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another interactive installation is running.") from None
        yield directory / "install-draft.json"


class InvalidDraft(ValueError):
    """The saved data cannot be read by this version of the installer."""


@dataclass
class Draft:
    """Record accepted answers and the results needed to replay the questionnaire."""

    path: Path
    answers: dict[str, str] = field(default_factory=dict)
    previous: dict[str, str] = field(default_factory=dict)
    results: dict[str, list[str]] = field(default_factory=dict)
    panel_installed: bool = False

    @classmethod
    def load(cls, path: Path) -> "Draft | None":
        """Read and check the version and structure of a private draft."""
        try:
            _private(path, stat.S_IFREG, 0o600)
        except FileNotFoundError:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:
            raise InvalidDraft("The installation draft is damaged.") from exc
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != VERSION:
            raise InvalidDraft("The installation draft uses an unsupported format.")
        answers = data.get("answers")
        previous = data.get("previous")
        results = data.get("results")
        installed = data.get("panel_installed")
        if (
            not all(isinstance(item, dict) and all(isinstance(value, str) for value in item.values())
                    for item in (answers, previous))
            or not isinstance(results, dict)
            or any(not isinstance(names, list) or not all(isinstance(name, str) for name in names)
                   for names in results.values())
            or type(installed) is not bool
            or answers.get("action") not in {"install", "restore"}
            or (installed and not Path(answers.get("install_dir", "")).is_absolute())
        ):
            raise InvalidDraft("The installation draft is damaged.")
        return cls(path, answers, previous, results, installed)

    def save(self) -> None:
        """Commit the current draft while leaving the last saved version intact on failure."""
        data = {
            "version": VERSION,
            "answers": self.answers,
            "previous": self.previous,
            "results": self.results,
            "panel_installed": self.panel_installed,
        }
        atomic_write(self.path, json.dumps(data).encode(), 0o600)

    def review(self) -> None:
        """Reopen configuration questions with their previous answers as defaults."""
        self.previous.update(self.answers)
        self.answers = {
            key: self.answers[key] for key in ("action", "install_dir", "backup_source") if key in self.answers
        }
        self.results.clear()
        self.save()
