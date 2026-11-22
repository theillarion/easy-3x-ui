"""Call the local 3x-ui API using an authenticated session."""

import http.client
import json
import socket
import ssl
from http.cookies import SimpleCookie
from typing import Any


class _LoopbackHTTPS(http.client.HTTPSConnection):
    def __init__(self, domain: str, port: int) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(domain, port, context=self.tls_context, timeout=10)

    def connect(self) -> None:
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=self.host)
        except OSError:
            self.sock.close()
            raise


class PanelAPI:
    def __init__(self, domain: str, port: int, path: str) -> None:
        self.connection = _LoopbackHTTPS(domain, port)
        self.path = path
        self.cookies: SimpleCookie = SimpleCookie()
        self.headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

    def call(self, endpoint: str, body: dict[str, Any] | None = None) -> object:
        headers = self.headers.copy()
        if self.cookies:
            headers["Cookie"] = "; ".join(cookie.OutputString(attrs=[]) for cookie in self.cookies.values())
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            self.connection.request("GET" if body is None else "POST", self.path + endpoint, data, headers)
            response = self.connection.getresponse()
            with response:
                result = response.read()
                if response.status != 200:
                    raise ValueError(f"3x-ui API {endpoint} returned HTTP {response.status}.")
                for key, value in response.getheaders():
                    if key.lower() == "set-cookie":
                        self.cookies.load(value)
            message = json.loads(result)
            if not isinstance(message, dict) or message.get("success") is not True:
                raise ValueError(
                    f"3x-ui rejected {endpoint}: {message.get('msg', 'unknown error') if isinstance(message, dict) else 'unknown error'}"
                )
            return message.get("obj")
        except (http.client.HTTPException, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read the 3x-ui response for {endpoint}.") from exc
        finally:
            self.connection.close()

    def login(self, username: str, password: str, two_factor: str = "") -> None:
        self._csrf()
        self.call("login", {"username": username, "password": password, "twoFactorCode": two_factor})
        self._csrf()

    def needs_two_factor(self) -> bool:
        self._csrf()
        return self.call("getTwoFactorEnable", {}) is True

    def _csrf(self) -> None:
        token = self.call("csrf-token")
        if not isinstance(token, str):
            raise ValueError("3x-ui did not return a CSRF token.")
        self.headers["X-CSRF-Token"] = token
