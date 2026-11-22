"""Check custom inbound templates without creating Docker containers."""

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import create_autospec, patch

from setup3ui import ASSETS
from setup3ui.config import validate_config
from setup3ui.panel import inbounds
from setup3ui.panel.api import PanelAPI


class InboundTemplateTests(TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.api = create_autospec(PanelAPI, instance=True)
        self.config = {"domain": "example.org", "email": "fixture@example.org"}
        self.private_key = "a" * 43
        self.public_key = "b" * 43
        keys = patch.object(
            inbounds,
            "docker",
            return_value=CompletedProcess(
                [],
                0,
                f"Private key: {self.private_key}\nPublic key: {self.public_key}\n",
            ),
        )
        self.keys = keys.start()
        self.addCleanup(keys.stop)

    def test_direct_xhttp_uses_custom_fields_and_runtime_values(self) -> None:
        copy = self.directory / "custom.json"
        data = json.loads((ASSETS / "templates" / "direct-xhttp.json").read_text())
        data.update(port=9999, remark="Personal XHTTP")
        data["settings"]["clients"] = [{"id": "ignored"}]
        data["streamSettings"]["xhttpSettings"].update(
            mode="stream-one", path="/ignored"
        )
        data["streamSettings"]["realitySettings"]["privateKey"] = "ignored"
        copy.write_text(json.dumps(data), encoding="utf-8")
        config = validate_config(
            self.config
            | {
                "direct_xhttp": True,
                "direct_xhttp_path": "/media/x",
                "reality_sni": ["tls.example.org"],
                "inbound_templates": {"direct_xhttp": str(copy)},
            }
        )
        with redirect_stdout(StringIO()):
            inbounds.create_inbounds(self.api, config, "fixture")
        _, created = self.api.call.call_args_list[0].args
        self.assertEqual(created["remark"], "Personal XHTTP")
        self.assertEqual(created["port"], 8443)
        self.assertEqual(created["tag"], "xray-xhttp-reality-8443")
        self.assertEqual(created["settings"]["clients"], [])
        stream = created["streamSettings"]
        self.assertEqual(
            stream["xhttpSettings"], {"mode": "stream-one", "path": "/media/x"}
        )
        self.assertEqual(stream["realitySettings"]["privateKey"], self.private_key)
        self.assertEqual(stream["realitySettings"]["serverNames"], ["tls.example.org"])
        self.assertEqual(
            stream["realitySettings"]["settings"]["publicKey"], self.public_key
        )
        self.assertEqual(len(stream["realitySettings"]["shortIds"]), 8)
        self.api.call.assert_called_with("panel/api/server/restartXrayService", {})

    def test_cdn_preserves_template_defaults_and_applies_yaml(self) -> None:
        copy = self.directory / "custom-yandex.json"
        data = json.loads((ASSETS / "templates" / "yandex.json").read_text())
        data["tag"] = "tag-from-template"
        data["streamSettings"]["xhttpSettings"]["serverMaxHeaderBytes"] = 32768
        copy.write_text(json.dumps(data), encoding="utf-8")
        config = validate_config(
            self.config
            | {
                "https": "caddy",
                "cdn": [
                    {
                        "provider": "yandex",
                        "edge": "edge.example.org",
                        "path": "/media/test",
                        "remark": "Home",
                    }
                ],
                "inbound_templates": {"yandex": str(copy)},
            }
        )
        with redirect_stdout(StringIO()):
            inbounds.create_inbounds(self.api, config, "fixture")
        _, created = self.api.call.call_args_list[0].args
        self.assertEqual(created["listen"], "127.0.0.1")
        self.assertEqual(created["remark"], "Home")
        self.assertEqual(created["tag"], "tag-from-template")
        self.assertEqual(created["settings"]["clients"], [])
        stream = created["streamSettings"]
        self.assertEqual(stream["xhttpSettings"]["path"], "/media/test")
        self.assertEqual(stream["xhttpSettings"]["serverMaxHeaderBytes"], 32768)
        self.assertEqual(stream["externalProxy"][0]["dest"], "edge.example.org")
        self.assertEqual(stream["externalProxy"][0]["remark"], "Home")
        self.keys.assert_not_called()

        data["tag"] = ""
        data["streamSettings"]["externalProxy"][0]["port"] = 9443
        copy.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(
            inbounds._cdn(config["cdn"][0], config)["tag"], "cdn-yandex-9443"
        )

    def test_explicit_cdn_tag_is_used_for_creation_only(self) -> None:
        from setup3ui.deploy.reconfigure import _sync_cdn

        backend = {
            "provider": "yandex",
            "edge": "edge.example.org",
            "path": "/media/test",
            "port": 8081,
        }
        self.assertEqual(inbounds._cdn(backend, {})["tag"], "cdn-yandex-443")
        inbound = inbounds._cdn(backend | {"tag": "explicit-tag"}, {})
        self.assertEqual(inbound["tag"], "explicit-tag")
        _sync_cdn(inbound, backend | {"tag": "other-tag", "path": "/media/new"})
        self.assertEqual(inbound["tag"], "explicit-tag")
        self.assertEqual(
            inbound["streamSettings"]["xhttpSettings"]["path"], "/media/new"
        )

    def test_multiple_backends_from_one_provider_get_distinct_external_port_tags(
        self,
    ) -> None:
        config = validate_config(
            self.config
            | {
                "https": "caddy",
                "cdn": [
                    {
                        "provider": "yandex",
                        "edge": "edge.example.org",
                        "path": "/media/first",
                    },
                    {
                        "provider": "yandex",
                        "edge": "edge.example.org",
                        "path": "/media/second",
                    },
                ],
            }
        )
        first, second = (
            inbounds._cdn(backend, config)["tag"] for backend in config["cdn"]
        )
        self.assertTrue(first.startswith("cdn-yandex-443-"))
        self.assertTrue(second.startswith("cdn-yandex-443-"))
        self.assertNotEqual(first, second)

    def test_bad_template_fails_before_key_generation_and_api_calls(self) -> None:
        copy = self.directory / "invalid.json"
        config = validate_config(
            self.config | {"reality": True, "inbound_templates": {"reality": str(copy)}}
        )
        missing_settings = {
            "protocol": "vless",
            "streamSettings": {"network": "tcp", "security": "reality"},
        }
        for payload, error in (
            ("invalid json", "Could not read inbound template reality"),
            ("[]", "must contain a JSON object"),
            ("{}", "must use VLESS"),
            (json.dumps(missing_settings), "missing a settings object"),
        ):
            copy.write_text(payload, encoding="utf-8")
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                inbounds.create_inbounds(self.api, config, "fixture")
        self.keys.assert_not_called()
        self.api.call.assert_not_called()

    def test_template_mapping_rejects_unknown_names_and_relative_paths(self) -> None:
        for templates, error in (
            ([], "must be a mapping"),
            ({"unknown": "/tmp/custom.json"}, "Unknown inbound_templates fields"),
            ({"reality": "custom.json"}, "must be an absolute path"),
        ):
            with (
                self.subTest(templates=templates),
                self.assertRaisesRegex(ValueError, error),
            ):
                validate_config(self.config | {"inbound_templates": templates})
