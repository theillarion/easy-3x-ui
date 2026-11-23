"""Render the interactive installer and REALITY checks in a terminal."""

import os
import shutil
import sys
import termios
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from threading import Event, Lock, Thread
from time import monotonic

from setup3ui.host.reality import ProbeResult


class Terminal:
    """Render text that also works in redirected output and narrow windows."""

    def __init__(self) -> None:
        self.stream = sys.stdout
        self.live = self.stream.isatty() and os.environ.get("TERM", "dumb") != "dumb"
        self.color = self.live and not os.environ.get("NO_COLOR")

    @property
    def width(self) -> int:
        """Return the current width for tables and live output."""
        return shutil.get_terminal_size().columns

    def style(self, text: str, code: str) -> str:
        """Apply an ANSI style when color is enabled."""
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def line(self, text: str = "") -> None:
        """Write a complete, visible line."""
        print(text, file=self.stream, flush=True)

    def heading(self, label: str) -> None:
        """Start a wizard section."""
        self.line(f"\n{self.style(label, '1;36')}")

    def pairs(self, fields: Mapping[str, str]) -> None:
        """Print the final settings as aligned labels and values."""
        width = max(map(len, fields))
        for label, value in fields.items():
            self.line(f"  {label:<{width}}  {value}")

    def box(self, title: str, lines: Sequence[str]) -> None:
        """Frame a result when the terminal can show each line in full."""
        width = max(len(title) + 1, max(map(len, lines), default=0))
        if not self.live or width + 4 > self.width:
            for line in lines:
                self.line(line)
            return
        self.line()
        self.line(self.style(f"╭─ {title} {'─' * (width - len(title) - 1)}╮", "1;32"))
        for line in lines:
            self.line(f"│ {line:<{width}} │")
        self.line(self.style(f"╰{'─' * (width + 2)}╯", "1;32"))

    def probe_results(self, rows: list[ProbeResult], *, numbered: bool) -> None:
        """Show successful checks and leave failed checks out of the choices."""
        passed = [row for row in rows if row.probe.milliseconds is not None]
        if numbered and passed:
            index_width = len(str(len(passed)))
            host_width = max(4, max(len(row.probe.name) for row in passed))
            tls_width = max(len(f"{row.probe.milliseconds} ms") for row in passed)
            ping_width = max(4, max(len(row.ping) for row in passed))
            header = (f"  {'#':>{index_width}}  {'Host':<{host_width}}  {'TLS':>{tls_width}}  "
                      f"{'Ping':>{ping_width}}  Result")
            if len(header) <= self.width:
                self.line(header)
                for index, (probe, ping) in enumerate(passed, 1):
                    tls = f"{probe.milliseconds} ms"
                    self.line(f"  {index:>{index_width}}  {probe.name:<{host_width}}  {tls:>{tls_width}}  "
                              f"{ping:>{ping_width}}  {self.style('Passed', '32')}")
            else:
                for index, (probe, ping) in enumerate(passed, 1):
                    self.line(f"{index}. {probe.name}")
                    self.line(f"   {self.style('Passed', '32')} · TLS {probe.milliseconds} ms · Ping {ping}")
        elif passed:
            for probe, ping in passed:
                self.line(f"{probe.name}: {self.style('Passed', '32')} · TLS {probe.milliseconds} ms · Ping {ping}")
        for probe, _ in rows:
            if probe.milliseconds is None:
                self.line(f"{probe.name}: {self.style('Failed', '31')} · {probe.error}.")


class ScanDisplay:
    """Observe existing checks and keep their waiting message up to date."""

    def __init__(self, terminal: Terminal) -> None:
        self.terminal = terminal
        self.rows: list[ProbeResult] = []
        self._label = ""
        self._detail = ""
        self._visible = ""
        self._started = 0.0
        self._lock = Lock()
        self._stop = Event()

    @contextmanager
    def track(self) -> Iterator[None]:
        """Stop updating and clear the status before control returns to the wizard."""
        worker = Thread(target=self._tick) if self.terminal.live else None
        try:
            if worker is not None:
                worker.start()
            yield
        finally:
            self._stop.set()
            if worker is not None and worker.ident is not None:
                worker.join()
            self._clear()
            if sys.stdin.isatty():
                try:
                    termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
                except termios.error:
                    # The terminal may have disconnected while the check ran.
                    pass

    def status(self, label: str, detail: str = "") -> None:
        """Show the current phase and any measured count."""
        with self._lock:
            if label != self._label:
                self._started = monotonic()
                if not self.terminal.live:
                    self.terminal.line(f"{label} ({detail})..." if detail else f"{label}...")
            self._label = label
            self._detail = detail
            if self.terminal.live:
                self._draw()

    def notice(self, message: str) -> None:
        """Print a message that stays visible when the phase ends."""
        self._clear()
        self.terminal.line(message)

    def results(self, rows: list[ProbeResult]) -> None:
        """Keep the measurements until the wizard is ready to show them."""
        self.rows = rows

    def _tick(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                if self._label:
                    self._draw()

    def _draw(self) -> None:
        suffix = f"  {self._detail}" if self._detail else ""
        suffix += f"  {int(monotonic() - self._started)}s"
        width = self.terminal.width
        label = self._label[:max(0, width - len(suffix) - 1)]
        line = (label + suffix)[:max(1, width - 1)]
        if line != self._visible:
            self.terminal.stream.write(f"\r\x1b[2K{line}")
            self.terminal.stream.flush()
            self._visible = line

    def _clear(self) -> None:
        with self._lock:
            if self._visible:
                self.terminal.stream.write("\r\x1b[2K")
                self.terminal.stream.flush()
                self._visible = ""
            self._label = ""
