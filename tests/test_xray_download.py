"""Verify the diagnostic Xray download using in-memory release fixtures."""

import hashlib
import json
import stat
import zipfile
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from setup3ui.diagnostics import xray


class XrayDownloadTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("xray", b"fixture executable")
            archive.writestr("README.md", b"fixture documentation")
        self.payload = stream.getvalue()
        self.asset = {
            "name": "Xray-linux-64.zip",
            "digest": f"sha256:{hashlib.sha256(self.payload).hexdigest()}",
            "browser_download_url": "https://github.com/XTLS/Xray-core/releases/download/test/Xray-linux-64.zip",
        }
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.machine = stack.enter_context(
            patch.object(xray.platform, "machine", return_value="x86_64")
        )
        self.system = stack.enter_context(
            patch.object(xray.platform, "system", return_value="Linux")
        )
        self.request = stack.enter_context(patch.object(xray, "urlopen"))

    def test_good_archive_extracts_only_xray(self) -> None:
        self.request.side_effect = [
            BytesIO(json.dumps({"assets": [self.asset]}).encode()),
            BytesIO(self.payload),
        ]
        executable = Path(xray.download_xray(self.root))
        self.assertEqual(executable.read_bytes(), b"fixture executable")
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o700)
        self.assertEqual(list(self.root.iterdir()), [executable])
        self.assertEqual(
            self.request.call_args.args[0], self.asset["browser_download_url"]
        )

    def test_rejects_bad_metadata_before_downloading_archive(self) -> None:
        cases = [
            (self.asset | {"name": "Xray-windows-64.zip"}, "no Xray archive"),
            (self.asset | {"digest": None}, "no archive digest"),
            (
                self.asset | {"browser_download_url": "https://example.org/xray"},
                "unexpected download URL",
            ),
        ]
        for asset, error in cases:
            with self.subTest(error=error):
                self.request.reset_mock()
                self.request.return_value = BytesIO(
                    json.dumps({"assets": [asset]}).encode()
                )
                with self.assertRaisesRegex(ValueError, error):
                    xray.download_xray(self.root)
                self.request.assert_called_once()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_rejects_wrong_digest_and_corrupt_zip(self) -> None:
        broken = b"invalid zip content"
        for asset, error in (
            (self.asset, "SHA-256 does not match"),
            (
                self.asset | {"digest": f"sha256:{hashlib.sha256(broken).hexdigest()}"},
                "Could not extract Xray",
            ),
        ):
            with self.subTest(error=error):
                self.request.side_effect = [
                    BytesIO(json.dumps({"assets": [asset]}).encode()),
                    BytesIO(broken),
                ]
                with self.assertRaisesRegex(ValueError, error):
                    xray.download_xray(self.root)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_unsupported_architecture_makes_no_request(self) -> None:
        self.machine.return_value = "riscv64"
        with self.assertRaisesRegex(ValueError, "supports Linux x86_64 and arm64"):
            xray.download_xray(self.root)
        self.request.assert_not_called()
