"""Check the YAML contract and the generated parts of the config."""

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest import TestCase
from unittest.mock import patch

from setup3ui.config import (
    XHTTP_EXAMPLE,
    port_plan,
    read_config,
    use_saved_paths,
    validate_config,
)


class ConfigTests(TestCase):
    base: ClassVar[dict[str, str]] = {
        "domain": "EXAMPLE.ORG",
        "email": "test@example.org",
    }
    edge: ClassVar[dict[str, str]] = {"provider": "yandex", "edge": "edge.example.org"}

    def test_defaults_and_caddy_ports(self) -> None:
        standalone = validate_config(
            self.base | {"direct_xhttp": True, "reality": True}
        )
        self.assertEqual(standalone["panel_domain"], "master.example.org")
        self.assertEqual(standalone["subscription_domain"], "gateway.example.org")
        self.assertEqual(standalone["direct_xhttp_path"], "/")
        self.assertEqual(port_plan(standalone)["TCP REALITY"], 443)
        self.assertNotIn("Caddy HTTPS", port_plan(standalone))

        caddy = validate_config(
            self.base | {"https": "caddy", "reality": True, "cdn": [self.edge]}
        )
        self.assertEqual(caddy["reality_address"], "example.org")
        self.assertEqual(
            caddy["cdn"],
            [
                self.edge
                | {"origin": "node.example.org", "port": 8080, "path": XHTTP_EXAMPLE}
            ],
        )
        self.assertEqual(port_plan(caddy)["TCP REALITY"], 8444)
        self.assertEqual(port_plan(caddy)["Caddy HTTPS"], 443)

    def test_invalid_config(self) -> None:
        cases = (
            ({"https": "letsencrypt"}, "https must"),
            ({"install_dir": 42}, "absolute path"),
            ({"install_dir": "./3x-ui"}, "absolute path"),
            ({"docker_user": "root"}, "regular Linux user"),
            ({"docker_user": "2test"}, "regular Linux user"),
            ({"admin_username": []}, "non-empty string"),
            ({"reality_tag": " "}, "non-empty string"),
            ({"direct_xhttp_tag": "abc\ndef"}, "non-empty string"),
            ({"reality": 1}, "true or false"),
            ({"domain": "https://example.org"}, "domain name"),
            ({"subscription_domain": "master.example.org"}, "must be different"),
            ({"email": "invalid"}, "valid ACME contact"),
            ({"cdn": {}}, "cdn must be a list"),
            ({"cdn": [self.edge]}, "require Caddy"),
            ({"reality_sni": "example.org"}, "non-empty list"),
            ({"reality_sni": []}, "non-empty list"),
            ({"reality_sni": [None]}, "domain name"),
            (
                {
                    "https": "caddy",
                    "reality": True,
                    "reality_sni": ["gateway.example.org"],
                },
                "site names",
            ),
            ({"direct_xhttp_path": "/media?foo=bar"}, "absolute URL path"),
            ({"direct_xhttp_path": "/media/../foo"}, "absolute URL path"),
            ({"direct_xhttp_path": 1}, "absolute URL path"),
            ({"reality_target": 443}, "domain and port"),
            ({"reality_target": "example.org"}, "domain and port"),
            ({"reality_target": "example.org:https"}, "valid port"),
            ({"reality_target": "example.org:65536"}, "valid port"),
            ({"action": "update"}, "YAML action"),
            ({"confirm": "yes"}, "confirm must"),
            ({"backup_file": 12}, "backup_file must"),
            ({"backup_file": "db/x-ui.db"}, "backup_file must"),
            ({"action": "reset", "backup_file": "/tmp/x-ui.db"}, "Use restore instead"),
            ({"panel_port": 65536}, "requires a port"),
            ({"panel_port": True}, "requires a port"),
            ({"panel_port": 2096}, "assigned to Panel and Subscriptions"),
        )
        for changes, error in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, error),
            ):
                validate_config(self.base | changes)
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            validate_config(None)

    def test_invalid_cdn(self) -> None:
        cases = (
            (["yandex"], "must be a mapping"),
            ([self.edge | {"provider": "unknown"}], "provider must"),
            ([self.edge | {"remark": 21}], "non-empty string"),
            ([self.edge | {"tag": " "}], "non-empty string"),
            ([self.edge | {"origin": "master.example.org"}], "must differ"),
            ([self.edge | {"path": "/"}], "absolute URL path"),
            ([self.edge | {"path": 1}], "absolute URL path"),
            ([self.edge, self.edge], "overlaps"),
            (
                [self.edge | {"path": "/media"}, self.edge | {"path": "/media/clip"}],
                "overlaps",
            ),
            (
                [self.edge | {"path": "/media/clip"}, self.edge | {"path": "/media"}],
                "overlaps",
            ),
        )
        for backends, error in cases:
            with (
                self.subTest(backends=backends),
                self.assertRaisesRegex(ValueError, error),
            ):
                validate_config(self.base | {"https": "caddy", "cdn": backends})

    def test_force_only_drops_unknown_fields(self) -> None:
        stderr = StringIO()
        raw = self.base | {
            "https": "caddy",
            "old": False,
            "cdn": [self.edge | {"old": True}],
        }
        with self.assertRaisesRegex(ValueError, "Unknown config fields"):
            validate_config(raw)
        with redirect_stderr(stderr):
            clean = validate_config(raw, force=True)
        self.assertIn("Ignoring unknown config fields: old.", stderr.getvalue())
        self.assertIn("Ignoring unknown cdn[0] fields: old.", stderr.getvalue())
        self.assertNotIn("old", clean)
        self.assertNotIn("old", clean["cdn"][0])
        with redirect_stderr(stderr), self.assertRaisesRegex(ValueError, "valid ACME"):
            validate_config(raw | {"email": "invalid"}, force=True)

    def test_reapply_generated_paths(self) -> None:
        raw = self.base | {
            "https": "caddy",
            "direct_xhttp": True,
            "direct_xhttp_path": "generate",
            "cdn": [self.edge | {"path": "generate"}],
            "reality_sni": ["WWW.Cloudflare.com", "www.cloudflare.com"],
            "reality_target": "WWW.Cloudflare.com:443",
        }
        with patch(
            "setup3ui.config.secrets.token_hex",
            side_effect=["12345678", "aaaaaaaa", "87654321", "bbbbbbbb"],
        ):
            old = validate_config(raw)
            new = validate_config(raw)
        self.assertNotEqual(old["direct_xhttp_path"], new["direct_xhttp_path"])
        self.assertNotEqual(old["cdn"][0]["path"], new["cdn"][0]["path"])
        use_saved_paths(None, old, new)
        use_saved_paths(raw, old, new)
        self.assertEqual(old, new)
        self.assertEqual(old["reality_sni"], ["www.cloudflare.com"])
        self.assertEqual(old["reality_target"], "www.cloudflare.com:443")

    def test_read_yaml_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yml"
            cases = (
                (
                    "domain: example.org\ndomain: example.net\n",
                    "Duplicate field 'domain' at line 2",
                ),
                ("true: example.org\n", "Expected a field name at line 1"),
                ("domain: [example.org", "Invalid YAML at line"),
            )
            for content, error in cases:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, error):
                        read_config(path, False)

            path.write_text(
                "domain: example.org\nemail: test@example.org\naction: restore\n"
                "backup_file: /tmp/x-ui.db\nconfirm: true\n",
                encoding="utf-8",
            )
            config = read_config(path, False)
            self.assertEqual(config["panel_domain"], "master.example.org")
            for key in ("action", "backup_file", "confirm"):
                self.assertNotIn(key, config)
