"""Ask for settings for an interactive installation."""

import os
from collections.abc import Mapping
from typing import Any

from setup3ui.config import DOMAIN, scanner_subnet, validate_config
from setup3ui.host.reality import blocked_names, primary_subnet, probe, report_probes, scan

EXAMPLE = "/media/hls/9636f214/index.m3u8"


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


def _pick_name(names: list[str]) -> str:
    for index, name in enumerate(names, 1):
        print(f"{index}. {name}")
    while True:
        choice = _value("REALITY SNI number, or m for manual entry", "1").lower()
        if choice == "m":
            return ""
        if choice.isascii() and choice.isdecimal() and 1 <= int(choice) <= len(names):
            return names[int(choice) - 1]
        print("Choose a number from the list or m.")


def _scan_automatically(config: Mapping[str, Any]) -> str:
    while True:
        mode = _value("REALITY scan type (top10, scanner, or manual)", "top10").lower()
        if mode == "manual":
            return ""
        if mode not in {"scanner", "top10"}:
            print("Choose top10, scanner, or manual.")
            continue
        try:
            if mode == "scanner":
                default = primary_subnet()
                value = _value("IPv4 subnet to scan, or top10", str(default) if default else "top10")
                if value.lower() == "top10":
                    mode = "top10"
                else:
                    network = scanner_subnet(value)
                    timeout = _port("Reality Scanner timeout in seconds", 30)
                    if timeout < 1:
                        raise ValueError("The scanner timeout must be positive.")
                    names = scan(config, mode, str(network), timeout)
            if mode == "top10":
                names = scan(config, mode)
        except (OSError, ValueError) as exc:
            print(f"REALITY scan failed: {exc}")
            continue
        if names:
            return _pick_name(names)
        print("No suitable REALITY SNI found.")


def ask_reality_sni(config: Mapping[str, Any]) -> dict[str, Any]:
    """Choose an SNI and its corresponding REALITY target in the terminal."""
    if _yes("Find REALITY SNI automatically?"):
        name = _scan_automatically(config)
        if name:
            return {"reality_sni": [name], "reality_target": f"{name}:443"}
    original = config["reality_sni"][0]
    default = original
    excluded = blocked_names(config)
    alternatives_checked = False
    while True:
        name = _value("REALITY SNI", default).lower()
        if not DOMAIN.fullmatch(name) or name in excluded:
            print("Enter a domain name that differs from the Caddy site names.")
            continue
        target = config.get("reality_target", f"{name}:443") if name == original else f"{name}:443"
        result = probe(name, target)
        if report_probes([result]):
            return {"reality_sni": [name], "reality_target": target}
        if not alternatives_checked:
            alternatives_checked = True
            names = scan(config, "top10")
            if names:
                selected = _pick_name(names)
                if selected:
                    return {"reality_sni": [selected], "reality_target": f"{selected}:443"}
        if _yes("Keep this SNI as unverified?"):
            print(f"{name}: saved as unverified.")
            return {"reality_sni": [name], "reality_target": target}
        default = name


def ask_config(
    defaults: Mapping[str, Any] | None = None, *, choose_sni: bool = True,
) -> dict[str, Any]:
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
    for field in ("reality_sni", "reality_target"):
        if field in defaults:
            data[field] = defaults[field]
    if choose_sni and (data["reality"] or data["direct_xhttp"]):
        data = validate_config(data, pending_sni=True)
        data.update(ask_reality_sni(data))
    return data
