"""Run commands on the local host."""

import subprocess
from collections.abc import Mapping


def run(
    *args: str, capture: bool = False, env: Mapping[str, str] | None = None, input_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a system command and report a failed exit."""
    return subprocess.run(
        args, check=True, text=True, capture_output=capture, env=env, input=input_text, timeout=timeout,
    )
