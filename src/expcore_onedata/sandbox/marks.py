# Provenance: new
"""Marker lines taken out of a command's output stream, as it is read (O55).

A cell's completion record (`completion.py`) is one line on each of the cell's streams, written after the cell's own
output: the marker, which only that exec knows, then the record's payload, then a newline. `take` finds every such
line in what has been read so far and returns the rest of the bytes, so whoever counts and caps the stream counts and
caps only the command's own bytes; the payloads go to `found`. Bytes that may still begin a marker line (a prefix of
the marker, or a marker whose newline has not come) are held back until the next read decides them. At the stream's
end (`final`) everything held is output: a marker cut before its newline is no record.
"""

MAX_PAYLOAD_BYTES = 4096  # a marker line longer than this is no record: its bytes are the command's own output


def _prefix_held(data: bytes, marker: bytes) -> int:
    """How many bytes at the end of `data` are a proper prefix of the marker, and so may begin one."""
    for size in range(min(len(data), len(marker) - 1), 0, -1):
        if data.endswith(marker[:size]):
            return size
    return 0


def take(data: bytes, marker: bytes, found: list[bytes], *, final: bool) -> tuple[bytes, bytes]:
    """Split `data` into the output it holds and the bytes to hold back for the next read, taking every whole marker
    line out of it and appending its payload to `found`. With `final`, nothing is held back."""
    out = bytearray()
    while True:
        start = data.find(marker)
        if start < 0:
            held = 0 if final else _prefix_held(data, marker)
            out += data[: len(data) - held]
            return bytes(out), data[len(data) - held :]
        end = data.find(b"\n", start + len(marker), start + len(marker) + MAX_PAYLOAD_BYTES + 1)
        if end >= 0:
            found.append(data[start + len(marker) : end])
            out += data[:start]
            data = data[end + 1 :]
            continue
        if final or len(data) - start > len(marker) + MAX_PAYLOAD_BYTES:  # no line end can make it a record now
            out += data[: start + 1]
            data = data[start + 1 :]
            continue
        out += data[:start]
        return bytes(out), data[start:]


__all__ = ["MAX_PAYLOAD_BYTES", "take"]
