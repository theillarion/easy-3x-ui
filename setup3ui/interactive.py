"""Ask for settings for an interactive installation."""

import os
from collections.abc import Callable, Mapping
from itertools import count
from typing import Any

from setup3ui.config import (
    NFTGUARD_DEFAULTS,
    _hostname,
    nftguard_allowlist,
    scanner_subnet,
    validate_config,
    validate_docker_user,
    validate_email,
    xhttp_path,
)
from setup3ui.host.reality import blocked_names, primary_subnet, probe, report_probes, scan
from setup3ui.install_draft import Draft
from setup3ui.terminal import ScanDisplay, Terminal

EXAMPLE = "/media/hls/9636f214/index.m3u8"


class Questions:
    """Ask and validate an answer, reusing it when a draft is resumed."""

    def __init__(self, draft: Draft | None = None) -> None:
        self.draft = draft
        self.terminal = Terminal()
        self._section = ""

    def section(self, label: str) -> None:
        """Set the heading for the next visible question or check."""
        self._section = label

    def show_section(self) -> None:
        """Print the current heading once when a section has visible work."""
        if self._section:
            self.terminal.heading(self._section)
            self._section = ""

    def answered(self, key: str) -> bool:
        """Tell whether the question already has an accepted answer."""
        return self.draft is not None and key in self.draft.answers

    def _ask(self, key: str, prompt: str, default: str, validate: Callable[[str], str]) -> str:
        if self.draft is not None and key in self.draft.answers:
            return self.draft.answers[key]
        self.show_section()
        while True:
            entered = input(prompt).strip()
            try:
                answer = validate(entered or default)
            except ValueError as exc:
                if self.draft is None:
                    raise
                print(exc)
                continue
            if self.draft is not None:
                self.draft.answers[key] = answer
                self.draft.save()
            return answer

    def value(
        self,
        key: str,
        label: str,
        default: str = "",
        validate: Callable[[str], str] = str,
    ) -> str:
        """Read a value and offer the previous answer during a review."""
        if self.draft is not None:
            default = self.draft.previous.get(key, default)
        prompt = f"{label} [{default}]: " if default else f"{label}: "
        return self._ask(key, prompt, default, validate)

    def yes(self, key: str, label: str, default: bool = False) -> bool:
        """Ask a yes-or-no question."""
        answer = "y" if default else "n"
        if self.draft is not None:
            answer = self.draft.previous.get(key, answer)

        def choice(reply: str) -> str:
            reply = reply.lower()
            if reply in {"y", "yes"}:
                return "y"
            if self.draft is not None and reply not in {"n", "no"}:
                raise ValueError("Enter yes or no.")
            return "n"

        return self._ask(key, f"{label} [{'Y/n' if answer == 'y' else 'y/N'}] ", answer, choice) == "y"

    def port(self, key: str, label: str, default: int) -> int:
        """Read and check a TCP port."""
        def validate(reply: str) -> str:
            if not reply.isdecimal():
                raise ValueError(f"{label} must be a port number.")
            value = int(reply)
            if not 1 <= value <= 65535:
                raise ValueError(f"{label} must be between 1 and 65535.")
            return str(value)

        return int(self.value(key, label, str(default), validate))

    def result(self, key: str, run: Callable[[], list[str]]) -> list[str]:
        """Remember a probe or scan result before asking about it."""
        if self.draft is None:
            return run()
        if key not in self.draft.results:
            self.draft.results[key] = run()
            self.draft.save()
        return self.draft.results[key]


def _choice(value: str, options: tuple[str, ...], error: str) -> str:
    value = value.lower()
    if value not in options:
        raise ValueError(error)
    return value


def _pick_name(questions: Questions, key: str, names: list[str]) -> str:
    def choose(value: str) -> str:
        value = value.lower()
        if value == "m":
            return ""
        digits = value.lstrip("0")
        if digits.isascii() and digits.isdecimal() and len(digits) <= len(str(len(names))):
            index = int(digits)
            if 1 <= index <= len(names):
                return names[index - 1]
        raise ValueError("Choose a number from the list or m.")

    while True:
        try:
            return questions.value(key, "REALITY SNI number, or m for manual entry", "1", choose)
        except ValueError as exc:
            print(exc)


def _choose_candidates(
    config: Mapping[str, Any], questions: Questions, result_key: str, pick_key: str,
    mode: str, cidr: str | None = None, timeout: int = 30,
) -> str | None:
    terminal = questions.terminal
    display = ScanDisplay(terminal)
    saved = questions.draft is not None and result_key in questions.draft.results

    def run() -> list[str]:
        questions.show_section()
        try:
            with display.track():
                if cidr is None:
                    return scan(config, mode, output=display)
                return scan(config, mode, cidr, timeout, output=display)
        except (OSError, ValueError) as exc:
            terminal.line(f"REALITY scan failed: {exc}")
            return []

    names = questions.result(result_key, run)
    if not questions.answered(pick_key):
        if display.rows:
            terminal.probe_results(display.rows, numbered=True)
        elif names:
            questions.show_section()
            if saved:
                terminal.line("Saved results:")
            for index, name in enumerate(names, 1):
                terminal.line(f"{index}. {name}")
    if names:
        return _pick_name(questions, pick_key, names)
    if not saved:
        terminal.line("No suitable REALITY SNI found.")
    return None


def _scan_automatically(config: Mapping[str, Any], questions: Questions) -> str:
    def subnet(value: str) -> str:
        return "top10" if value.lower() == "top10" else str(scanner_subnet(value))

    def timeout_value(value: str) -> str:
        if not value.isdecimal():
            raise ValueError("Enter the Reality Scanner timeout in seconds.")
        if int(value) < 1:
            raise ValueError("The scanner timeout must be positive.")
        return value

    for attempt in count():
        key = f"sni.auto.{attempt}"
        try:
            mode = questions.value(
                f"{key}.mode", "REALITY scan type (top10, scanner, or manual)", "top10",
                lambda value: _choice(value, ("top10", "scanner", "manual"), "Choose top10, scanner, or manual."),
            )
            if mode == "manual":
                return ""
            cidr, timeout = None, 30
            if mode == "scanner":
                default = None if questions.answered(f"{key}.subnet") else primary_subnet()
                value = questions.value(
                    f"{key}.subnet", "IPv4 subnet to scan, or top10", str(default or "top10"), subnet,
                )
                if value == "top10":
                    mode = "top10"
                else:
                    cidr = value
                    timeout = int(questions.value(
                        f"{key}.timeout", "Reality Scanner timeout in seconds", "30", timeout_value,
                    ))
        except (OSError, ValueError) as exc:
            if questions.draft is not None:
                raise
            print(f"REALITY scan failed: {exc}")
            continue
        selected = _choose_candidates(config, questions, key, f"{key}.pick", mode, cidr, timeout)
        if selected is not None:
            return selected


def _verify_manual(questions: Questions, name: str, target: str) -> list[str]:
    questions.show_section()
    display = ScanDisplay(questions.terminal)
    with display.track():
        display.status(f"Checking TLS for {name} ({target})")
        passed = report_probes([probe(name, target)], output=display)
    questions.terminal.probe_results(display.rows, numbered=False)
    return passed


def ask_reality_sni(config: Mapping[str, Any], *, questions: Questions | None = None) -> dict[str, Any]:
    """Choose an SNI and its corresponding REALITY target in the terminal."""
    questions = questions or Questions()
    questions.section("REALITY SNI")
    if questions.yes("sni.automatic", "Find REALITY SNI automatically?"):
        name = _scan_automatically(config, questions)
        if name:
            return {"reality_sni": [name], "reality_target": f"{name}:443"}
    original = config["reality_sni"][0]
    default = original
    excluded = blocked_names(config)

    def validate(name: str) -> str:
        try:
            name = _hostname(name, "REALITY SNI")
        except ValueError:
            name = ""
        if not name or name in excluded:
            raise ValueError("Enter a domain name that differs from the Caddy site names.")
        return name

    for attempt in count():
        key = f"sni.manual.{attempt}"
        while True:
            try:
                name = questions.value(key, "REALITY SNI", default, validate)
                break
            except ValueError as exc:
                print(exc)
        target = config.get("reality_target", f"{name}:443") if name == original else f"{name}:443"
        if questions.result(f"{key}.probe", lambda: _verify_manual(questions, name, target)):
            return {"reality_sni": [name], "reality_target": target}
        if attempt == 0:
            selected = _choose_candidates(config, questions, "sni.alternatives", "sni.alternative", "top10")
            if selected:
                return {"reality_sni": [selected], "reality_target": f"{selected}:443"}
        if questions.yes(f"{key}.keep", "Keep this SNI as unverified?"):
            questions.terminal.line(questions.terminal.style(f"{name}: saved as unverified.", "33"))
            return {"reality_sni": [name], "reality_target": target}
        default = name


def _ask_nftguard(questions: Questions, defaults: Mapping[str, Any]) -> dict[str, Any]:
    questions.section("Scanner protection")
    defaults = NFTGUARD_DEFAULTS | dict(defaults)
    enabled = questions.yes("nftguard.enabled", "Enable nftguard", defaults["enabled"])
    if not enabled:
        return {"enabled": False}
    dry_run = questions.yes("nftguard.dry_run", "Start in dry-run mode", defaults["dry_run"])
    if not dry_run and not questions.answered("nftguard.allow_cidrs"):
        questions.show_section()
        print("Allow your management SSH address before enabling the host firewall rules.")
    return {
        "enabled": True,
        "dry_run": dry_run,
        "enable_vk_output": questions.yes(
            "nftguard.enable_vk_output", "Block outgoing connections to VK, MAX and OK", defaults["enable_vk_output"],
        ),
        "allow_cidrs": questions.value(
            "nftguard.allow_cidrs", "IPv4 addresses/CIDRs to allow (comma-separated, '-' for none)",
            defaults["allow_cidrs"] or "-", lambda value: "" if value == "-" else nftguard_allowlist(value, 4),
        ),
        "allow_cidrs6": questions.value(
            "nftguard.allow_cidrs6", "IPv6 addresses/CIDRs to allow (comma-separated, '-' for none)",
            defaults["allow_cidrs6"] or "-", lambda value: "" if value == "-" else nftguard_allowlist(value, 6),
        ),
    }


def ask_config(
    defaults: Mapping[str, Any] | None = None, *, choose_sni: bool = True, questions: Questions | None = None,
) -> dict[str, Any]:
    """Ask only for settings used by the selected options."""
    defaults = defaults or {}
    questions = questions or Questions()
    questions.section("HTTPS & domains")
    https = questions.value(
        "https", "HTTPS with certbot or caddy", defaults.get("https", "certbot"),
        lambda value: _choice(value, ("certbot", "caddy"), "Choose certbot or caddy."),
    )
    hint = defaults.get("panel_domain", "").partition(".")[2]
    domain = questions.value("domain", "Main domain", hint, lambda value: _hostname(value, "domain"))
    data: dict[str, Any] = {
        "https": https,
        "domain": domain,
        "panel_domain": questions.value(
            "panel_domain", "Panel domain", defaults.get("panel_domain", f"master.{domain}"),
            lambda value: _hostname(value, "panel_domain"),
        ),
        "subscription_domain": questions.value(
            "subscription_domain", "Subscription domain", defaults.get("subscription_domain", f"gateway.{domain}"),
            lambda value: _hostname(value, "subscription_domain"),
        ),
        "email": questions.value("email", "ACME contact email", defaults.get("email", ""), validate_email),
        "panel_port": questions.port("panel_port", "Panel port", defaults.get("panel_port", 2053)),
        "subscription_port": questions.port(
            "subscription_port", "Subscription port", defaults.get("subscription_port", 2096),
        ),
    }
    sudo_user = os.environ.get("SUDO_USER")
    if questions.answered("docker_user") or not sudo_user or sudo_user == "root":
        data["docker_user"] = questions.value(
            "docker_user", "Linux user to add to the docker group", validate=validate_docker_user,
        )
    elif questions.draft is not None:
        # Preserve the effective sudo user if the wizard is resumed by another administrator.
        questions.draft.answers["docker_user"] = validate_docker_user(sudo_user)
        questions.draft.save()
        data["docker_user"] = sudo_user
    questions.section("Transports")
    data["reality"] = questions.yes("reality", "Use TCP REALITY", defaults.get("reality", False))
    data["direct_xhttp"] = questions.yes(
        "direct_xhttp", "Use direct XHTTP REALITY", defaults.get("direct_xhttp", False),
    )
    if data["reality"]:
        field = "reality_backend_port" if https == "caddy" else "reality_port"
        data[field] = questions.port(
            field, "TCP REALITY backend port" if https == "caddy" else "TCP REALITY port",
            defaults.get(field, 8444 if https == "caddy" else 443),
        )
    if data["direct_xhttp"]:
        data["direct_xhttp_port"] = questions.port(
            "direct_xhttp_port", "Direct XHTTP port", defaults.get("direct_xhttp_port", 8443),
        )
        data["direct_xhttp_path"] = questions.value(
            "direct_xhttp_path", f"Direct XHTTP path, or generate (example {EXAMPLE})",
            defaults.get("direct_xhttp_path", "/"),
            lambda value: xhttp_path(value, "direct_xhttp_path", allow_root=True),
        )
    if https == "caddy":
        cdns: list[dict[str, Any]] = list(defaults.get("cdn", ()))
        while questions.yes(
            f"cdn.{len(cdns)}.add", "Add another CDN XHTTP backend" if cdns else "Add a CDN XHTTP backend",
        ):
            index = len(cdns)
            key = f"cdn.{index}"
            cdns.append(
                {
                    "provider": questions.value(
                        f"{key}.provider", "Provider (selectel or yandex)", "selectel",
                        lambda value: _choice(value, ("selectel", "yandex"), "Choose selectel or yandex."),
                    ),
                    "edge": questions.value(
                        f"{key}.edge", "Public CDN domain", f"edge{index + 1 if index else ''}.{domain}",
                        lambda value: _hostname(value, "CDN edge"),
                    ),
                    "origin": questions.value(
                        f"{key}.origin", "Origin domain", f"node.{domain}",
                        lambda value: _hostname(value, "CDN origin"),
                    ),
                    "port": questions.port(f"{key}.port", "Local XHTTP port", 8080 + index),
                    "path": questions.value(
                        f"{key}.path", "XHTTP path, or generate", EXAMPLE, lambda value: xhttp_path(value, "CDN path"),
                    ),
                }
            )
        data["cdn"] = cdns
    data["nftguard"] = _ask_nftguard(questions, defaults.get("nftguard", {}))
    for field in ("reality_sni", "reality_target"):
        if field in defaults:
            data[field] = defaults[field]
    if choose_sni and (data["reality"] or data["direct_xhttp"]):
        data = validate_config(data, pending_sni=True)
        data.update(ask_reality_sni(data, questions=questions))
    return data
