"""Check that the Certbot timer launcher can run on its own."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from setup3ui.certificates.renewal import _install_renewal_runtime


class RenewalRuntimeTest(unittest.TestCase):
    def test_launcher_imports_only_the_copied_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "runtime"
            with patch("setup3ui.certificates.renewal.check_root_owned_directory"):
                _install_renewal_runtime(directory)
            result = subprocess.run(
                [sys.executable, "-E", "-s", str(directory / "certificate_renewal.py"), "--help"],
                cwd=temporary,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("install_dir", result.stdout)
