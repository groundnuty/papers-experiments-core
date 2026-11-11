"""Helpers for tests that a file is never read into memory whole."""

from pathlib import Path


def whole_reads(monkeypatch, *names: str) -> list[str]:
    """Record every `Path.read_bytes` of a file with one of `names`, the call that reads a file whole; the list the
    test asserts on fills as the code under test reads."""
    seen: list[str] = []
    real = Path.read_bytes

    def recording(self: Path) -> bytes:
        if self.name in names:
            seen.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", recording)
    return seen


def counting_open(monkeypatch, module, served: dict[str, int]) -> None:
    """Make `open` in `module` count, by file name, the bytes each `read` of an opened file returns."""
    opened = open

    def counting(file, mode="r", *args, **kwargs):
        handle = opened(file, mode, *args, **kwargs)
        reader = handle.read
        name = Path(file).name

        def read(size=-1):
            data = reader(size)
            served[name] = served.get(name, 0) + len(data)
            return data

        handle.read = read
        return handle

    monkeypatch.setattr(module, "open", counting, raising=False)
