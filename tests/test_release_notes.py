"""Check that a release contains only the requested changelog section."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "release_notes.py"


class ReleaseNotesTests(TestCase):
    def run_script(self, changelog: str, tag: str) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as directory:
            (Path(directory) / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), tag],
                cwd=directory,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_only_selected_version_is_used(self) -> None:
        changelog = (
            "# Changelog\n\n## [Unreleased]\n\n- Новое\n"
            "\n## [0.1.1] - 2026-11-22\n\n### Исправлено\n\n- Исправление\n"
            "\n## [0.1.0] - 2026-11-21\n\n- Первая версия\n"
        )
        result = self.run_script(changelog, "v0.1.1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "### Исправлено\n\n- Исправление\n")

    def test_missing_release_does_not_use_unreleased_notes(self) -> None:
        result = self.run_script("## [Unreleased]\n\n- Новое\n", "v0.1.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("[0.1.0]", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_tag_is_rejected(self) -> None:
        result = self.run_script("", "v0.1.0-rc1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("vX.Y.Z", result.stderr)
