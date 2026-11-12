"""Measure what a range read places when it starts inside bytes the receiver already holds, when it is longer than
1 MiB, and when reads follow one another.

    uv run python deploy/compose-federation/measure_read_windows.py [--layout PATH]

Built on `measure_granularity_and_eviction.py`, which found that one range read through the receiver places a 1 MiB
window from the read's own offset, clipped to the file's end. On the running federation, as its admin, in the study
space, never through the core's provisioner; a fresh file for every case, written through the data provider and read
through the receiver (the study space's first mirror), whose block map is read from the data provider's distribution
answer:

1. Overlap. A fresh 8 MiB file; a 1 MiB read at offset 0 holds [0, 1 MiB); then 4 KiB at offset 512 KiB (wholly inside
   what is held) on one file, 4 KiB at offset 1 MiB - 1 (one held byte, then unheld ones) on another, and, as a control,
   4 KiB at an offset aligned to nothing in the middle of the file (no held byte) on a third. The ranges the receiver's
   block map gains are printed, as the ranges the later map covers and the earlier one did not.
2. Length. A fresh 8 MiB file each, one read of 1.5 MiB and one of 3 MiB at an offset aligned to nothing: exactly the
   read's length, or rounded up.
3. Sequence. A fresh 64 MiB file each, three reads of 4 KiB and three of 64 KiB, one after the other from offset 0,
   each settled before the next: does the window grow.

After every read the block map is read until two reads in a row agree, then once more `LATE_S` later, so a fetch that
lands late (a read-ahead) is not taken for none. Each step prints one JSON line; never a token or an id. Every file is
removed at the end.
"""

import time

from fedcontent import Content
from measure_granularity_and_eviction import KIB, MIB, Probe, main, offset_for, show

SMALL, LARGE = 8 * MIB, 64 * MIB
LATE_S = 5.0


def merged(ranges: list[list[int]]) -> list[list[int]]:
    """The ranges as sorted [start, length] pairs, touching or overlapping ones joined."""
    out: list[list[int]] = []
    for start, length in sorted(ranges):
        if out and start <= out[-1][0] + out[-1][1]:
            out[-1][1] = max(out[-1][1], start + length - out[-1][0])
        else:
            out.append([start, length])
    return out


def gained(before: list[list[int]], after: list[list[int]]) -> list[list[int]]:
    """The [start, length] ranges `after` covers that `before` did not."""
    held = merged(before)
    out: list[list[int]] = []
    for start, length in merged(after):
        cursor, end = start, start + length
        for held_start, held_length in held:
            if held_start + held_length <= cursor or held_start >= end:
                continue
            if held_start > cursor:
                out.append([cursor, held_start - cursor])
            cursor = max(cursor, held_start + held_length)
        if cursor < end:
            out.append([cursor, end - cursor])
    return out


class Windows(Probe):
    root = ""

    def fresh(self, size: int, name: str) -> str:
        """A new file written through the data provider that the receiver sees; its id. Each name is used once."""
        file_id = self.write(self.root, f"{name}-{len(self.made)}-{size}.bin", size)
        self.seen(self.receiver, file_id, size)
        return file_id

    def read(self, file_id: str, offset: int, length: int) -> dict:
        """One range read through the receiver, then its block map once it has settled and `LATE_S` after."""
        answer = self.call(
            self.receiver,
            "GET",
            f"/data/{file_id}/content",
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        )
        blocks, settle = self.settled(self.receiver, file_id)
        time.sleep(LATE_S)
        later = self.blocks(self.receiver, file_id)
        return {
            "read_bytes": length,
            "read_offset": offset,
            "status": answer.status_code,
            "body_bytes": len(answer.content),
            "receiver_holds": blocks,
            "receiver_holds_bytes": sum(n for _, n in blocks),
            "settled_after_s": settle,
            "changed_after_settle": later != blocks,
            "receiver_holds_late": later,
        }

    def overlap(self) -> None:
        # the last offset is the control: a second read that starts in no held byte at all
        for offset in (512 * KIB, MIB - 1, offset_for(SMALL, 4 * KIB)):
            file_id = self.fresh(SMALL, "overlap")
            held = self.read(file_id, 0, MIB)
            show("a read that holds the first MiB", file_bytes=SMALL, **held)
            again = self.read(file_id, offset, 4 * KIB)
            show(
                "a second read on a file whose first MiB is held",
                file_bytes=SMALL,
                held_before=held["receiver_holds_late"],
                gained=gained(held["receiver_holds_late"], again["receiver_holds_late"]),
                **again,
            )

    def length(self) -> None:
        for length in (3 * MIB // 2, 3 * MIB):
            file_id = self.fresh(SMALL, "length")
            offset = offset_for(SMALL, length)
            got = self.read(file_id, offset, length)
            show("a read longer than 1 MiB", file_bytes=SMALL, gained=gained([], got["receiver_holds_late"]), **got)

    def sequence(self) -> None:
        for length in (4 * KIB, 64 * KIB):
            file_id = self.fresh(LARGE, "sequence")
            before: list[list[int]] = []
            for number in range(3):
                got = self.read(file_id, number * length, length)
                show(
                    "a sequential read",
                    file_bytes=LARGE,
                    number=number + 1,
                    gained=gained(before, got["receiver_holds_late"]),
                    **got,
                )
                before = got["receiver_holds_late"]

    def run(self) -> None:
        self.root = Content(self.boot).root(self.spec.provider(self.holder), self.spec.study_space)
        try:
            self.overlap()
            self.length()
            self.sequence()
        finally:
            for file_id in self.made:
                self.call(self.holder, "DELETE", f"/data/{file_id}")
            show("removed", files=len(self.made))


if __name__ == "__main__":
    main(Windows, __doc__)
