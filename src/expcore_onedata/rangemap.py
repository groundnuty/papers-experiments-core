# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/s2ranges.py, blob 37fc53e2da12e927b555b235388dc7c5eee4f61c
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The byte ranges of S2 a required extent needs, found by tracing the pipeline's own reader on the local verified copy.

Onedata's transfer jobs replicate whole files only, so the required extent of S2 (1,544 chunks of one 3.79 GB file) can
be placed only by reading those bytes through the receiving provider (on-the-fly replication). Which bytes: every
read the pipeline's reader makes for that instance, HDF5 metadata included, recorded from a file object with the same
xarray + h5netcdf calls as `insandbox.py`. Nothing is computed from HDF5 internals, so nothing can drift from the reader.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from .placement import merged


class TracingFile(io.RawIOBase):
    """A read-only file object recording every (offset, length) actually read."""

    def __init__(self, path: Path | str):
        super().__init__()
        self._f = open(path, "rb")
        self.reads: list[tuple[int, int]] = []

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._f.seek(offset, whence)

    def tell(self) -> int:
        return self._f.tell()

    def readinto(self, b) -> int:
        pos = self._f.tell()
        n = self._f.readinto(b)
        if n:
            self.reads.append((pos, n))
        return n

    def close(self) -> None:
        self._f.close()
        super().close()


def required_ranges(s2_path: Path | str, rec: dict, days: np.ndarray) -> list[tuple[int, int]]:
    """Merged (offset, length) ranges the pipeline's reader needs for this instance's box and days."""
    import xarray
    i = rec["instance"]
    f = TracingFile(s2_path)
    with xarray.open_dataset(f, engine="h5netcdf") as ds:
        for d in days:
            ds["analysed_sst"].isel(time=int(d), latitude=slice(i["lat0"], i["lat1"]),
                                    longitude=slice(i["lon0"], i["lon1"])).values   # noqa: B018
    reads = list(f.reads)
    f.close()
    return merged(reads)
