"""Discover and verify REALITY TLS targets from the installation host."""

import csv
import hashlib
import json
import platform
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple

from setup3ui.config import DOMAIN, SNI_RUN_FIELDS, scanner_subnet, validate_config

SNI_CHOICES = (
    "www.cloudflare.com",
    "www.microsoft.com",
    "www.apple.com",
    "www.nvidia.com",
    "www.amazon.com",
    "www.samsung.com",
    "www.amd.com",
    "www.intel.com",
    "www.sony.com",
    "dl.google.com",
)
SCANNER_HIT = re.compile(
    r'^time=\S+ level=INFO msg="Connected to target" feasible=true .*? cert-domain=([a-zA-Z0-9.-]+) ',
)
SCANNER_RELEASE = "https://github.com/XTLS/RealiTLScanner/releases/download/v0.2.3"
SCANNER_ASSETS = {
    "x86_64": ("amd64", "a55595446de9f1c2e6c5c3cd766a7320a11115947df48f101749bb62c8055592"),
    "aarch64": ("arm64", "27bdd3e53d4391c66c8df3391d3c3fb5eb2dc356125f2fb33ac58fcaaf8f88b3"),
}


class Probe(NamedTuple):
    name: str
    milliseconds: int | None
    error: str = ""


def probe(name: str, target: str) -> Probe:
    """Check the exact SNI and target pair with a five-second deadline."""
    started = time.monotonic()
    try:
        result = subprocess.run(
            [
                "openssl", "s_client", "-connect", target, "-servername", name,
                "-verify_hostname", name, "-verify_return_error",
                "-tls1_3", "-groups", "X25519", "-alpn", "h2",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Probe(name, None, "TLS check timed out")
    except OSError as exc:
        return Probe(name, None, f"Could not run OpenSSL ({exc.strerror})")
    if result.returncode:
        return Probe(name, None, "TLS 1.3, X25519 or certificate check failed")
    if "ALPN protocol: h2" not in result.stdout.splitlines():
        return Probe(name, None, "HTTP/2 was not negotiated")
    return Probe(name, round((time.monotonic() - started) * 1000))


def _ping(name: str) -> str:
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", "2", "--", name],
            capture_output=True, text=True, errors="replace", timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    for line in result.stdout.splitlines():
        if "time=" in line:
            return line.partition("time=")[2].strip()
    return "unavailable"


def report_probes(probes: Iterable[Probe]) -> list[str]:
    """Print probe results and return compatible names in latency order."""
    results = sorted(probes, key=lambda item: (item.milliseconds is None, item.milliseconds, item.name))
    passed = []
    for item in results:
        if item.milliseconds is None:
            print(f"{item.name}: {item.error}.")
            continue
        passed.append(item.name)
        print(
            f"{item.name}: TLS 1.3, HTTP/2, X25519 and certificate OK, "
            f"TLS {item.milliseconds} ms, ping {_ping(item.name)}."
        )
    return passed


def blocked_names(config: Mapping[str, Any]) -> set[str]:
    """Return Caddy site names that cannot also route TCP REALITY."""
    if config["https"] != "caddy" or not config["reality"]:
        return set()
    return {
        config["panel_domain"], config["subscription_domain"],
        *(item["origin"] for item in config["cdn"]),
    }


def _ip_json(*args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ip", "-j", "-4", *args], capture_output=True, text=True, timeout=5, check=True,
    )
    return json.loads(result.stdout)


def primary_subnet() -> IPv4Network | None:
    """Return the IPv4 subnet of the preferred default-route interface."""
    try:
        routes = _ip_json("route", "show", "default")
        route = min((route for route in routes if route.get("dev")), key=lambda item: item.get("metric", 0), default=None)
        if route is None:
            return None
        devices = _ip_json("addr", "show", "dev", route["dev"], "scope", "global")
        addresses = [address for device in devices for address in device["addr_info"]]
        address = next(
            (item for item in addresses if item["local"] == route.get("prefsrc")),
            addresses[0] if addresses else None,
        )
        if address is not None and address["prefixlen"] < 32:
            return IPv4Interface(f"{address['local']}/{address['prefixlen']}").network
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        pass
    return None


def _download_scanner(directory: Path) -> Path:
    asset = SCANNER_ASSETS.get(platform.machine()) if platform.system() == "Linux" else None
    if asset is None:
        raise ValueError("Reality Scanner supports Linux amd64 and arm64.")
    architecture, checksum = asset
    binary = directory / f"RealiTLScanner-linux-{architecture}"
    try:
        subprocess.run(
            [
                "curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
                "--connect-timeout", "10", "--max-time", "60",
                "--output", str(binary), f"{SCANNER_RELEASE}/{binary.name}",
            ],
            check=True, timeout=65,
        )
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != checksum:
            raise ValueError("Reality Scanner SHA-256 checksum mismatch.")
        binary.chmod(0o700)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Could not download Reality Scanner.") from exc
    return binary


def _scanner_names(output: Path, log: Path) -> list[str]:
    names: list[str] = []
    if output.is_file():
        with output.open(encoding="utf-8", errors="replace") as stream:
            # A killed scanner can leave an incomplete final record.
            rows = csv.DictReader(line for line in stream if line.endswith("\n"))
            names.extend(row.get("CERT_DOMAIN") or "" for row in rows)
    # The upstream CSV writer can exit before flushing its last result.
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match = SCANNER_HIT.match(line)
            if match and line.endswith("\n"):
                names.append(match[1])
    normalized = (name.strip().rstrip(".").lower() for name in names)
    return list(dict.fromkeys(name for name in normalized if DOMAIN.fullmatch(name)))


def scanner_names(cidr: str | None, timeout: int) -> list[str]:
    """Run the pinned scanner and read any complete results produced by its deadline."""
    network = scanner_subnet(cidr) if cidr else primary_subnet()
    if network is None:
        raise ValueError("Could not determine the VPS subnet. Set reality_sni_scan_cidr.")
    print(f"Scanning {network} on TCP/443 with a {timeout}s deadline.", flush=True)
    with TemporaryDirectory(prefix="setup3ui-reality-") as stage:
        directory = Path(stage)
        binary = _download_scanner(directory)
        output = directory / "result.csv"
        log = directory / "scanner.log"
        try:
            with log.open("wb") as stream:
                subprocess.run(
                    [
                        str(binary), "-addr", str(network), "-port", "443", "-out", str(output),
                        "-thread", "8", "-timeout", "5",
                    ],
                    cwd=directory, stdin=subprocess.DEVNULL, stdout=stream,
                    stderr=subprocess.DEVNULL, check=True, timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            print("Reality Scanner reached its deadline. Checking completed results.")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("Reality Scanner failed.") from exc
        return _scanner_names(output, log)


def scan(config: Mapping[str, Any], mode: str, cidr: str | None = None, timeout: int = 30) -> list[str]:
    """Run the selected discovery and rank compatible domains by TLS latency."""
    names = SNI_CHOICES if mode == "top10" else scanner_names(cidr, timeout)
    excluded = blocked_names(config)
    with ThreadPoolExecutor(max_workers=8) as pool:
        candidates = (name for name in names if name not in excluded)
        results = list(pool.map(lambda name: probe(name, f"{name}:443"), candidates))
    return report_probes(results)


def resolve_sni(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve one-time YAML options into durable SNI and target settings."""
    if "reality_sni_auto" not in config:
        return config
    resolved = {key: value for key, value in config.items() if key not in SNI_RUN_FIELDS}
    if not config["reality_sni_auto"]:
        names = config["reality_sni"]
        target = config.get("reality_target", f"{names[0]}:443")
        if len(report_probes(probe(name, target) for name in names)) != len(names):
            raise ValueError("A configured REALITY SNI failed its TLS check.")
        resolved["reality_target"] = target
        return resolved

    try:
        names = scan(
            config, config["reality_sni_scan"], config.get("reality_sni_scan_cidr"),
            config.get("reality_sni_scan_timeout", 30),
        )
    except (OSError, ValueError) as exc:
        print(f"REALITY scan failed: {exc}")
        names = []
    if names:
        name = names[0]
    else:
        print("No suitable candidate. Checking fallback names from reality_sni.")
        excluded = blocked_names(config)
        for name in config["reality_sni"]:
            if name in excluded:
                print(f"{name}: conflicts with a Caddy site name.")
                continue
            if report_probes([probe(name, f"{name}:443")]):
                break
        else:
            raise ValueError("No fallback REALITY SNI in reality_sni passed the checks.")
    resolved.update(reality_sni=[name], reality_target=f"{name}:443")
    return validate_config(resolved)
