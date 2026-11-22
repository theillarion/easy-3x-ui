"""Download a temporary Xray binary from its official Linux release."""

import hashlib
import json
import platform
import re
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

RELEASE = "https://api.github.com/repos/XTLS/Xray-core/releases/latest"


def download_xray(directory: Path) -> str:
    """Check the archive digest and extract just the executable."""
    assets = {"x86_64": "Xray-linux-64.zip", "aarch64": "Xray-linux-arm64-v8a.zip"}
    name = assets.get(platform.machine())
    if platform.system() != "Linux" or not name:
        raise ValueError("Automatic Xray download supports Linux x86_64 and arm64.")
    request = Request(RELEASE, headers={"Accept": "application/vnd.github+json"})
    with urlopen(request, timeout=15) as response:  # noqa: S310
        release = json.load(response)
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            break
    else:
        raise ValueError("The official release has no Xray archive for this machine.")
    digest = asset.get("digest")
    url = asset.get("browser_download_url", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("The official release has no archive digest.")
    if not url.startswith("https://github.com/XTLS/Xray-core/releases/download/"):
        raise ValueError("The official release returned an unexpected download URL.")
    with urlopen(url, timeout=30) as response:  # noqa: S310
        archive = response.read(65_000_001)
    if len(archive) > 65_000_000 or hashlib.sha256(archive).hexdigest() != digest.removeprefix("sha256:"):
        raise ValueError("Xray archive size or SHA-256 does not match.")
    executable = directory / "xray"
    try:
        with zipfile.ZipFile(BytesIO(archive)) as batch:
            if batch.getinfo("xray").file_size > 64_000_000:
                raise ValueError("Xray binary is too large.")
            executable.write_bytes(batch.read("xray"))
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Could not extract Xray from the official archive.") from exc
    executable.chmod(0o700)
    return str(executable)
