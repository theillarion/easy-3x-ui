"""Command-line entry point for setup-3x-ui."""

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from setup3ui.config import (
    AUTH_FIELDS,
    DEFAULTS,
    DOMAIN,
    SNI_RUN_FIELDS,
    _load_yaml,
    _run_options,
    port_plan,
    read_config,
    use_saved_paths,
    validate_config,
)

if TYPE_CHECKING:
    from setup3ui.install_draft import Draft


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
        config = read_config(directory / "config.yml", False, installed=True)
        backup = restore_panel(directory, config, source)
    print(f"Database restored. Previous database: {backup / 'x-ui.db'}")


def change_reality_sni(directory: Path) -> None:
    """Run SNI selection against a managed configuration and apply it on confirmation."""
    from setup3ui.deploy.managed import managed_directory, operation_lock
    from setup3ui.deploy.reconfigure import apply_config
    from setup3ui.interactive import ask_reality_sni

    directory = managed_directory(directory)
    path = directory / "config.yml"
    current = read_config(path, False, installed=True)
    if not (current["reality"] or current["direct_xhttp"]):
        raise ValueError("Enable TCP REALITY or direct XHTTP REALITY first.")
    selected = ask_reality_sni(current)
    current_target = current.get("reality_target", f"{current['reality_sni'][0]}:443")
    print(f"Current REALITY SNI: {', '.join(current['reality_sni'])}, target: {current_target}")
    print(f"Proposed REALITY SNI: {', '.join(selected['reality_sni'])}, target: {selected['reality_target']}")
    if current["reality_sni"] == selected["reality_sni"] and current_target == selected["reality_target"]:
        print("Settings already match.")
        return
    if input("Apply these REALITY settings? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("REALITY settings unchanged.")
        return
    with operation_lock():
        if read_config(path, False, installed=True) != current:
            raise ValueError("Settings changed during SNI selection. Run reality-sni again.")
        apply_config(directory, current, validate_config(current | selected), {}, interactive=True)


def deploy_when_requested(*, required: bool = False) -> None:
    """Install the CLI when run through the package launcher."""
    if required or os.environ.get("SETUP_3X_UI_DEPLOY_CLI") == "1":
        from setup3ui.deploy.release import deploy_cli

        deploy_cli(Path(__file__).resolve().parents[1])


def _managed_installation(default: Path) -> Path | None:
    from setup3ui.deploy.managed import managed_directory
    from setup3ui.panel.discovery import discover_panels

    if (default / "config.yml").is_file():
        return default
    for panel in discover_panels():
        if (
            not panel["rootless"] and panel["project"] == "3x-ui"
            and panel["service"] == "panel" and panel["compose_dir"]
        ):
            try:
                return managed_directory(Path(panel["compose_dir"]))
            except (OSError, ValueError):
                continue
    return None


def _install_summary(config: dict[str, Any]) -> None:
    from setup3ui.terminal import Terminal

    terminal = Terminal()
    terminal.heading("Review & apply")
    ports = port_plan(config)
    summary = {
        "Directory": str(config["install_dir"]),
        "HTTPS": config["https"].title(),
        "Panel": f"{config['panel_domain']}:{config['panel_port']}",
        "Subscriptions": f"{config['subscription_domain']}:{config['subscription_port']}",
        "ACME HTTP": str(ports["ACME HTTP"]),
    }
    if "Caddy HTTPS" in ports:
        summary["Caddy HTTPS"] = str(ports["Caddy HTTPS"])
    if config["reality"]:
        port = ports["TCP REALITY"]
        summary["TCP REALITY"] = f"Port {port}" if config["https"] == "certbot" else f"Backend port {port}"
    if config["direct_xhttp"]:
        summary["Direct XHTTP REALITY"] = f"Port {config['direct_xhttp_port']} · {config['direct_xhttp_path']}"
    for index, cdn in enumerate(config["cdn"], 1):
        summary[f"CDN {index} ({cdn['provider']})"] = f"{cdn['edge']} → {cdn['origin']}:{cdn['port']}{cdn['path']}"
    if config["reality"] or config["direct_xhttp"]:
        names = config["reality_sni"]
        summary["REALITY SNI"] = ", ".join(names)
        summary["REALITY target"] = config.get("reality_target", f"{names[0]}:443")
    guard = config["nftguard"]
    summary["Scanner protection"] = (
        ("Dry run" if guard["dry_run"] else "Enforcing") if guard["enabled"] else "Disabled"
    )
    terminal.pairs(summary)
    if guard["enabled"] and not guard["dry_run"]:
        print("Check that the nftguard allowlist includes your management SSH address.")
    terminal.line()


@contextmanager
def _report_install_interrupt(path: Path) -> Iterator[None]:
    try:
        yield
    except KeyboardInterrupt:
        if path.is_file():
            raise KeyboardInterrupt("Run the same command to reopen the installation draft.") from None
        raise


def _interactive_install(args: argparse.Namespace, *, menu: bool, explicit_directory: bool) -> list[str]:
    from setup3ui.deploy.install import fresh_install_directory, install
    from setup3ui.deploy.managed import managed_directory, operation_lock
    from setup3ui.install_draft import Draft, InvalidDraft, draft_lock
    from setup3ui.interactive import Questions, ask_config, ask_reality_sni
    from setup3ui.terminal import Terminal

    if explicit_directory and not args.install_dir.is_absolute():
        raise ValueError("Enter an absolute installation directory.")
    with draft_lock() as path, _report_install_interrupt(path):
        try:
            draft = Draft.load(path)
        except InvalidDraft as exc:
            print(exc)
            if input("Start over and discard the unreadable draft? [y/N] ").strip().lower() not in {"y", "yes"}:
                return []
            path.unlink()
            draft = None
        if draft is not None:
            Terminal().heading("easy-3x-ui · Resume installation")
            while True:
                choice = input("Continue the unfinished installation? [Y/n] ").strip().lower()
                if choice in {"", "y", "yes", "continue"}:
                    break
                if choice in {"n", "no", "start over"}:
                    path.unlink()
                    draft = None
                    break
                print("Enter yes to continue or no to start over.")
        if draft is None:
            if menu:
                directory = _managed_installation(args.install_dir)
                if directory is not None:
                    commands = ("status", "backup", "restore", "reality-sni", "update", "test", "reset", "uninstall")
                    print(f"Manage 3x-ui. Commands: {', '.join(commands)}.")
                    action = input("Command [status]: ").strip() or "status"
                    if action not in commands:
                        raise ValueError("Choose one of the listed commands.")
                    return [action, "--install-dir", str(directory)]
            action = "restore" if args.backup_source else "install"
            if menu:
                Terminal().heading("easy-3x-ui · Installation")
                while True:
                    action = input(
                        "Start a fresh install, restore a backup, or discover/adopt an existing panel [install]: "
                    ).strip().lower() or "install"
                    if action in {"discover", "adopt"}:
                        return [action]
                    if action in {"install", "restore"}:
                        break
                    print("Choose install, restore, discover, or adopt.")
            draft = Draft(path, {"action": action})
            draft.save()

        answers = draft.answers
        if (
            explicit_directory and "install_dir" in answers
            and args.install_dir.resolve() != Path(answers["install_dir"])
        ):
            raise ValueError("The draft uses a different installation directory. Answer no to start over.")
        if args.backup_source and (
            answers["action"] != "restore"
            or ("backup_source" in answers and args.backup_source.resolve() != Path(answers["backup_source"]))
        ):
            raise ValueError("The draft uses a different backup source. Answer no to start over.")
        if draft.panel_installed:
            Terminal().heading("Finish CLI installation")
            with operation_lock():
                managed_directory(Path(answers["install_dir"]))
                print("The panel is installed. Finishing CLI installation.")
                deploy_when_requested(required=True)
                path.unlink()
            return []

        questions = Questions(draft)
        if "install_dir" in answers:
            directory = str(fresh_install_directory(Path(answers["install_dir"])))
        elif explicit_directory:
            directory = str(fresh_install_directory(args.install_dir))
            answers["install_dir"] = directory
            draft.save()
        else:
            questions.section("Installation directory")
            directory = questions.value(
                "install_dir", "Installation directory", str(args.install_dir),
                lambda value: str(fresh_install_directory(Path(value))),
            )
        source = None
        defaults: dict[str, Any] = {}
        if answers["action"] == "restore":
            source, defaults = _interactive_backup(draft, args.backup_source)

        while True:
            raw = ask_config(defaults, choose_sni=False, questions=questions)
            choose_sni = source is None and (raw["reality"] or raw["direct_xhttp"])
            try:
                config = validate_config(raw | {"install_dir": directory}, pending_sni=choose_sni)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                if input("Review your answers? [Y/n] ").strip().lower() not in {"", "y", "yes"}:
                    return []
                draft.review()
                continue
            break
        if choose_sni:
            config.update(ask_reality_sni(config, questions=questions))
            config = validate_config(config)
        _install_summary(config)
        if input(f"Install with {config['https']}? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("Install cancelled. Your answers are saved.")
            return []
        with operation_lock():
            install(config, port_plan(config), source=source, interactive=True)
            if os.environ.get("SETUP_3X_UI_DEPLOY_CLI") == "1":
                draft.panel_installed = True
                draft.save()
                deploy_when_requested()
            path.unlink()
    return []


def _interactive_backup(draft: "Draft", explicit_source: Path | None) -> tuple[Path, dict[str, Any]]:
    from setup3ui.interactive import Questions
    from setup3ui.panel.restore import backup_file

    def read(value: str) -> Path:
        if not value:
            raise ValueError("Enter the path to a backup database or backup directory.")
        try:
            source = Path(value).resolve()
            backup_file(source)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Could not check the backup path {value}: {exc}") from exc
        return source

    questions = Questions(draft)
    if explicit_source and "backup_source" not in draft.answers:
        source = read(str(explicit_source))
        defaults = backup_defaults(source)
        draft.answers["backup_source"] = str(source)
        draft.save()
        return source, defaults
    while True:
        source = Path(questions.value(
            "backup_source", "Backup database path", validate=lambda value: str(read(value)),
        ))
        try:
            backup_file(source)
            return source, backup_defaults(source)
        except (OSError, ValueError) as exc:
            print(exc)
            draft.previous["backup_source"] = draft.answers.pop("backup_source")
            draft.save()


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
            "reality-sni",
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
    try:
        explicit_directory = any(arg == "--install-dir" or arg.startswith("--install-dir=") for arg in raw_args)
        if args.action == "install" and args.config is None and sys.stdin.isatty():
            if args.docker_user:
                parser.error("--docker-user requires prepare-docker.")
            followup = _interactive_install(args, menu=not raw_args, explicit_directory=explicit_directory)
            if followup:
                main(followup)
            return
        if args.action == "reality-sni":
            if args.config or not sys.stdin.isatty():
                raise ValueError("Run reality-sni in a terminal, or use install --config for YAML.")
            change_reality_sni(args.install_dir)
            return
        if args.action == "install" and args.config:
            raw = _load_yaml(args.config)
            if not isinstance(raw, dict):
                raise ValueError("The YAML config must be a mapping.")
            action, source, confirmed = _run_options(raw)
            args.action = action
            args.backup_source = args.backup_source or source
            args.yes = args.yes or confirmed
            if (action != "install" or args.backup_source) and SNI_RUN_FIELDS.intersection(raw):
                raise ValueError("Run REALITY SNI selection separately after restoring or resetting.")
            if (
                action in ("restore", "reset")
                and "install_dir" in raw
                and not explicit_directory
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
                show_status(directory, read_config(directory / "config.yml", False, installed=True)["https"])
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
                    config = read_config(directory / "config.yml", False, installed=True)
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
                    reset(directory, read_config(directory / "config.yml", False, installed=True), auth, interactive=not args.config)
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
        if args.config is None and (args.action != "adopt" or not sys.stdin.isatty()):
            parser.error(f"{args.action} requires --config.")
        if args.docker_user:
            parser.error("--docker-user requires prepare-docker.")
        defaults = backup_defaults(args.backup_source) if args.action == "install" and args.backup_source else {}
        if args.config is None:
            from setup3ui.interactive import ask_config

            config = validate_config(ask_config(choose_sni=False))
            _install_summary(config)
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
        if args.action == "adopt" and SNI_RUN_FIELDS.intersection(config):
            raise ValueError("Run REALITY SNI selection separately after adoption.")
        if args.action == "install" and SNI_RUN_FIELDS.intersection(config):
            from setup3ui.host.reality import resolve_sni

            config = resolve_sni(config)
        if args.action == "render-compose":
            from setup3ui.deploy.compose import render_compose

            print(render_compose(config), end="")
            return
        if args.action == "render-caddy":
            from setup3ui.deploy.caddy import render_caddy

            if config["https"] != "caddy":
                raise ValueError("render-caddy requires https: caddy.")
            if config.get("reality_sni_auto"):
                raise ValueError("Use a config with a resolved REALITY SNI to render Caddy.")
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
                    current = read_config(local_config, False, installed=True)
                    if args.backup_source:
                        raise ValueError("Use restore to replace a database in an existing installation.")
                    if args.config:
                        incoming = _load_yaml(args.config)
                        use_saved_paths(incoming, current, config)
                        if isinstance(incoming, dict) and "nftguard" not in incoming:
                            config["nftguard"] = current["nftguard"]
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
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Error: {exc}\n")


def entrypoint() -> None:
    """Run the CLI and handle terminal cancellation."""
    try:
        main()
    except KeyboardInterrupt as exc:
        print(f"Interrupted. {exc}".rstrip(), file=sys.stderr)
        sys.exit(130)
    except EOFError:
        print("Cancelled.", file=sys.stderr)
        sys.exit(1)
