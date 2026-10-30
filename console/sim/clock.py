"""Virtual time shared by the engine, the emulators and the code under test."""

from __future__ import annotations

import threading


class VirtualClock:
    """A clock that only moves when told to.

    Pass ``clock.now`` as ``clock=`` and ``clock.sleep`` as ``sleep=`` to the
    client and rotator: a retry backoff then advances virtual time instead of
    blocking, so a simulated day runs in seconds.
    """

    def __init__(self, start: float):
        self._t = float(start)
        self._lock = threading.Lock()

    def now(self) -> float:
        return self._t

    __call__ = now

    def advance_to(self, t: float) -> None:
        """Move forward to t; never backwards (a sleep may already have passed it)."""
        with self._lock:
            if t > self._t:
                self._t = float(t)

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))
