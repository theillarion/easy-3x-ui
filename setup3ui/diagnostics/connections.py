"""Check public HTTPS and test selected VLESS links with Xray."""

import base64
import binascii
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TypedDict
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import ProxyHandler, build_opener

DEFAULT_RESOURCES = ("https://www.youtube.com/", "https://www.google.com/", "https://telegram.org/")


class Result(TypedDict):
    connection: str
    resource: str
    available: bool
    status: int | None
    latency_ms: int


def _probe(url: str, connection: str, proxy: int | None = None) -> Result:
    endpoint = urlsplit(url)
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None:
        raise ValueError("Test resources must use HTTPS and have no username or password.")
    options = ["--proxy", f"socks5h://127.0.0.1:{proxy}", "--noproxy", ""] if proxy else ["--noproxy", "*"]
    start = time.monotonic()
    result = subprocess.run(
        [
            "curl",
            "--disable",
            "--silent",
            "--max-time",
            "12",
            "--output",
            os.devnull,
            "--write-out",
            "%{http_code}",
            *options,
            "--",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    code = int(result.stdout) if result.stdout.isdecimal() and result.stdout != "000" else None
    return {
        "connection": connection,
        "resource": endpoint.netloc,
        "available": result.returncode == 0 and code is not None and 200 <= code < 400,
        "status": code,
        "latency_ms": round((time.monotonic() - start) * 1000),
    }


def _links(text: str) -> tuple[list[str], int]:
    text = text.strip()
    if not text.startswith("vless://") and "://" not in text:
        encoded = "".join(text.split())
        try:
            text = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Could not read the subscription as links or base64.") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    links = list(dict.fromkeys(line for line in lines if line.startswith("vless://")))
    return links, sum(not line.startswith("vless://") for line in lines)


def _outbound(link: str) -> dict[str, Any]:
    try:
        uri = urlsplit(link)
        port = uri.port
    except ValueError as exc:
        raise ValueError("Invalid server address.") from exc
    if not uri.hostname or not uri.username or port is None:
        raise ValueError("Missing server, port or client ID.")
    query = {key: values[0] for key, values in parse_qs(uri.query).items()}
    transport = query.get("type", "tcp")
    security = query.get("security", "none")
    if transport not in {"tcp", "raw", "xhttp", "ws", "grpc"} or security not in {"none", "tls", "reality"}:
        raise ValueError(f"Unsupported transport or TLS mode ({transport}, {security}).")
    stream: dict[str, Any] = {"network": transport, "security": security}
    if security == "reality":
        if not query.get("pbk") or not query.get("sni"):
            raise ValueError("Missing REALITY public key or SNI.")
        stream["realitySettings"] = {
            "fingerprint": query.get("fp", "chrome"),
            "serverName": query["sni"],
            "password": query["pbk"],
            "shortId": query.get("sid", ""),
            "spiderX": query.get("spx", "/"),
        }
    elif security == "tls":
        stream["tlsSettings"] = {"serverName": query.get("sni", uri.hostname), "fingerprint": query.get("fp", "chrome")}
        if query.get("alpn"):
            stream["tlsSettings"]["alpn"] = query["alpn"].split(",")
    if transport == "xhttp":
        stream["xhttpSettings"] = {
            "host": query.get("host", ""),
            "path": query.get("path", "/"),
            "mode": query.get("mode", "auto"),
        }
        if query.get("extra"):
            extra = json.loads(query["extra"])
            if not isinstance(extra, dict):
                raise ValueError("Invalid XHTTP extra settings.")
            stream["xhttpSettings"]["extra"] = extra
    elif transport == "ws":
        stream["wsSettings"] = {"path": query.get("path", "/"), "host": query.get("host", "")}
    elif transport == "grpc":
        stream["grpcSettings"] = {
            "serviceName": query.get("serviceName", ""),
            "multiMode": query.get("mode") == "multi",
        }
    client = {"id": unquote(uri.username), "encryption": query.get("encryption", "none")}
    if query.get("flow"):
        client["flow"] = query["flow"]
    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": uri.hostname, "port": port, "users": [client]}]},
        "streamSettings": stream,
    }


@contextmanager
def _client(binary: str, outbound: dict[str, Any]) -> Iterator[int]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    configuration = {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"auth": "noauth"}}],
        "outbounds": [outbound],
    }
    with subprocess.Popen(
        [binary, "run", "-config", "stdin:"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        try:
            assert process.stdin is not None
            with process.stdin:
                process.stdin.write(json.dumps(configuration).encode())
            for _ in range(30):
                if process.poll() is not None:
                    raise ValueError("Xray could not start with this link.")
                try:
                    sock = socket.create_connection(("127.0.0.1", port), timeout=0.1)
                except OSError:
                    time.sleep(0.1)
                    continue
                with sock:
                    sock.sendall(b"\x05\x01\x00")
                    if sock.recv(2) != b"\x05\x00":
                        raise ValueError("The test SOCKS proxy did not start.")
                break
            else:
                raise ValueError("The test SOCKS proxy did not start.")
            yield port
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()


def _select(links: list[str], *, interactive: bool) -> list[tuple[str, str]]:
    names = []
    for index, link in enumerate(links, 1):
        name = unquote(link.partition("#")[2])
        name = re.sub(r"[^\x20-\x7EÀ-ÿА-яЁё]", "", name)[:80]
        names.append((f"{index}. {name or 'VLESS'}", link))
    if not interactive or len(links) < 2:
        return names
    for label, _ in names:
        print(label)
    choice = input("Connection numbers separated by commas, or all [all]: ").strip().lower()
    if choice in ("", "all"):
        return names
    indexes = choice.replace(" ", "").split(",")
    if not all(item.isdecimal() and 1 <= int(item) <= len(names) for item in indexes):
        raise ValueError("Invalid connection selection.")
    return [names[int(index) - 1] for index in dict.fromkeys(indexes)]


def test_connections(
    config: Mapping[str, Any] | None,
    link_file: Path | None,
    resources: list[str],
    xray: str,
    json_output: bool,
    *,
    download: bool = False,
) -> None:
    """Check direct access, each requested connection, and configured public endpoints."""
    results: list[Result] = []
    skipped = 0
    failed: list[dict[str, str]] = []
    if config:
        is_caddy = config["https"] == "caddy"
        for label in ("panel", "subscription"):
            port = "" if is_caddy else f":{config[f'{label}_port']}"
            item = _probe(f"https://{config[f'{label}_domain']}{port}/", label)
            # A hidden panel or subscription can send 404 at the root URL.
            item["available"] = item["available"] or item["status"] == 404
            results.append(item)
        public = {(label, backend[label]) for backend in config["cdn"] for label in ("origin", "edge")}
        for label, domain in sorted(public):
            results.append(_probe(f"https://{domain}/", label))

    if link_file:
        text = link_file.read_text(encoding="utf-8").strip()
    elif os.isatty(0) and not json_output:
        text = getpass.getpass("VLESS link or HTTPS subscription URL (empty to skip): ").strip()
    else:
        text = ""
    links: list[str] = []
    if text:
        if text.startswith("https://"):
            try:
                with build_opener(ProxyHandler({})).open(text, timeout=15) as response:
                    data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise ValueError("Subscription is too large.")
                text = data.decode()
            except (OSError, ValueError) as exc:
                raise ValueError("Could not load the subscription.") from exc
        links, skipped = _links(text)

    urls = resources or list(DEFAULT_RESOURCES)
    results.extend(_probe(url, "direct") for url in urls)
    with TemporaryDirectory(prefix="setup-3x-ui-xray-") as stage:
        if links and download and shutil.which(xray) is None:
            from setup3ui.diagnostics.xray import download_xray

            xray = download_xray(Path(stage))
        for label, link in _select(links, interactive=os.isatty(0) and not json_output):
            try:
                outbound = _outbound(link)
                with _client(xray, outbound) as port_number:
                    results.extend(_probe(url, label, port_number) for url in urls)
            except (OSError, ValueError):
                failed.append({"connection": label, "error": "Could not run this VLESS connection."})

    if json_output:
        print(json.dumps({"checks": results, "unsupported_entries": skipped, "errors": failed}, indent=2))
        return
    for item in results:
        status = f"HTTP {item['status']}" if item["status"] else "no HTTPS response"
        state = "available" if item["available"] else "unavailable"
        print(f"{item['connection']} | {item['resource']} | {state} | {status} | {item['latency_ms']} ms")
    if skipped:
        print(f"Skipped {skipped} unsupported subscription entries.")
    for failure in failed:
        print(f"{failure['connection']} | {failure['error']}")
