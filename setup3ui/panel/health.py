"""Check the locally served panel and subscription URLs."""

import http.client
import socket
import ssl
import time


def verify_https(domain: str, port: int, path: str, expected: set[int]) -> None:
    """Wait for a local HTTPS response from the panel or subscription service."""
    context = ssl.create_default_context()
    last_error = "Connection not attempted."
    for _ in range(45):
        response = None
        try:
            with (
                socket.create_connection(("127.0.0.1", port), timeout=2) as connection,
                context.wrap_socket(connection, server_hostname=domain) as tls,
            ):
                tls.sendall(f"GET {path} HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode())
                response = http.client.HTTPResponse(tls)
                response.begin()
                if response.status in expected:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, http.client.HTTPException) as exc:
            last_error = str(exc)
        finally:
            if response is not None:
                response.close()
        time.sleep(1)
    raise ValueError(f"Local HTTPS check failed for {domain}:{port}: {last_error}")
