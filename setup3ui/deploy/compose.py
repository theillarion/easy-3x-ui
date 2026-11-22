"""Build the Compose file for a 3x-ui installation."""

from collections.abc import Mapping
from typing import Any


def _bind(source: str, target: str, *, read_only: bool = False) -> dict[str, object]:
    return {
        "type": "bind",
        "source": f"./{source}",
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def _certbot_compose() -> dict[str, Any]:
    return {
        "name": "3x-ui",
        "services": {
            "panel": {
                "image": "ghcr.io/mhsanaei/3x-ui:latest",
                "pull_policy": "never",
                "network_mode": "host",
                "cap_add": ["NET_ADMIN", "NET_RAW"],
                "devices": ["/dev/net/tun:/dev/net/tun"],
                "volumes": [
                    _bind("db", "/etc/x-ui"),
                    _bind("logs", "/var/log/x-ui"),
                    _bind("certs", "/certs", read_only=True),
                ],
                "environment": {
                    "XRAY_VMESS_AEAD_FORCED": "false",
                    "XUI_ENABLE_FAIL2BAN": "false",
                    "XUI_LOG_FOLDER": "/var/log/x-ui",
                },
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "5"},
                },
                "tty": True,
                "restart": "unless-stopped",
            },
            "certbot": {
                "image": "certbot/certbot:v5.1.0",
                "pull_policy": "never",
                "profiles": ["certificates"],
                "network_mode": "host",
                "volumes": [
                    _bind("certs", "/etc/letsencrypt"),
                    _bind("certbot-work", "/var/lib/letsencrypt"),
                    _bind("certbot-log", "/var/log/letsencrypt"),
                ],
            },
        },
    }


def render_compose(config: Mapping[str, object]) -> str:
    """Return the Compose YAML for a validated setup config."""
    import yaml

    definition = _certbot_compose()
    if config["https"] == "caddy":
        from setup3ui.deploy.caddy import CADDY_IMAGE

        services = definition["services"]
        services.pop("certbot")
        services["panel"]["volumes"].pop()
        services["caddy"] = {
            "image": CADDY_IMAGE,
            "pull_policy": "never",
            "network_mode": "host",
            "volumes": [
                _bind("caddy-config", "/etc/caddy", read_only=True),
                _bind("caddy-data", "/data"),
                _bind("caddy-state", "/config"),
                _bind("www", "/srv", read_only=True),
            ],
            "logging": services["panel"]["logging"].copy(),
            "restart": "unless-stopped",
        }
    return yaml.safe_dump(definition, sort_keys=False)
