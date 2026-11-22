"""Check DNS records for domains that point at the local server."""

import ipaddress
import json
import socket
from collections.abc import Iterable

from setup3ui.host.commands import run


def check_dns(domains: Iterable[str]) -> None:
    """Reject missing DNS or addresses that differ from known public host addresses."""
    interfaces = json.loads(run("ip", "-j", "address", "show", capture=True).stdout)
    local = {
        address
        for interface in interfaces
        for item in interface.get("addr_info", [])
        if (address := ipaddress.ip_address(item["local"])).is_global
    }
    for domain in dict.fromkeys(domains):
        try:
            records = {
                ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise ValueError(f"Could not resolve DNS for {domain}. Set its A or AAAA record first.") from exc
        for version in (4, 6):
            host_addresses = {address for address in local if address.version == version}
            dns_addresses = {address for address in records if address.version == version}
            if host_addresses and dns_addresses - host_addresses:
                actual = ", ".join(sorted(map(str, dns_addresses)))
                expected = ", ".join(sorted(map(str, host_addresses)))
                raise ValueError(f"{domain} has IPv{version} records {actual}. Server has {expected}. Check DNS.")
        print(f"DNS {domain}: {', '.join(sorted(map(str, records)))}")
