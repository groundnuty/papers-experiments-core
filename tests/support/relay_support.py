"""Test support for how the fault relay's client waits for an answer: a model of an NFS client's negative lookup cache,
and the relay's setup (the federation's root, the relay, the verbs behind it) as the relay's tests build it."""

import errno
import io
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fault_support import SERVICES, FakeClock
from hpc_support import write_state

from expcore_onedata.compose import CommandResult
from expcore_onedata.fault_relay import Relay

SERVICE = SERVICES["beta"]
PREFIX = ["/opt/venv/bin/python", "hpcfed.py", "--root", "ROOT", "--layout", "LAYOUT"]
ACDIRMIN_S = 30.0  # the NFS client's default `acdirmin`: how long a directory's cached attributes stay valid


class Verbs:
    """The federation's verbs behind the relay: every command recorded, each answered `answer`."""

    def __init__(self, answer: CommandResult | None = None) -> None:
        self.answer = answer or CommandResult(0, json.dumps({"running": True, "identity": "1:4001:901"}) + "\n")
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], *, timeout_s: float) -> CommandResult:
        self.calls.append((list(argv), timeout_s))
        return self.answer


def root_of(tmp_path: Path) -> Path:
    """A federation's root with its state file, which lists the providers' services."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    write_state(root, tuple(SERVICES.values()))
    return root


def no_watch(argv: Sequence[str], env: Mapping[str, str]) -> Any:
    """The watch for a timed-out verb's late steps, which the tests that use this setup do not run."""
    return type("Handle", (), {"poll": staticmethod(lambda: 0)})()


def relay_of(root: Path, verbs: Verbs) -> Relay:
    return Relay(
        root, PREFIX, runner=verbs, environ={"PATH": "/usr/bin:/bin", "HOME": "/home/someone"}, watcher=no_watch
    )


def serving(relay: Relay, clock: FakeClock) -> Callable[[float], None]:
    """A client's pause between two looks for its answer, in which the relay answers what waits."""

    def pause(seconds: float) -> None:
        clock.sleep(seconds)
        relay.serve_once()

    return pause


class _Listing:
    """What `os.scandir` returns: iterable and a context manager, over entries read when the directory was opened."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        return iter(self._entries)

    def __enter__(self) -> "_Listing":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def close(self) -> None:
        return None


class NegativeLookupCache:
    """What a compute node's NFS client does with a name that another host creates in a directory it watches.

    A lookup by path (`os.stat`, `os.lstat`, `os.open`, `io.open`: whatever `Path.stat`, `is_symlink` and `read_text`
    call) of a name directly under `watched` that does not exist yet leaves a negative entry, and every later lookup of
    that name fails with ENOENT, the file there or not, until the entry is dropped: by a listing of `watched` (an
    `opendir` revalidates the directory's attributes, which drops its stale negative entries) or when the directory's
    attributes time out, `ttl_s` of `clock` after the first miss (`acdirmin`, 30 s by default).

    It records what a client may not do: `premature` holds every lookup by path of a name that no listing of `watched`
    had shown (a creating `os.open` aside, which is no lookup of an existing file), and `listings` the names each
    listing of `watched` showed. Nothing else is changed: paths outside `watched`, file descriptors and every other
    call reach the real file system.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        watched: Path,
        clock: Callable[[], float],
        *,
        ttl_s: float = ACDIRMIN_S,
    ) -> None:
        self.watched, self._clock, self._ttl_s = os.fspath(watched), clock, ttl_s
        self.missing: dict[str, float] = {}  # name: the clock's time at its first miss
        self.shown: set[str] = set()
        self.premature: list[tuple[str, str]] = []
        self.listings: list[list[str]] = []
        for owner, name in ((os, "stat"), (os, "lstat"), (io, "open")):
            monkeypatch.setattr(owner, name, self._by_path(name, getattr(owner, name)))
        monkeypatch.setattr(os, "open", self._opened(os.open))
        monkeypatch.setattr(os, "scandir", self._listed(os.scandir))

    def _name(self, path: Any) -> str | None:
        """The name of `path` when it is directly under the watched directory, else None."""
        if isinstance(path, int):
            return None
        text = os.fspath(path)
        return os.path.basename(text) if isinstance(text, str) and os.path.dirname(text) == self.watched else None

    def _lookup(self, call: str, real: Callable[..., Any], path: Any, *args: Any, **kwargs: Any) -> Any:
        name = self._name(path)
        if name is None:
            return real(path, *args, **kwargs)
        if name not in self.shown:
            self.premature.append((call, name))
        first = self.missing.get(name)
        if first is not None and self._clock() - first < self._ttl_s:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(path))
        self.missing.pop(name, None)
        try:
            return real(path, *args, **kwargs)
        except FileNotFoundError:
            self.missing[name] = self._clock()
            raise

    def _by_path(self, call: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            return self._lookup(call, real, path, *args, **kwargs)

        return wrapper

    def _opened(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
            if flags & os.O_CREAT:  # a create makes the name, here as on the host that creates it: no lookup
                self.missing.pop(self._name(path) or "", None)
                return real(path, flags, *args, **kwargs)
            return self._lookup("open", real, path, flags, *args, **kwargs)

        return wrapper

    def _listed(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(path: Any = None) -> Any:
            if path is None or os.fspath(path) != self.watched:
                return real() if path is None else real(path)
            with real(path) as entries:
                found = list(entries)
            self.missing.clear()
            names = sorted(entry.name for entry in found)
            self.shown.update(names)
            self.listings.append(names)
            return _Listing(found)

        return wrapper
