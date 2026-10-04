"""A transient status line on stderr, so slow NextDNS responses don't look like a hang.

Only drawn when stderr is a terminal; scripts and pipes see nothing. Anything else
written to stderr while it's active (warnings, info) clears it first.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional, TextIO

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
TICK = 0.1
SHOW_ELAPSED_AFTER = 2.0


class StatusLine:
    def __init__(self, stream: Optional[TextIO] = None):
        self.stream = stream or sys.stderr
        self.enabled = _isatty(self.stream)
        self._lock = threading.RLock()
        self._message: Optional[str] = None
        self._note: Optional[str] = None
        self._started = 0.0
        self._visible_from = 0.0
        self._paused = False
        self._frame = 0
        self._drawn = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def active(self) -> bool:
        return self._message is not None

    def start(self, message: str, delay: float = 0.0) -> None:
        """Show `message`; with `delay`, only once it has been running that long."""
        with self._lock:
            self._message, self._note, self._started = message, None, time.monotonic()
            self._visible_from = self._started + delay
            if self.enabled and self._thread is None:
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def update(self, message: str) -> None:
        """Change the message; the elapsed time keeps counting from start()."""
        with self._lock:
            if self._message is not None:
                self._message, self._note = message, None

    def note(self, text: str) -> None:
        """A temporary remark after the message, e.g. a rate-limit wait."""
        with self._lock:
            self._note = text

    def stop(self) -> None:
        thread = self._thread
        self._stop.set()
        if thread is not None:
            thread.join()
        with self._lock:
            self._thread = None
            self._message = self._note = None
            self._clear()

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Don't draw at all while something else owns the line (e.g. a progress bar)."""
        with self._lock:
            self._clear()
            self._paused = True
        try:
            yield
        finally:
            with self._lock:
                self._paused = False

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Clear the line while something else writes to the terminal."""
        with self._lock:
            self._clear()
            yield

    def _run(self) -> None:
        while not self._stop.wait(TICK):
            self._draw()

    def _draw(self) -> None:
        with self._lock:
            if self._message is None or self._paused or time.monotonic() < self._visible_from:
                return
            self._frame = (self._frame + 1) % len(FRAMES)
            text = f"{FRAMES[self._frame]} {self._message}"
            elapsed = time.monotonic() - self._started
            if elapsed >= SHOW_ELAPSED_AFTER:
                text += f" ({elapsed:.0f}s)"
            if self._note:
                text += f" · {self._note}"
            width = shutil.get_terminal_size((80, 20)).columns - 1
            self.stream.write("\r\033[K" + text[:width])
            self.stream.flush()
            self._drawn = True

    def _clear(self) -> None:
        if self._drawn:
            self.stream.write("\r\033[K")
            self.stream.flush()
            self._drawn = False


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


status = StatusLine()


@contextmanager
def working(message: str) -> Iterator[StatusLine]:
    """Show `message` with a spinner while the block runs."""
    status.start(message)
    try:
        yield status
    finally:
        status.stop()


class RequestIndicator:
    """Client hooks: show "Waiting for NextDNS" while a request is slow, unless a more
    specific status is already showing."""

    DELAY = 0.5

    def __init__(self, line: StatusLine):
        self.line = line
        self._lock = threading.Lock()
        self._in_flight = 0
        self._owns_line = False

    def started(self, method: str, path: str) -> None:
        with self._lock:
            self._in_flight += 1
            if not self.line.active:
                self.line.start("Waiting for NextDNS", delay=self.DELAY)
                self._owns_line = True

    def finished(self) -> None:
        with self._lock:
            self._in_flight -= 1
            if self._in_flight == 0 and self._owns_line:
                self._owns_line = False
                self.line.stop()


request_indicator = RequestIndicator(status)


class StatusAwareHandler(logging.StreamHandler):
    """Prints warnings above the status line; shows routine notes (rate-limit waits) in it."""

    def __init__(self, verbose: bool):
        super().__init__(sys.stderr)
        self.verbose = verbose

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not self.verbose:
            if status.active:
                status.note(record.getMessage())
            return
        with status.suspended():
            super().emit(record)
