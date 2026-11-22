"""Command-line entry point for setup-3x-ui."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from setup3ui.config import (
    AUTH_FIELDS,
    DEFAULTS,
    DOMAIN,
    _load_yaml,
    _run_options,
    port_plan,
    read_config,
    use_saved_paths,
    validate_config,
)


def backup_defaults(source: Path) -> dict[str, Any]:
    """Offer panel domains and ports found in a backup."""
    from setup3ui.panel.restore import settings

    values = settings(source / "x-ui.db" if source.is_dir() else source)
    defaults: dict[str, Any] = {}
    bundled_config = source / "config.yml"
    if source.is_dir() and bundled_config.is_file():
        config = read_config(bundled_config, False)
        excluded = AUTH_FIELDS | {"install_dir", "docker_user"}
        defaults.update({key: value for key, value in config.items() if key not in excluded})
    elif values.get("webListen") == "127.0.0.1" and not values.get("webCertFile"):
        defaults["https"] = "caddy"
    for label, key in (("panel", "web"), ("subscription", "sub")):
        domain = values.get(f"{key}Domain", "")
        if DOMAIN.fullmatch(domain):
            defaults[f"{label}_domain"] = domain
        port = values.get(f"{key}Port", "")
        if port.isdecimal() and 1 <= int(port) <= 65535:
            defaults[f"{label}_port"] = int(port)
    return defaults


def restore(directory: Path, source: Path | None, confirmed: bool) -> None:
    """Select a backup and confirm replacement of the local database."""
    from setup3ui.deploy.managed import operation_lock
    from setup3ui.panel.restore import restore_panel

    if source is None:
        if not sys.stdin.isatty():
            raise ValueError("Set --from to the backup file or directory.")
        choices = sorted((directory / "backups").glob("*/x-ui.db"), reverse=True)
        choices.extend(sorted((directory / "backups").glob("x-ui-*.db"), reverse=True))
        if not choices:
            raise ValueError("No backups found. Use --from to select a file.")
        for number, item in enumerate(choices, 1):
            print(f"{number}. {item} ({item.stat().st_size // 1024} KiB)")
        selection = input("Backup number: ").strip()
        if not selection.isdecimal() or not 1 <= int(selection) <= len(choices):
            raise ValueError("Choose a backup number from the list.")
        source = choices[int(selection) - 1]
    if not confirmed:
        if not sys.stdin.isatty():
            raise ValueError("Restore replaces the current database. Use --yes to confirm.")
        if input("Back up the current database and replace it? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Restore cancelled.")
            return
    with operation_lock():
        config = read_config(directory / "config.yml", False)
        backup = restore_panel(directory, config, source)
    print(f"Database restored. Previous database: {backup / 'x-ui.db'}")


def deploy_when_requested() -> None:
    """Install the CLI when run through the package launcher."""
    if os.environ.get("SETUP_3X_UI_DEPLOY_CLI") == "1":
        from setup3ui.deploy.release import deploy_cli

        deploy_cli(Path(__file__).resolve().parents[1])


def main(argv: list[str] | None = None) -> None:
    """Parse command arguments and run the selected setup step."""
    parser = argparse.ArgumentParser(description="Validate settings, check the host, or print Compose.")
    parser.add_argument(
        "action",
        nargs="?",
        choices=(
            "validate",
            "install",
            "prepare-docker",
            "preflight",
            "render-compose",
            "render-caddy",
            "issue-cert",
            "configure-panel-tls",
            "install-renewal",
            "renew-cert",
            "status",
            "backup",
            "restore",
            "discover",
            "adopt",
            "update",
            "uninstall",
            "reset",
            "test",
        ),
        default="install",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--from", dest="backup_source", type=Path, help="3x-ui database or backup directory.")
    parser.add_argument("--yes", action="store_true", help="Confirm an operation that replaces data.")
    parser.add_argument(
        "--component", choices=("panel", "caddy", "certbot", "installer"), help="Update only this component."
    )
    parser.add_argument("--installer-source", help="Installer archive path or HTTPS URL.")
    parser.add_argument("--sha256", help="SHA-256 of an installer archive.")
    parser.add_argument("--rotate-token", action="store_true", help="Replace a node token during update.")
    parser.add_argument("--token-name", default="setup-3x-ui", help="Name of the node token to replace.")
    parser.add_argument("--purge-data", action="store_true", help="Remove stored data during uninstall.")
    parser.add_argument("--link-file", type=Path, help="File with VLESS links or an HTTPS subscription URL.")
    parser.add_argument("--resource", action="append", default=[], help="HTTPS resource to check with test.")
    parser.add_argument("--xray", default="xray", help="Path to the Xray client binary.")
    parser.add_argument("--download-xray", action="store_true", help="Get a temporary official Xray binary if needed.")
    parser.add_argument("--json", action="store_true", help="Print test results as JSON.")
    parser.add_argument("--install-dir", type=Path, default=Path(str(DEFAULTS["install_dir"])))
    parser.add_argument("--docker-user", help="Linux user to add to the docker group during prepare-docker.")
    parser.add_argument("--force", action="store_true", help="Ignore unknown YAML fields.")
    raw_args = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(raw_args)
    if not raw_args and sys.stdin.isatty():
        directory = Path(str(DEFAULTS["install_dir"]))
        if (directory / "config.yml").is_file():
            print("Manage 3x-ui. Commands: status, backup, restore, update, test, reset, uninstall.")
            action = input("Command [status]: ").strip() or "status"
            if action not in {"status", "backup", "restore", "update", "test", "reset", "uninstall"}:
                parser.error("Choose one of the listed commands.")
            main([action])
            return
        action = (
            input("Start a fresh install, restore a backup, or discover/adopt an existing panel [install]: ")
            .strip()
            .lower()
        )
        if action in ("discover", "adopt"):
            main([action])
            return
        if action == "restore":
            main(["install", "--from", input("Backup database path: ").strip()])
            return
        if action not in ("", "install"):
            parser.error("Choose install, restore, discover, or adopt.")
    try:
        if args.action == "install" and args.config:
            raw = _load_yaml(args.config)
            if not isinstance(raw, dict):
                raise ValueError("The YAML config must be a mapping.")
            action, source, confirmed = _run_options(raw)
            args.action = action
            args.backup_source = args.backup_source or source
            args.yes = args.yes or confirmed
            if (
                action in ("restore", "reset")
                and "install_dir" in raw
                and not any(arg == "--install-dir" or arg.startswith("--install-dir=") for arg in raw_args)
            ):
                directory = raw["install_dir"]
                if not isinstance(directory, str) or not Path(directory).is_absolute():
                    raise ValueError("install_dir must be an absolute path.")
                args.install_dir = Path(directory)
        if args.action == "discover":
            from setup3ui.panel.discovery import discover_panels, show_panels

            show_panels(discover_panels())
            return
        if args.action == "prepare-docker":
            from setup3ui.host.docker import prepare_docker

            config = read_config(args.config, args.force) if args.config else {}
            prepare_docker(args.docker_user or config.get("docker_user"))
            return
        if args.action == "test":
            from setup3ui.diagnostics.connections import test_connections

            config_path = args.config or args.install_dir / "config.yml"
            try:
                test_config = read_config(config_path, args.force) if args.config or config_path.is_file() else None
            except PermissionError:
                if args.config:
                    raise
                test_config = None
            test_connections(
                test_config, args.link_file, args.resource, args.xray, args.json, download=args.download_xray
            )
            return
        if args.action == "update" and (args.component == "installer" or args.installer_source):
            from setup3ui.deploy.managed import operation_lock
            from setup3ui.deploy.updater import update_installer

            with operation_lock():
                if args.installer_source:
                    update_installer(args.installer_source, args.sha256)
                elif os.environ.get("SETUP_3X_UI_DEPLOY_CLI") == "1":
                    deploy_when_requested()
                    print("Installer files updated.")
                else:
                    raise ValueError("Run 'install.sh update --component installer' or pass --installer-source.")
            if args.component == "installer":
                return
        if args.action in ("status", "backup", "restore", "update", "uninstall", "reset"):
            from setup3ui.deploy.managed import (
                backup_installation,
                managed_directory,
                operation_lock,
                show_status,
            )

            directory = managed_directory(args.install_dir)
            if args.action == "backup":
                with operation_lock():
                    print(f"Backup: {backup_installation(directory)}")
            elif args.action == "status":
                show_status(directory, read_config(directory / "config.yml", False)["https"])
            elif args.action == "restore":
                restore(directory, args.backup_source, args.yes)
            elif args.action == "update":
                from setup3ui.deploy.lifecycle import rotate_token, update

                if args.rotate_token:
                    print("Replacing a token invalidates the previous token with this name.")
                    if not args.yes and (
                        not sys.stdin.isatty() or input("Replace the token? [y/N] ").strip().lower() not in {"y", "yes"}
                    ):
                        raise ValueError("Token replacement was not confirmed.")
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.token_name):
                        raise ValueError("Invalid token name.")
                with operation_lock():
                    config = read_config(directory / "config.yml", False)
                    update(directory, config, args.component)
                    if args.rotate_token:
                        rotate_token(directory, config, args.token_name)
            elif args.action == "reset":
                from setup3ui.deploy.lifecycle import reset

                if not args.yes and (
                    not sys.stdin.isatty()
                    or input("Back up and reset the database and token? [y/N] ").strip().lower() not in {"y", "yes"}
                ):
                    raise ValueError("Database reset was not confirmed.")
                auth = _load_yaml(args.config) if args.config else {}
                if not isinstance(auth, dict):
                    raise ValueError("Authentication YAML must be a mapping.")
                with operation_lock():
                    reset(directory, read_config(directory / "config.yml", False), auth, interactive=not args.config)
            else:
                from setup3ui.deploy.lifecycle import uninstall

                print(f"Uninstall paths are under {directory}.")
                if (
                    args.purge_data
                    and not args.yes
                    and (not sys.stdin.isatty() or input("Type DELETE to remove saved data: ") != "DELETE")
                ):
                    raise ValueError("Data removal was not confirmed.")
                with operation_lock():
                    uninstall(directory, purge=args.purge_data)
            return
        if args.config is None and (args.action not in ("install", "adopt") or not sys.stdin.isatty()):
            parser.error(f"{args.action} requires --config.")
        if args.docker_user:
            parser.error("--docker-user requires prepare-docker.")
        defaults = backup_defaults(args.backup_source) if args.action == "install" and args.backup_source else {}
        if args.config is None:
            from setup3ui.interactive import ask_config

            config = validate_config(ask_config(defaults))
            print(
                json.dumps(
                    {
                        "https": config["https"],
                        "panel": config["panel_domain"],
                        "subscription": config["subscription_domain"],
                        "ports": port_plan(config),
                        "cdn": config["cdn"],
                    },
                    indent=2,
                )
            )
            if input(f"Install with {config['https']}? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Install cancelled.")
                return
        elif defaults:
            raw = _load_yaml(args.config)
            if not isinstance(raw, dict):
                raise ValueError("The YAML config must be a mapping.")
            config = validate_config(defaults | raw, args.force)
        else:
            config = read_config(args.config, args.force)
        if args.action == "render-compose":
            from setup3ui.deploy.compose import render_compose

            print(render_compose(config), end="")
            return
        if args.action == "render-caddy":
            from setup3ui.deploy.caddy import render_caddy

            if config["https"] != "caddy":
                raise ValueError("render-caddy requires https: caddy.")
            print(render_caddy(config), end="")
            return
        if args.action == "issue-cert":
            from setup3ui.certificates.acme import issue_certificate

            if config["https"] != "certbot":
                raise ValueError("issue-cert requires https: certbot.")
            chain, key = issue_certificate(
                Path(config["install_dir"]), config["panel_domain"], config["subscription_domain"], config["email"]
            )
            print(f"Certificate: {chain}\nPrivate key file: {key}")
            return
        if args.action in ("install-renewal", "renew-cert"):
            from setup3ui.certificates.renewal import (
                install_renewal_timer,
                renew_certificate,
            )

            if config["https"] != "certbot":
                raise ValueError(f"{args.action} requires https: certbot.")
            install_dir, domain = Path(config["install_dir"]), config["panel_domain"]
            if args.action == "install-renewal":
                install_renewal_timer(install_dir, domain)
            elif renew_certificate(install_dir, domain):
                print("Panel restarted with the renewed certificate.")
            return
        if args.action == "configure-panel-tls":
            from setup3ui.panel.tls import configure_panel_tls

            if config["https"] != "certbot":
                raise ValueError("configure-panel-tls requires https: certbot.")
            panel_url, subscription_url, backup = configure_panel_tls(
                Path(config["install_dir"]),
                config["panel_domain"],
                config["panel_port"],
                config["subscription_domain"],
                config["subscription_port"],
            )
            if backup:
                print(f"Database backup: {backup}")
            print(f"Panel: {panel_url}\nSubscription base URL: {subscription_url}")
            return
        ports = port_plan(config)
        if args.action == "adopt":
            from setup3ui.deploy.adoption import adopt
            from setup3ui.deploy.managed import operation_lock

            if not args.yes and (
                not sys.stdin.isatty()
                or input("Back up and replace the old Compose deployment? [y/N] ").strip().lower() not in {"yes", "y"}
            ):
                raise ValueError("Adoption was not confirmed.")
            with operation_lock():
                adopt(config, ports)
                deploy_when_requested()
            return
        if args.action == "install":
            from setup3ui.deploy.install import install
            from setup3ui.deploy.managed import operation_lock

            with operation_lock():
                local_config = Path(config["install_dir"]) / "config.yml"
                if local_config.is_file():
                    current = read_config(local_config, False)
                    if args.backup_source:
                        raise ValueError("Use restore to replace a database in an existing installation.")
                    if args.config:
                        use_saved_paths(_load_yaml(args.config), current, config)
                        config = validate_config(config)
                    for field in ("reality_tag", "direct_xhttp_tag"):
                        if field in current:
                            config.setdefault(field, current[field])
                    comparable = {key: value for key, value in config.items() if key not in AUTH_FIELDS}
                    if current != comparable:
                        from setup3ui.deploy.managed import managed_directory
                        from setup3ui.deploy.reconfigure import apply_config

                        auth = {key: value for key, value in config.items() if key in AUTH_FIELDS}
                        apply_config(
                            managed_directory(local_config.parent),
                            current,
                            comparable,
                            auth,
                            interactive=not args.config,
                        )
                    else:
                        print("Settings already match.")
                else:
                    install(config, ports, source=args.backup_source, interactive=not args.config)
                deploy_when_requested()
            return
        if args.action == "preflight":
            from setup3ui.host.preflight import check_host

            check_host(ports)
            print("Local TCP ports are free and the TUN device can be opened.")
            return
        preview = {"https": config["https"], "ports": ports, "cdn": config["cdn"]}
        if "docker_user" in config:
            preview["docker_user"] = config["docker_user"]
        print(json.dumps(preview, indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as exc:
        parser.exit(1, f"Error: {exc}\n")


def entrypoint() -> None:
    """Run the CLI and handle terminal cancellation."""
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        sys.exit(1)
