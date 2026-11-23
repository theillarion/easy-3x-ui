"""Read the editable Compose files for a 3x-ui installation."""

from collections.abc import Mapping
from typing import Any

from setup3ui import ASSETS
from setup3ui.config import NFTGUARD_DEFAULTS


def render_compose(config: Mapping[str, Any]) -> str:
    """Return Compose YAML for a validated setup config."""
    import yaml

    definition = yaml.safe_load((ASSETS / "compose.yml").read_text(encoding="utf-8"))
    mode = yaml.safe_load((ASSETS / f"compose.{config['https']}.yml").read_text(encoding="utf-8"))
    services = definition["services"]
    services["panel"].setdefault("volumes", []).extend(mode["services"].pop("panel", {}).get("volumes", []))
    services.update(mode["services"])
    nftguard = config.get("nftguard", NFTGUARD_DEFAULTS)
    if nftguard["enabled"]:
        services["nftguard"].setdefault("environment", {}).update(
            DRY_RUN=str(nftguard["dry_run"]).lower(),
            ENABLE_VK_OUTPUT=str(nftguard["enable_vk_output"]).lower(),
            ALLOW_CIDRS=nftguard["allow_cidrs"],
            ALLOW_CIDRS6=nftguard["allow_cidrs6"],
        )
    else:
        services.pop("nftguard", None)
    return yaml.safe_dump(definition, sort_keys=False)
