"""Notice when the Pioreactor drops off the network, and when it comes back.

paho reports a disconnect at once and again on every failed reconnect attempt, so this keeps the
first moment the link went down and only calls `on_down` once it has stayed down for `grace_s`
(a Wi-Fi blip shouldn't page anyone). `on_up` follows when the link returns, with the downtime.
Call `check()` periodically; the callbacks run on the thread that calls check() or connected().
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class LinkWatch:
    def __init__(self, grace_s: float, on_down: Callable[[float], None], on_up: Callable[[float], None],
                 clock: Callable[[], float] = time.monotonic):
        self.grace_s = grace_s
        self.on_down, self.on_up, self.clock = on_down, on_up, clock
        self._ever_connected = False
        self._down_since: float | None = None
        self._alerted = False
        self._lock = threading.Lock()

    def connected(self) -> None:
        with self._lock:
            down_since, alerted = self._down_since, self._alerted
            self._ever_connected, self._down_since, self._alerted = True, None, False
        if alerted and down_since is not None:
            self.on_up(self.clock() - down_since)

    def disconnected(self) -> None:
        with self._lock:
            if self._ever_connected and self._down_since is None:
                self._down_since = self.clock()

    def check(self) -> None:
        with self._lock:
            if self._down_since is None or self._alerted:
                return
            down_for = self.clock() - self._down_since
            if down_for < self.grace_s:
                return
            self._alerted = True
        self.on_down(down_for)

    def start(self, every_s: float = 5.0) -> threading.Thread:
        """Run check() in a daemon thread."""
        def loop() -> None:
            while True:
                time.sleep(every_s)
                self.check()

        thread = threading.Thread(target=loop, name="larre-linkwatch", daemon=True)
        thread.start()
        return thread
