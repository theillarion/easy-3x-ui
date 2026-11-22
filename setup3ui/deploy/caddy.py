"""Prepare the Caddy image and render routes for the panel, subscriptions, and XHTTP."""

import hashlib
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from setup3ui import ASSETS
from setup3ui.host.docker import docker

CADDY_IMAGE = "setup-3x-ui-caddy:2.11.7-l4"
CADDY_REGISTRY = "ghcr.io/theillarion/easy-3x-ui-caddy"


def prepare_caddy_image(image: str = CADDY_IMAGE, *, refresh: bool = False) -> None:
    """Use the published build for this Dockerfile, or build it locally."""
    if not refresh:
        try:
            docker("image", "inspect", image, capture=True)
        except subprocess.CalledProcessError:
            pass
        else:
            return

    recipe = (ASSETS / "Caddy.Dockerfile").read_bytes()
    digest = hashlib.sha256(recipe).hexdigest()
    published = f"{CADDY_REGISTRY}:sha256-{digest}"
    try:
        docker("pull", published, capture=True)
    except subprocess.CalledProcessError:
        print("Could not pull the Caddy image. Building it locally.", flush=True)
        pull = ("--pull",) if refresh else ()
        docker("build", *pull, "-t", image, "-", input_text=recipe.decode("utf-8"))
    else:
        docker("image", "tag", published, image)


def render_caddy(config: Mapping[str, Any]) -> str:
    """Use public HTTPS for web services and forward REALITY as raw TCP."""
    sections = [
        "{",
        "\temail " + config["email"],
        "\tservers :443 {",
        "\t\tprotocols h1 h2",
    ]
    if config["reality"]:
        sections.extend(
            (
                "\t\tlistener_wrappers {",
                "\t\t\tlayer4 {",
                "\t\t\t\t@reality tls sni " + " ".join(config["reality_sni"]),
                "\t\t\t\troute @reality {",
                f"\t\t\t\t\tproxy tcp/127.0.0.1:{config['reality_backend_port']}",
                "\t\t\t\t}",
                "\t\t\t}",
                "\t\t\ttls",
                "\t\t}",
            )
        )
    sections.extend(
        (
            "\t}",
            "}",
            "",
            "(public_tls) {",
            "\ttls {",
            "\t\tissuer acme {",
            "\t\t\tdisable_tlsalpn_challenge",
            "\t\t}",
            "\t}",
            "}",
        )
    )
    for kind in ("panel", "subscription"):
        sections.extend(
            (
                "",
                f"{config[f'{kind}_domain']} {{",
                "\timport public_tls",
                f"\treverse_proxy 127.0.0.1:{config[f'{kind}_port']}",
                "}",
            )
        )
    groups = defaultdict(list)
    for backend in config["cdn"]:
        groups[backend["origin"]].append(backend)
    for origin, backends in groups.items():
        sections.extend(("", f"{origin} {{", "\timport public_tls"))
        for index, backend in enumerate(backends):
            path = backend["path"]
            sections.extend(
                (
                    f"\t@xhttp{index} path {path} {path}/*",
                    f"\thandle @xhttp{index} {{",
                    f"\t\treverse_proxy 127.0.0.1:{backend['port']} {{",
                    "\t\t\tflush_interval -1",
                    "\t\t}",
                    "\t}",
                )
            )
        sections.extend(
            ("\thandle {", "\t\troot * /srv", "\t\tfile_server", "\t}", "}")
        )
    return "\n".join(sections) + "\n"
