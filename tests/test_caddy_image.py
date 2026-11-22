"""Check image selection with a fake registry and Docker daemon."""

import hashlib
from contextlib import redirect_stdout
from io import StringIO
from subprocess import CalledProcessError
from unittest import TestCase
from unittest.mock import call, patch

from setup3ui import ASSETS
from setup3ui.deploy import caddy


class CaddyImageTests(TestCase):
    def setUp(self) -> None:
        self.recipe = (ASSETS / "Caddy.Dockerfile").read_bytes()
        digest = hashlib.sha256(self.recipe).hexdigest()
        self.published = f"ghcr.io/theillarion/easy-3x-ui-caddy:sha256-{digest}"
        command = patch.object(caddy, "docker")
        self.docker = command.start()
        self.addCleanup(command.stop)

    def test_fresh_install_reuses_local_image(self) -> None:
        caddy.prepare_caddy_image()
        self.docker.assert_called_once_with(
            "image", "inspect", caddy.CADDY_IMAGE, capture=True
        )

    def test_missing_local_image_uses_registry_without_building(self) -> None:
        self.docker.side_effect = [CalledProcessError(1, "inspect"), None, None]
        caddy.prepare_caddy_image()
        self.assertEqual(
            self.docker.call_args_list,
            [
                call("image", "inspect", caddy.CADDY_IMAGE, capture=True),
                call("pull", self.published, capture=True),
                call("image", "tag", self.published, caddy.CADDY_IMAGE),
            ],
        )

    def test_missing_registry_image_falls_back_to_current_recipe(self) -> None:
        self.docker.side_effect = [
            CalledProcessError(1, "inspect"),
            CalledProcessError(1, "pull"),
            None,
        ]
        output = StringIO()
        with redirect_stdout(output):
            caddy.prepare_caddy_image()
        self.assertIn("Building it locally", output.getvalue())
        self.assertEqual(
            self.docker.call_args,
            call(
                "build", "-t", caddy.CADDY_IMAGE, "-", input_text=self.recipe.decode()
            ),
        )

    def test_refresh_pulls_and_reuses_the_existing_compose_image_name(self) -> None:
        image_name = "setup-3x-ui-caddy:legacy"
        caddy.prepare_caddy_image(image_name, refresh=True)
        self.assertEqual(
            self.docker.call_args_list,
            [
                call("pull", self.published, capture=True),
                call("image", "tag", self.published, image_name),
            ],
        )

    def test_refresh_fallback_build_updates_base_image(self) -> None:
        image_name = "setup-3x-ui-caddy:legacy"
        self.docker.side_effect = [CalledProcessError(1, "pull"), None]
        with redirect_stdout(StringIO()):
            caddy.prepare_caddy_image(image_name, refresh=True)
        self.assertEqual(
            self.docker.call_args,
            call(
                "build",
                "--pull",
                "-t",
                image_name,
                "-",
                input_text=self.recipe.decode(),
            ),
        )
