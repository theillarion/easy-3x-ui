"""Ask for settings for an interactive installation."""

import os
import shutil
import subprocess
import time
from collections.abc import Iterable, Mapping
from typing import Any

EXAMPLE = "/media/hls/9636f214/index.m3u8"
SNI_CHOICES = ("www.cloudflare.com", "www.microsoft.com")


def _value(label: str, default: str) -> str:
    return input(f"{label} [{default}]: ").strip() or default


def _yes(label: str, default: bool = False) -> bool:
    reply = input(f"{label} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return reply in {"y", "yes"} if reply else default


def _port(label: str, default: int) -> int:
    result = _value(label, str(default))
    if not result.isdecimal():
        raise ValueError(f"{label} must be a port number.")
    return int(result)


def _sni_latency(names: Iterable[str]) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        print("Install openssl to check REALITY targets.")
        return
    results = []
    failed = []
    for host in dict.fromkeys(names):
        begin = time.monotonic()
        try:
            reply = subprocess.run(
                [
                    openssl,
                    "s_client",
                    "-connect",
                    f"{host}:443",
                    "-servername",
                    host,
                    "-verify_hostname",
                    host,
                    "-verify_return_error",
                    "-tls1_3",
                    "-groups",
                    "X25519",
                    "-alpn",
                    "h2",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=7,
            )
            if reply.returncode or "ALPN protocol: h2" not in reply.stdout:
                failed.append(host)
            else:
                results.append((round((time.monotonic() - begin) * 1000), host))
        except (OSError, subprocess.TimeoutExpired):
            failed.append(host)
    ping = shutil.which("ping")
    for latency, host in sorted(results):
        ping_time = "unavailable"
        if ping:
            try:
                reply = subprocess.run(
                    [ping, "-n", "-c", "1", "-W", "2", "--", host],
                    timeout=3,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                for line in reply.stdout.splitlines():
                    if "time=" in line:
                        ping_time = line.partition("time=")[2].strip()
                        break
            except (OSError, subprocess.TimeoutExpired):
                pass
        print(f"{host}: TLS 1.3, HTTP/2, X25519 and certificate OK, TLS {latency} ms, ping {ping_time}.")
    for host in failed:
        print(f"{host}: TLS 1.3, HTTP/2, X25519 or certificate check failed.")


def ask_config(defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Ask only for settings used by the selected options."""
    defaults = defaults or {}
    https = _value("HTTPS with certbot or caddy", defaults.get("https", "certbot")).lower()
    if https not in {"certbot", "caddy"}:
        raise ValueError("Choose certbot or caddy.")
    hint = defaults.get("panel_domain", "").partition(".")[2]
    domain = _value("Main domain", hint) if hint else input("Main domain: ").strip()
    data: dict[str, Any] = {
        "https": https,
        "domain": domain,
        "panel_domain": _value("Panel domain", defaults.get("panel_domain", f"master.{domain}")),
        "subscription_domain": _value("Subscription domain", defaults.get("subscription_domain", f"gateway.{domain}")),
        "email": _value("ACME contact email", defaults["email"])
        if defaults.get("email")
        else input("ACME contact email: ").strip(),
        "panel_port": _port("Panel port", defaults.get("panel_port", 2053)),
        "subscription_port": _port("Subscription port", defaults.get("subscription_port", 2096)),
    }
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or sudo_user == "root":
        data["docker_user"] = input("Linux user to add to the docker group: ").strip()
    data["reality"] = _yes("Use TCP REALITY", defaults.get("reality", False))
    data["direct_xhttp"] = _yes("Use direct XHTTP REALITY", defaults.get("direct_xhttp", False))
    if data["reality"]:
        field = "reality_backend_port" if https == "caddy" else "reality_port"
        data[field] = _port(
            "TCP REALITY backend port" if https == "caddy" else "TCP REALITY port",
            defaults.get(field, 8444 if https == "caddy" else 443),
        )
    if data["direct_xhttp"]:
        data["direct_xhttp_port"] = _port("Direct XHTTP port", defaults.get("direct_xhttp_port", 8443))
        data["direct_xhttp_path"] = _value(
            f"Direct XHTTP path, or generate (example {EXAMPLE})", defaults.get("direct_xhttp_path", "/")
        )
    if data["reality"] or data["direct_xhttp"]:
        name = _value("REALITY SNI", defaults.get("reality_sni", SNI_CHOICES)[0]).lower()
        if _yes("Check the SNI and show other choices"):
            _sni_latency((name, *SNI_CHOICES))
            name = _value("REALITY SNI to use", name).lower()
        data["reality_sni"] = [name]
    if https == "caddy":
        cdns: list[dict[str, Any]] = list(defaults.get("cdn", ()))
        while _yes("Add a CDN XHTTP backend" if not cdns else "Add another CDN XHTTP backend"):
            index = len(cdns)
            cdns.append(
                {
                    "provider": _value("Provider (selectel or yandex)", "selectel").lower(),
                    "edge": _value("Public CDN domain", f"edge{index + 1 if index else ''}.{domain}"),
                    "origin": _value("Origin domain", f"node.{domain}"),
                    "port": _port("Local XHTTP port", 8080 + index),
                    "path": _value("XHTTP path, or generate", EXAMPLE),
                }
            )
        data["cdn"] = cdns
    if defaults.get("reality_target"):
        data["reality_target"] = defaults["reality_target"]
    return data
