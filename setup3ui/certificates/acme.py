"""Issue a certificate for the panel and subscription domains."""

import fcntl
import os
import ssl
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from setup3ui.host.docker import docker
from setup3ui.host.preflight import tcp_listeners


def issue_certificate(install_dir: Path, panel_domain: str, subscription_domain: str, email: str) -> tuple[Path, Path]:
    """Use HTTP-01 and return the paths to the certificate and private key."""
    if os.geteuid() != 0:
        raise ValueError("Run issue-cert with sudo.")
    install_dir = install_dir.resolve()
    install_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    cert_dir = install_dir / "certs" / "live" / panel_domain
    chain, key = cert_dir / "fullchain.pem", cert_dir / "privkey.pem"
    with certificate_lock(install_dir):
        for port, address, owners in tcp_listeners():
            if port == 80:
                owner = ", ".join(owners) if owners else "owner unavailable"
                raise ValueError(f"TCP port 80 is in use at {address} by {owner}. Certbot needs this port.")
        for name in ("certs", "certbot-work", "certbot-log"):
            (install_dir / name).mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="setup-3x-ui-acme-") as temporary_dir:
            compose_file = install_dir / "compose.yml"
            if not compose_file.is_file():
                from setup3ui.deploy.compose import render_compose

                compose_file = Path(temporary_dir) / "compose.yml"
                definition = render_compose({"https": "certbot", "nftguard": {"enabled": False}})
                compose_file.write_text(definition, encoding="utf-8")
            docker(
                "compose",
                "--project-directory",
                str(install_dir),
                "-f",
                str(compose_file),
                "run",
                "--rm",
                "--no-deps",
                "--pull",
                "missing",
                "-T",
                "certbot",
                "certonly",
                "--standalone",
                "--non-interactive",
                "--agree-tos",
                "--keep-until-expiring",
                "--renew-with-new-domains",
                "--cert-name",
                panel_domain,
                "--email",
                email,
                "-d",
                panel_domain,
                "-d",
                subscription_domain,
            )
        check_certificate(chain, key)
    return chain, key


def check_certificate(chain: Path, key: Path) -> None:
    """Check that the certificate and its key can be loaded together."""
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(chain, key)
    except OSError as exc:
        raise ValueError(f"Could not load the certificate and key from {chain.parent}: {exc}") from exc


@contextmanager
def certificate_lock(install_dir: Path) -> Iterator[None]:
    """Allow only one Certbot operation per installation."""
    fd = os.open(install_dir / ".certificate.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another certificate operation is running.") from None
        yield
