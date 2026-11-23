"""Load and validate the installer YAML configuration."""

from __future__ import annotations

import re
import secrets
import sys
from collections.abc import Hashable, Iterable, Mapping
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yaml.nodes import MappingNode


DEFAULTS = {
    "https": "certbot",
    "install_dir": "/opt/3x-ui",
    "panel_port": 2053,
    "subscription_port": 2096,
    "reality": False,
    "direct_xhttp": False,
    "reality_port": 443,
    "reality_backend_port": 8444,
    "direct_xhttp_port": 8443,
    "direct_xhttp_path": "/",
}
NFTGUARD_DEFAULTS = {
    "enabled": True,
    "dry_run": False,
    "enable_vk_output": False,
    "allow_cidrs": "",
    "allow_cidrs6": "",
}
OPTIONAL_FIELDS = {
    "nftguard",
    "domain",
    "panel_domain",
    "subscription_domain",
    "email",
    "origin_domain",
    "docker_user",
    "cdn",
    "admin_username",
    "admin_password",
    "reality_sni",
    "reality_address",
    "reality_target",
    "reality_tag",
    "direct_xhttp_tag",
    "inbound_templates",
}
INBOUND_TEMPLATES = {"reality", "direct_xhttp", "selectel", "yandex"}
CDN_FIELDS = {"provider", "edge", "path", "port", "remark", "origin", "tag"}
AUTH_FIELDS = {"admin_username", "admin_password"}
RUN_FIELDS = {"action", "backup_file", "confirm"}
SNI_RUN_FIELDS = {
    "reality_sni_auto", "reality_sni_scan", "reality_sni_scan_cidr",
    "reality_sni_scan_timeout",
}
XHTTP_EXAMPLE = "/media/hls/9636f214/index.m3u8"
DOMAIN = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.ASCII | re.IGNORECASE,
)


def _load_yaml(path: Path) -> object:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        if exc.name != "yaml":
            raise
        raise ValueError("Install python3-yaml to read config files.") from exc

    class ConfigLoader(yaml.SafeLoader):
        def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, Any]:
            seen: set[str] = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                line = key_node.start_mark.line + 1
                if not isinstance(key, str):
                    raise ValueError(f"Expected a field name at line {line}.")
                if key in seen:
                    raise ValueError(f"Duplicate field {key!r} at line {line}.")
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=ConfigLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}" if mark else ""
        raise ValueError(f"Invalid YAML{location}.") from None


def read_config(path: Path, force: bool, *, installed: bool = False) -> dict[str, Any]:
    """Load setup YAML and preserve the service selection of older installations."""
    raw = _load_yaml(path)
    if installed and isinstance(raw, dict):
        raw.setdefault("nftguard", {"enabled": False})
    return validate_config(raw, force)


def validate_config(raw: object, force: bool = False, *, pending_sni: bool = False) -> dict[str, Any]:
    """Apply defaults and validate the loaded configuration."""
    if not isinstance(raw, dict):
        raise ValueError("The YAML config must be a mapping.")
    _run_options(raw)
    source = {key: value for key, value in raw.items() if key not in RUN_FIELDS}
    config = DEFAULTS | _clean_fields(
        source, DEFAULTS.keys() | OPTIONAL_FIELDS | SNI_RUN_FIELDS, "config", force,
    )
    config.setdefault("cdn", [])
    config["nftguard"] = _nftguard_options(config.get("nftguard", {}), force)
    if config["https"] not in ("certbot", "caddy"):
        raise ValueError("https must be certbot or caddy.")
    if not isinstance(config["install_dir"], str) or not Path(config["install_dir"]).is_absolute():
        raise ValueError("install_dir must be an absolute path.")
    if "docker_user" in config:
        user = config["docker_user"]
        validate_docker_user(user)
    for field in AUTH_FIELDS | {"reality_tag", "direct_xhttp_tag"}:
        if field in config:
            value = config[field]
            if not isinstance(value, str) or not value.strip() or any(c in value for c in "\0\r\n"):
                raise ValueError(f"{field} must be a non-empty string.")
    for field in ("reality", "direct_xhttp"):
        if type(config[field]) is not bool:
            raise ValueError(f"{field} must be true or false.")
    if "inbound_templates" in config:
        templates = config["inbound_templates"]
        if not isinstance(templates, dict):
            raise ValueError("inbound_templates must be a mapping.")
        templates = _clean_fields(templates, INBOUND_TEMPLATES, "inbound_templates", force)
        for name, path in templates.items():
            if not isinstance(path, str) or "\0" in path or not Path(path).is_absolute():
                raise ValueError(f"inbound_templates.{name} must be an absolute path.")
        config["inbound_templates"] = templates
    if "domain" in config:
        domain = _hostname(config["domain"], "domain")
        config.setdefault("panel_domain", f"master.{domain}")
        config.setdefault("subscription_domain", f"gateway.{domain}")
        config.setdefault("origin_domain", f"node.{domain}")
    for field in ("panel_domain", "subscription_domain"):
        config[field] = _hostname(config.get(field), field)
    if config["panel_domain"] == config["subscription_domain"]:
        raise ValueError("Panel and subscription domains must be different.")
    validate_email(config.get("email"))
    if not isinstance(config["cdn"], list):
        raise ValueError("cdn must be a list.")
    if config["cdn"] and config["https"] != "caddy":
        raise ValueError("CDN backends require Caddy.")
    config["cdn"] = _read_cdns(config, force)
    _validate_sni_options(config)
    names = config.get("reality_sni", ["www.cloudflare.com"])
    if not isinstance(names, list) or not names:
        raise ValueError("reality_sni must be a non-empty list of domain names.")
    sni_names = list(dict.fromkeys(_hostname(name, "reality_sni") for name in names))
    config["reality_sni"] = sni_names
    if not pending_sni and config.get("reality_sni_auto") is not True and config["https"] == "caddy" and config["reality"]:
        web_names = {config["panel_domain"], config["subscription_domain"]}
        web_names.update(backend["origin"] for backend in config["cdn"])
        if web_names.intersection(sni_names):
            raise ValueError("REALITY SNI names must differ from the Caddy site names.")
    path = config["direct_xhttp_path"]
    config["direct_xhttp_path"] = xhttp_path(path, "direct_xhttp_path", allow_root=True)
    if "reality_target" in config:
        target = config["reality_target"]
        if not isinstance(target, str) or ":" not in target:
            raise ValueError("reality_target must be a domain and port.")
        host, _, port = target.rpartition(":")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("reality_target must use a valid port.")
        config["reality_target"] = f"{_hostname(host, 'reality_target')}:{port}"
    if config["reality"] and config["https"] == "caddy":
        config["reality_address"] = _hostname(config.get("reality_address", config.get("domain")), "reality_address")
    port_plan(config)
    return config


def nftguard_allowlist(value: object, version: int) -> str:
    """Normalize a comma-separated list of addresses or CIDRs of one family."""
    field = "nftguard.allow_cidrs" if version == 4 else "nftguard.allow_cidrs6"
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a comma-separated string.")
    networks = []
    for item in re.split(r"[ ,]+", value.strip()):
        if not item:
            continue
        try:
            network = ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"{field} contains an invalid IP address or CIDR: {item}.") from exc
        if network.version != version or "%" in item:
            raise ValueError(f"{field} must contain IPv{version} addresses or CIDRs without zone IDs.")
        networks.append(str(network))
    return ",".join(dict.fromkeys(networks))


def _nftguard_options(raw: object, force: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("nftguard must be a mapping.")
    options = NFTGUARD_DEFAULTS | _clean_fields(raw, NFTGUARD_DEFAULTS, "nftguard", force)
    for field in ("enabled", "dry_run", "enable_vk_output"):
        if type(options[field]) is not bool:
            raise ValueError(f"nftguard.{field} must be true or false.")
    options["allow_cidrs"] = nftguard_allowlist(options["allow_cidrs"], 4)
    options["allow_cidrs6"] = nftguard_allowlist(options["allow_cidrs6"], 6)
    return options


def _validate_sni_options(config: dict[str, Any]) -> None:
    if not SNI_RUN_FIELDS.intersection(config):
        return
    if not (config["reality"] or config["direct_xhttp"]):
        raise ValueError("REALITY SNI options require TCP REALITY or direct XHTTP REALITY.")
    auto = config.get("reality_sni_auto")
    if type(auto) is not bool:
        raise ValueError("reality_sni_auto must be true or false.")
    if not auto:
        if (SNI_RUN_FIELDS - {"reality_sni_auto"}).intersection(config):
            raise ValueError("Scan settings require reality_sni_auto: true.")
        if "reality_sni" not in config:
            raise ValueError("Set reality_sni when reality_sni_auto is false.")
        return
    if "reality_target" in config:
        raise ValueError("Remove reality_target when using automatic SNI selection.")
    if "reality_sni" not in config:
        raise ValueError("Set reality_sni with fallback names when using automatic SNI selection.")
    mode = config.setdefault("reality_sni_scan", "top10")
    if mode not in ("top10", "scanner"):
        raise ValueError("reality_sni_scan must be top10 or scanner.")
    if mode == "scanner":
        _validate_scanner_options(config)
    elif "reality_sni_scan_cidr" in config or "reality_sni_scan_timeout" in config:
        raise ValueError("CIDR and timeout require reality_sni_scan: scanner.")


def scanner_subnet(value: object) -> IPv4Network:
    """Validate a scanner target and normalize its host bits."""
    if not isinstance(value, str) or "/" not in value:
        raise ValueError("Enter an IPv4 CIDR, such as 192.0.2.0/28.")
    network = IPv4Network(value, strict=False)
    if network.prefixlen == 32:
        raise ValueError("Reality Scanner requires an IPv4 subnet wider than /32.")
    return network


def _validate_scanner_options(config: dict[str, Any]) -> None:
    if "reality_sni_scan_cidr" in config:
        config["reality_sni_scan_cidr"] = str(scanner_subnet(config["reality_sni_scan_cidr"]))
    timeout = config.setdefault("reality_sni_scan_timeout", 30)
    if type(timeout) is not int or timeout < 1:
        raise ValueError("reality_sni_scan_timeout must be a positive integer.")


def _run_options(raw: Mapping[str, Any]) -> tuple[str, Path | None, bool]:
    """Read one-time YAML options without adding them to the saved config."""
    action = raw.get("action", "install")
    if action not in ("install", "restore", "reset"):
        raise ValueError("YAML action must be install, restore, or reset.")
    confirmed = raw.get("confirm", False)
    if type(confirmed) is not bool:
        raise ValueError("confirm must be true or false.")
    source = raw.get("backup_file")
    if source is not None and (not isinstance(source, str) or not Path(source).is_absolute()):
        raise ValueError("backup_file must be an absolute path.")
    if source and action == "reset":
        raise ValueError("Use restore instead of reset with a backup file.")
    return action, Path(source) if source else None, confirmed


def _clean_fields(data: Mapping[str, Any], allowed: Iterable[str], label: str, force: bool) -> dict[str, Any]:
    unknown = data.keys() - allowed
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        if not force:
            raise ValueError(f"Unknown {label} fields: {names}.")
        print(f"Ignoring unknown {label} fields: {names}.", file=sys.stderr)
    return {key: value for key, value in data.items() if key in allowed}


def _hostname(value: object, field: str) -> str:
    if not isinstance(value, str) or not DOMAIN.fullmatch(value):
        raise ValueError(f"{field} must be a domain name.")
    return value.lower()


def _valid_route(path: str) -> bool:
    return bool(
        re.fullmatch(r"/[A-Za-z0-9_.~-]+(?:/[A-Za-z0-9_.~-]+)*", path)
        and all(segment not in (".", "..") for segment in path.split("/"))
    )


def validate_email(value: object) -> str:
    """Check the ACME contact email."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z0-9-]+", value):
        raise ValueError("email must be a valid ACME contact email.")
    return value


def validate_docker_user(value: object) -> str:
    """Check the name of a regular Linux user."""
    if not isinstance(value, str) or value == "root" or not re.fullmatch(r"[a-z_][a-z0-9_.-]*", value, re.I):
        raise ValueError("docker_user must be a regular Linux user.")
    return value


def xhttp_path(value: object, field: str, *, allow_root: bool = False) -> str:
    """Resolve a generated path and check the XHTTP route syntax."""
    if value == "generate":
        value = f"/media/hls/{secrets.token_hex(4)}/index.m3u8"
    if not isinstance(value, str) or not ((allow_root and value == "/") or _valid_route(value)):
        raise ValueError(f"{field} must be an absolute URL path with no query string.")
    return value


def _read_cdns(config: Mapping[str, Any], force: bool) -> list[dict[str, Any]]:
    cdns: list[dict[str, Any]] = []
    for i, source in enumerate(config["cdn"]):
        label = f"cdn[{i}]"
        if not isinstance(source, dict):
            raise ValueError(f"{label} must be a mapping.")
        cdn = _clean_fields(source, CDN_FIELDS, label, force)
        if cdn.get("provider") not in ("selectel", "yandex"):
            raise ValueError(f"{label}.provider must be selectel or yandex.")
        for field in ("tag", "remark"):
            if field in cdn and (not isinstance(cdn[field], str) or not cdn[field].strip()):
                raise ValueError(f"{label}.{field} must be a non-empty string.")
        cdn["edge"] = _hostname(cdn.get("edge"), f"{label}.edge")
        cdn["origin"] = _hostname(cdn.get("origin", config.get("origin_domain")), f"{label}.origin")
        if cdn["origin"] in (config["panel_domain"], config["subscription_domain"]):
            raise ValueError(f"{label}.origin must differ from the panel and subscription domains.")
        cdn.setdefault("port", 8080 + i)
        path = cdn["path"] = xhttp_path(cdn.get("path", XHTTP_EXAMPLE), f"{label}.path")
        for other in cdns:
            if other["origin"] == cdn["origin"] and (
                path == other["path"] or path.startswith(other["path"] + "/") or other["path"].startswith(path + "/")
            ):
                raise ValueError(f"{label}.path overlaps with {other['path']} on {cdn['origin']}.")
        cdns.append(cdn)
    return cdns


def port_plan(config: Mapping[str, Any]) -> dict[str, int]:
    """Assign the ports needed by setup and reject duplicate assignments."""
    caddy = config["https"] == "caddy"
    ports = {"ACME HTTP": 80, "Panel": config["panel_port"], "Subscriptions": config["subscription_port"]}
    if caddy:
        ports["Caddy HTTPS"] = 443
    if config["reality"]:
        ports["TCP REALITY"] = config["reality_backend_port" if caddy else "reality_port"]
    if config["direct_xhttp"]:
        ports["Direct XHTTP REALITY"] = config["direct_xhttp_port"]
    ports.update({f"CDN {i + 1} backend": cdn["port"] for i, cdn in enumerate(config["cdn"])})
    owners: dict[int, str] = {}
    for label, port in ports.items():
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(f"{label} requires a port from 1 to 65535.")
        if port in owners:
            raise ValueError(f"Port {port} is assigned to {owners[port]} and {label}.")
        owners[port] = label
    return ports


def use_saved_paths(raw: object, current: Mapping[str, Any], config: dict[str, Any]) -> None:
    """Reuse a generated path when the same YAML is applied again."""
    if not isinstance(raw, dict):
        return
    if raw.get("direct_xhttp_path") == "generate" and current["direct_xhttp"]:
        config["direct_xhttp_path"] = current["direct_xhttp_path"]
    saved_ports = {backend["port"]: backend for backend in current["cdn"]}
    for backend, source in zip(config["cdn"], raw.get("cdn", []), strict=True):
        old = saved_ports.get(backend["port"])
        if source.get("path") == "generate" and old and old["provider"] == backend["provider"]:
            backend["path"] = old["path"]
