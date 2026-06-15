"""Monotonic sequence/revision counter for live-state mutations (PRD §19.1, §22).

Every applied change bumps this counter; the websocket stamps each delta with the
new value so clients can detect a gap and resynchronize (§22.5). A single asyncio
event loop owns the live state, so a plain integer needs no locking.
"""


class Sequence:
    def __init__(self, start: int = 0) -> None:
        self._value = start

    @property
    def current(self) -> int:
        return self._value

    def next(self) -> int:
        self._value += 1
        return self._value
