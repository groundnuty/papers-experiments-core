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
    """Answers what a platform would: which holders are gone, which cannot be told about, and which resources are gone.

    `unknown_owners` answer None (cannot tell); `failing_owners` and `failing_keys` raise, as a probe that cannot look.
    """

    def __init__(
        self, gone_owners: Any = (), gone_keys: Any = (), unknown_owners: Any = (), failing_owners: Any = (),
        failing_keys: Any = (),
    ) -> None:  # fmt: skip
        self.gone_owners, self.gone_keys = set(gone_owners), set(gone_keys)
        self.unknown_owners, self.failing_owners, self.failing_keys = (
            set(unknown_owners),
            set(failing_owners),
            set(failing_keys),
        )
        self.asked: list[tuple[str, dict]] = []

    def holder_gone(self, owner: str) -> bool | None:
        if owner in self.failing_owners:
            raise OSError("cannot look at the holder")
        if owner in self.unknown_owners:
            return None
        return owner in self.gone_owners

    def resource_gone(self, key: str, note: dict) -> bool:
        self.asked.append((key, dict(note)))
        if key in self.failing_keys:
            raise OSError("cannot look at the resource")
        return key in self.gone_keys
