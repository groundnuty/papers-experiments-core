"""Fakes for the lease tests: a clock that only moves when told, and a probe that answers from sets. No platform."""

from typing import Any


class Clock:
    """A clock that only moves when told to; `sleep` moves it and runs an optional hook."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.on_sleep = None
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()


class Probe:
    """Answers what a platform would: which holders are gone and which resources are shown gone."""

    def __init__(self, gone_owners: Any = (), gone_keys: Any = ()) -> None:
        self.gone_owners, self.gone_keys = set(gone_owners), set(gone_keys)
        self.asked: list[tuple[str, dict]] = []

    def holder_gone(self, owner: str) -> bool:
        return owner in self.gone_owners

    def resource_gone(self, key: str, note: dict) -> bool:
        self.asked.append((key, dict(note)))
        return key in self.gone_keys
