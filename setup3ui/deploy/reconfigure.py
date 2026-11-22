"""Apply domain and inbound changes to a managed installation."""

import getpass
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from setup3ui.certificates.acme import issue_certificate
from setup3ui.certificates.renewal import install_renewal_timer
from setup3ui.deploy.caddy import render_caddy
from setup3ui.deploy.lifecycle import _panel_health
from setup3ui.deploy.managed import backup_installation, compose
from setup3ui.host.dns import check_dns
from setup3ui.host.files import atomic_write
from setup3ui.host.preflight import tcp_listeners
from setup3ui.panel.api import PanelAPI
from setup3ui.panel.inbounds import _cdn
from setup3ui.panel.restore import settings
from setup3ui.panel.tls import configure_panel_tls

TRANSPORT_KEYS = {"reality_sni", "reality_target", "direct_xhttp_path", "reality_address"}


def _inbound(api: PanelAPI, tag: str) -> dict[str, Any]:
    data = api.call("panel/api/inbounds/list")
    if not isinstance(data, list):
        raise ValueError("3x-ui did not return its inbounds.")
    matches = [entry for entry in data if isinstance(entry, dict) and entry.get("tag") == tag]
    if len(matches) != 1:
        raise ValueError(f"Expected one inbound with tag {tag}, got {len(matches)}.")
    inbound: dict[str, Any] = matches[0]
    print(f"Inbound {tag}: {inbound['remark']}")
    if not isinstance(inbound.get("id"), int) or inbound.get("protocol") != "vless":
        raise ValueError(f"Inbound {tag} is not a VLESS inbound with an ID.")
    if isinstance(inbound.get("streamSettings"), str):
        inbound["streamSettings"] = json.loads(inbound["streamSettings"])
    return inbound


def _login(directory: Path, config: Mapping[str, Any], auth: Mapping[str, Any], *, interactive: bool) -> PanelAPI:
    database = settings(directory / "db" / "x-ui.db")
    port = 443 if config["https"] == "caddy" else config["panel_port"]
    api = PanelAPI(config["panel_domain"], port, database.get("webBasePath") or "/")
    username, password = auth.get("admin_username"), auth.get("admin_password")
    if interactive and sys.stdin.isatty() and not auth:
        username = input("Current panel username: ").strip()
        password = getpass.getpass("Current panel password: ")
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        raise ValueError("Provide admin_username and admin_password for inbound changes.")
    code = ""
    if api.needs_two_factor():
        if not sys.stdin.isatty():
            raise ValueError("Inbound changes on a panel with 2FA need a terminal.")
        code = getpass.getpass("Current 2FA code: ")
    api.login(username, password, code)
    return api


def _edit_reality(inbound: dict[str, Any], desired: Mapping[str, Any], *, is_xhttp: bool) -> None:
    stream = inbound["streamSettings"]
    reality = stream.get("realitySettings")
    expected = "xhttp" if is_xhttp else "tcp"
    if stream.get("network") not in {expected, "raw" if not is_xhttp else expected} or not isinstance(reality, dict):
        raise ValueError(f"{inbound['tag']} is not the expected REALITY inbound.")
    names = desired["reality_sni"]
    reality.update(serverNames=names, target=desired.get("reality_target", f"{names[0]}:443"))
    reality.setdefault("settings", {}).update(serverName=names[0])
    if is_xhttp:
        stream["xhttpSettings"]["path"] = desired["direct_xhttp_path"]
    elif desired["https"] == "caddy":
        proxies = stream.get("externalProxy")
        if isinstance(proxies, list) and len(proxies) == 1:
            proxies[0]["dest"] = desired["reality_address"]


def _sync_cdn(inbound: dict[str, Any], backend: Mapping[str, Any]) -> None:
    stream = inbound["streamSettings"]
    if stream.get("network") != "xhttp" or stream.get("security") != "none":
        raise ValueError(f"{inbound['tag']} is not an XHTTP CDN inbound.")
    proxies = stream.get("externalProxy")
    if not isinstance(proxies, list) or len(proxies) != 1:
        raise ValueError(f"{inbound['tag']} must have one external proxy.")
    stream["xhttpSettings"]["path"] = backend["path"]
    proxies[0]["dest"] = backend["edge"]
    if backend.get("remark"):
        inbound["remark"] = proxies[0]["remark"] = backend["remark"]


def _reload_caddy(directory: Path) -> None:
    compose(directory, "exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile")


def apply_config(
    directory: Path,
    current: Mapping[str, Any],
    desired: Mapping[str, Any],
    auth: Mapping[str, Any],
    *,
    interactive: bool = True,
) -> None:
    """Apply supported config changes with rollback from a local snapshot."""
    import yaml

    fixed = {
        "https",
        "install_dir",
        "panel_port",
        "subscription_port",
        "reality_port",
        "reality_backend_port",
        "direct_xhttp_port",
        "reality",
        "direct_xhttp",
        "docker_user",
    }
    for key in fixed:
        if current.get(key) != desired.get(key):
            raise ValueError(f"Changing {key} on an existing install is unavailable.")
    previous_cdns = {item["port"]: item for item in current["cdn"]}
    next_cdns = {item["port"]: item for item in desired["cdn"]}
    if previous_cdns.keys() - next_cdns.keys():
        raise ValueError("Keep existing CDN ports when applying a config.")
    added = next_cdns.keys() - previous_cdns.keys()
    for port, address, owners in tcp_listeners():
        if port in added:
            raise ValueError(f"TCP port {port} is in use at {address} by {', '.join(owners) or 'unknown owner'}.")
    changed = {name for name in desired.keys() | current.keys() if desired.get(name) != current.get(name)}
    auth_required = bool(changed & TRANSPORT_KEYS and (current["reality"] or current["direct_xhttp"]))
    cdn_changes = []
    for port in sorted(previous_cdns):
        previous, backend = previous_cdns[port], next_cdns[port]
        if previous["provider"] != backend["provider"]:
            raise ValueError(f"Provider of the existing CDN on {port} cannot change.")
        if any(previous.get(key) != backend.get(key) for key in ("path", "edge", "remark")):
            cdn_changes.append(backend)
    changes: list[dict[str, Any]] = []
    api = None
    if auth_required or cdn_changes or added:
        api = _login(directory, current, auth, interactive=interactive)
        for name, needed, xhttp in (
            ("reality_tag", current["reality"] and bool(changed & (TRANSPORT_KEYS - {"direct_xhttp_path"})), False),
            (
                "direct_xhttp_tag",
                current["direct_xhttp"] and bool(changed & (TRANSPORT_KEYS - {"reality_address"})),
                True,
            ),
        ):
            if needed:
                tag = desired.get(name)
                if not tag:
                    raise ValueError(f"Set {name} to the existing inbound tag.")
                inbound = _inbound(api, tag)
                _edit_reality(inbound, desired, is_xhttp=xhttp)
                changes.append(inbound)
        for backend in cdn_changes:
            if not backend.get("tag"):
                raise ValueError(f"Set the tag for the existing CDN on {backend['port']}.")
            inbound = _inbound(api, backend["tag"])
            if inbound["port"] != backend["port"]:
                raise ValueError(f"Inbound {backend['tag']} does not use port {backend['port']}.")
            _sync_cdn(inbound, backend)
            changes.append(inbound)

    panel, subscription = desired["panel_domain"], desired["subscription_domain"]
    domains = [panel, subscription, *(item["origin"] for item in next_cdns.values())]
    check_dns(domains)
    caddy = desired["https"] == "caddy"
    web_changed = bool(changed & {"panel_domain", "subscription_domain"})
    certificate_changed = not caddy and (web_changed or "email" in changed)
    caddyfile = directory / "caddy-config" / "Caddyfile"
    new_caddyfile = render_caddy(desired).encode() if caddy else b""
    caddy_changed = caddy and caddyfile.read_bytes() != new_caddyfile
    backup = backup_installation(directory)
    db_touched = False
    caddy_touched = False
    try:
        if certificate_changed:
            issue_certificate(directory, panel, subscription, desired["email"])
        if caddy_changed:
            new_config = caddyfile.with_name("candidate.Caddyfile")
            try:
                atomic_write(new_config, new_caddyfile, 0o644)
                compose(
                    directory,
                    "exec",
                    "-T",
                    "caddy",
                    "caddy",
                    "validate",
                    "--adapter",
                    "caddyfile",
                    "--config",
                    "/etc/caddy/candidate.Caddyfile",
                )
            finally:
                new_config.unlink(missing_ok=True)
        if api:
            for inbound in changes:
                db_touched = True
                api.call(f"panel/api/inbounds/update/{inbound['id']}", inbound)
            for port in sorted(added):
                db_touched = True
                api.call("panel/api/inbounds/add", _cdn(next_cdns[port], desired))
            if db_touched:
                api.call("panel/api/server/restartXrayService", {})
        if web_changed:
            compose(directory, "stop", "panel")
            db_touched = True
            configure_panel_tls(
                directory,
                panel,
                desired["panel_port"],
                subscription,
                desired["subscription_port"],
                tls_at_panel=not caddy,
            )
            compose(directory, "up", "-d", "--no-deps", "panel")
        if caddy_changed:
            atomic_write(caddyfile, new_caddyfile, 0o644)
            caddy_touched = True
            _reload_caddy(directory)
        _panel_health(directory, desired)
        if certificate_changed:
            install_renewal_timer(directory, panel)
        atomic_write(directory / "config.yml", yaml.safe_dump(dict(desired), sort_keys=False).encode(), 0o600)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt):
        try:
            if caddy_touched:
                atomic_write(caddyfile, (backup / "Caddyfile").read_bytes(), 0o644)
                _reload_caddy(directory)
            if db_touched:
                compose(directory, "stop", "panel")
                database = directory / "db" / "x-ui.db"
                for suffix in ("-wal", "-shm"):
                    database.with_name(database.name + suffix).unlink(missing_ok=True)
                atomic_write(database, (backup / "x-ui.db").read_bytes(), 0o600)
                compose(directory, "up", "-d", "--no-deps", "panel")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"Could not roll back. Backup: {backup}.") from exc
        raise
    print(f"Settings applied. Backup: {backup}. Refresh client subscriptions if their settings changed.")
