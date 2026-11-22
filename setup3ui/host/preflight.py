"""Check local resources before starting the panel."""

import os
import re
import stat
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path


def tcp_listeners() -> Iterator[tuple[int, str, list[str]]]:
    """Yield the port, address, and visible owners of each TCP listener."""
    result = subprocess.run(("ss", "-H", "-ltnp"), capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise ValueError(f"Could not list TCP listeners with ss: {detail}")
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            raise ValueError("Could not read the TCP listener list from ss.")
        address = fields[3]
        port = address.rsplit(":", 1)[-1]
        if not port.isdecimal():
            raise ValueError("Could not read a TCP port from ss.")
        processes = re.findall(r'\("([^"]+)",pid=(\d+)', fields[5] if len(fields) == 6 else "")
        owners = sorted({f"{name} (PID {pid})" for name, pid in processes})
        yield int(port), address, owners


def check_tun(path: Path = Path("/dev/net/tun")) -> None:
    """Check that the Linux TUN device can be opened."""
    try:
        device = path.stat()
        device_number = os.major(device.st_rdev), os.minor(device.st_rdev)
        if not stat.S_ISCHR(device.st_mode) or device_number != (10, 200):
            raise ValueError(f"{path} must be the Linux TUN character device (10:200).")
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        os.close(fd)
    except FileNotFoundError as exc:
        raise ValueError(f"TUN device {path} is missing.") from exc
    except OSError as exc:
        raise ValueError(f"Could not open TUN device {path}: {exc.strerror}.") from exc


def check_host(ports: Mapping[str, int]) -> None:
    """Report local port conflicts and TUN errors before installation."""
    if os.geteuid() != 0:
        raise ValueError("Run preflight with sudo so it can see which processes own the ports.")
    required = {port: label for label, port in ports.items()}
    problems = []
    for port, address, owners in tcp_listeners():
        if port in required:
            owner = ", ".join(owners) if owners else "owner unavailable"
            problems.append(f"TCP port {port} for {required[port]} is in use at {address} by {owner}.")
    try:
        check_tun()
    except ValueError as exc:
        problems.append(str(exc))
    if problems:
        raise ValueError("Local preflight failed:\n" + "\n".join(problems))
