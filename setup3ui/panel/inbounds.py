"""Build empty inbounds and add them through the local panel."""

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from setup3ui import ASSETS
from setup3ui.host.docker import docker
from setup3ui.panel.api import PanelAPI

KEY_COMMAND = 'set -- /app/bin/xray-linux-*; exec "$1" x25519'


def default_reality_tag(config: Mapping[str, Any], *, xhttp: bool) -> str:
    """Use the port that the client connects to."""
    if xhttp:
        return f"xray-xhttp-reality-{config['direct_xhttp_port']}"
    port = 443 if config["https"] == "caddy" else config["reality_port"]
    return f"xray-tls-reality-{port}"


def _template(
    name: str,
    config: Mapping[str, Any],
    *,
    network: str,
    security: str,
) -> dict[str, Any]:
    path = Path(config.get("inbound_templates", {}).get(name, ASSETS / "templates" / f"{name.replace('_', '-')}.json"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Could not read inbound template {name} at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Inbound template {name} at {path} must contain a JSON object.")
    stream = data.get("streamSettings")
    if (
        data.get("protocol") != "vless"
        or not isinstance(stream, dict)
        or (stream.get("network"), stream.get("security")) != (network, security)
    ):
        raise ValueError(f"Inbound template {name} at {path} must use VLESS, {network} and security {security}.")
    sections = [data.get("settings")]
    if network == "xhttp":
        sections.append(stream.get("xhttpSettings"))
    if security == "reality":
        reality = stream.get("realitySettings")
        sections.extend((reality, reality.get("settings") if isinstance(reality, dict) else None))
    else:
        proxies = stream.get("externalProxy")
        if not isinstance(proxies, list) or len(proxies) != 1 or not isinstance(proxies[0], dict):
            raise ValueError(f"Inbound template {name} at {path} must have one external proxy.")
    if any(not isinstance(section, dict) for section in sections):
        raise ValueError(f"Inbound template {name} at {path} is missing a settings object.")
    remark = data.get("remark")
    if not isinstance(remark, str) or not remark.strip():
        raise ValueError(f"Inbound template {name} at {path} must have a remark.")
    data["settings"]["clients"] = []
    return data


def _reality(image: str, config: Mapping[str, Any], *, xhttp: bool) -> dict[str, Any]:
    name = "direct_xhttp" if xhttp else "reality"
    inbound = _template(name, config, network="xhttp" if xhttp else "tcp", security="reality")
    result = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/bin/sh",
        image,
        "-c",
        KEY_COMMAND,
        capture=True,
    )
    private = re.search(r"(?im)^Private\s*Key:\s*([\w-]{43})$", result.stdout)
    public = re.search(r"(?im)^(?:Public\s*Key|Password(?: \(PublicKey\))?):\s*([\w-]{43})$", result.stdout)
    if not private or not public:
        raise ValueError("Xray did not return a usable REALITY key pair.")
    use_caddy = config["https"] == "caddy"
    names = config["reality_sni"]
    port = config["direct_xhttp_port"] if xhttp else config["reality_backend_port" if use_caddy else "reality_port"]
    stream = inbound["streamSettings"]
    reality = stream["realitySettings"]
    reality.update(
        target=config.get("reality_target", f"{names[0]}:443"),
        serverNames=names,
        privateKey=private.group(1),
        shortIds=[secrets.token_hex(8) for _ in range(8)],
    )
    reality["settings"].update(publicKey=public.group(1), serverName=names[0])
    stream.pop("externalProxy", None)
    if xhttp:
        stream["xhttpSettings"]["path"] = config["direct_xhttp_path"]
    elif use_caddy:
        stream["externalProxy"] = [
            {"dest": config["reality_address"], "port": 443, "forceTls": "same", "remark": inbound["remark"]}
        ]
    inbound.update(
        tag=config.get(f"{name}_tag", default_reality_tag(config, xhttp=xhttp)),
        listen="127.0.0.1" if use_caddy and not xhttp else "",
        port=port,
    )
    return inbound


def _cdn(backend: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    inbound = _template(backend["provider"], config, network="xhttp", security="none")
    inbound.update(port=backend["port"], listen="127.0.0.1")
    stream = inbound["streamSettings"]
    stream["xhttpSettings"]["path"] = backend["path"]
    proxy = stream["externalProxy"][0]
    proxy["dest"] = backend["edge"]
    proxy.setdefault("port", 443)
    tag = backend.get("tag") or inbound.get("tag")
    if not tag:
        provider = backend["provider"]
        tag = f"cdn-{provider}-{proxy['port']}"
        if sum(item["provider"] == provider for item in config.get("cdn", ())) > 1:
            route = f"{backend['origin']}:{backend['path']}"
            tag += f"-{hashlib.sha256(route.encode()).hexdigest()[:8]}"
    inbound["tag"] = tag
    if "remark" in backend:
        inbound["remark"] = proxy["remark"] = backend["remark"]
    return inbound


def create_inbounds(api: PanelAPI, config: Mapping[str, Any], image: str) -> None:
    """Create independent inbounds without clients."""
    inbounds = []
    for flag, xhttp in (("reality", False), ("direct_xhttp", True)):
        if config[flag]:
            inbounds.append(_reality(image, config, xhttp=xhttp))
    inbounds.extend(_cdn(backend, config) for backend in config["cdn"])
    for inbound in inbounds:
        api.call("panel/api/inbounds/add", inbound)
        print(f"Added inbound {inbound['remark']} on port {inbound['port']}.")
    if inbounds:
        api.call("panel/api/server/restartXrayService", {})
