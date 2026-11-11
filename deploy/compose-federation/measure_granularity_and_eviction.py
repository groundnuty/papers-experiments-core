"""Measure what a range read places on a provider that lacks the bytes, and which eviction empties it again.

    uv run python deploy/compose-federation/measure_granularity_and_eviction.py

On the running federation, as its admin, in the study space, never through the core's provisioner:

1. Granularity. For each file size (64 KiB, 8 MiB, 64 MiB) and each read size (1 B, 4 KiB, 1 MiB) a fresh file is
   written through the data provider, the receiver is waited for until it sees it, and one range is read through the
   receiver at an offset that is aligned to nothing (`offset_for`). Once the receiver's block map has given the same
   answer twice in a row, its blocks are printed: the placed ranges, the bytes placed, and whether they start and end
   on a multiple of 4 KiB, 64 KiB and 1 MiB.
2. Eviction. Each of those files, now partly held by the receiver, is evicted from it by one `POST /transfers`
   (`evictingProviderId` naming the receiver), alternately scheduled through the receiver itself and through the data
   provider, each only after the block map has settled. The transfer's state and `filesEvicted` are printed, and the
   receiver's block map is read for 30 s after the transfer ends: emptied or not, and when.

Each step prints one JSON line; never a token or an id. Every file is removed at the end.
"""

import json
import time

from fedboot import Bootstrap
from fedcontent import Content, provider
from fedlayout import HOLDER, PROVIDERS, STUDY_SPACE, Layout
from fedrest import PROVIDER_API, Rest, bearer

RECEIVER = "birch"
KIB, MIB = 1 << 10, 1 << 20
FILE_SIZES = (64 * KIB, 8 * MIB, 64 * MIB)
READ_SIZES = (1, 4 * KIB, 1 * MIB)
WRITE_CHUNK = 4 * MIB
CHUNK = bytes((i % 251) + 1 for i in range(WRITE_CHUNK))  # no zero byte, so a zero read back is not data
ANY = tuple(range(200, 600))
BOUND_S, POLL_S = 120.0, 0.5
ENDED = ("completed", "failed", "cancelled", "skipped")


def show(step: str, **facts) -> None:
    print(json.dumps({"step": step, **facts}), flush=True)


def offset_for(size: int, length: int) -> int:
    """An offset near the middle of the file that is a multiple of no power of two above 1, so a placed range that
    starts on a boundary was widened by the provider, not asked for."""
    offset = size // 2 + 4097
    return max(0, min(offset, size - length)) | 1


def aligned(ranges: list[list[int]], unit: int) -> bool:
    return all(start % unit == 0 and ((start + length) % unit == 0) for start, length in ranges)


class Probe:
    def __init__(self, rest: Rest, boot: Bootstrap) -> None:
        self.rest, self.boot = rest, boot
        self.token = bearer(boot.admin_token())
        self.made: list[str] = []

    def call(self, name: str, method: str, path: str, **options):
        url = f"{provider(name).url}{PROVIDER_API}{path}"
        headers = {**self.token, **options.pop("headers", {})}
        return self.rest.call(method, url, label=f"{name} {method}", ok=ANY, headers=headers, **options)

    def write(self, root: str, name: str, size: int) -> str:
        file_id = self.call(HOLDER, "POST", f"/data/{root}/children", params={"name": name, "type": "REG"}).json()[
            "fileId"
        ]
        self.made.append(file_id)
        octets = {"Content-Type": "application/octet-stream"}
        for offset in range(0, size, WRITE_CHUNK):
            body = CHUNK[: min(WRITE_CHUNK, size - offset)]
            answer = self.call(
                HOLDER, "PUT", f"/data/{file_id}/content", params={"offset": offset}, content=body, headers=octets
            )
            if answer.status_code >= 300:
                raise RuntimeError(f"a write answered {answer.status_code}")
        return file_id

    def seen(self, name: str, file_id: str, size: int) -> float:
        began = time.monotonic()
        while time.monotonic() - began < BOUND_S:
            answer = self.call(name, "GET", f"/data/{file_id}", params={"attribute": "size"})
            if answer.status_code == 200 and answer.json().get("size") == size:
                return round(time.monotonic() - began, 1)
            time.sleep(POLL_S)
        raise RuntimeError(f"{name} did not see a new file in time")

    def blocks(self, name: str, file_id: str) -> list[list[int]]:
        answer = self.call(HOLDER, "GET", f"/data/{file_id}/distribution").json()
        pid = self.boot.ids.providers[name]
        entry = answer.get("distributionPerProvider", {}).get(pid, {})
        out: list[list[int]] = []
        for storage in (entry.get("distributionPerStorageBackend") or {}).values():
            out += [list(b) for b in (storage or {}).get("blocks") or []]
        return sorted(out)

    def settled(self, name: str, file_id: str) -> tuple[list[list[int]], float]:
        """The receiver's blocks once two reads in a row agree, and the seconds it took."""
        began, last = time.monotonic(), None
        while time.monotonic() - began < BOUND_S:
            now = self.blocks(name, file_id)
            if now == last:
                return now, round(time.monotonic() - began, 1)
            last = now
            time.sleep(1.0)
        return last or [], round(time.monotonic() - began, 1)

    def evict(self, via: str, file_id: str) -> dict:
        body = {
            "type": "eviction",
            "dataSourceType": "file",
            "fileId": file_id,
            "evictingProviderId": self.boot.ids.providers[RECEIVER],
        }
        tid = self.call(via, "POST", "/transfers", json=body).json()["transferId"]
        began = time.monotonic()
        record: dict = {}
        while time.monotonic() - began < BOUND_S:
            record = self.call(via, "GET", f"/transfers/{tid}").json()
            if record.get("transferStatus") in ENDED:
                break
            time.sleep(POLL_S)
        ended = round(time.monotonic() - began, 1)
        emptied_after = None
        for tick in range(61):
            if not self.blocks(RECEIVER, file_id):
                emptied_after = round(tick * 0.5, 1)
                break
            time.sleep(0.5)
        return {
            "transfer": record.get("transferStatus"),
            "filesEvicted": record.get("filesEvicted"),
            "transfer_ended_after_s": ended,
            "receiver_empty_after_s": emptied_after,
            "receiver_holds_after": sum(length for _, length in self.blocks(RECEIVER, file_id)),
        }

    def run(self) -> None:
        root = Content(self.boot).root(provider(HOLDER), STUDY_SPACE)
        placed = []
        try:
            for size in FILE_SIZES:
                for length in READ_SIZES:
                    file_id = self.write(root, f"granularity-{size}-{length}.bin", size)
                    waited = self.seen(RECEIVER, file_id, size)
                    offset = offset_for(size, length)
                    answer = self.call(
                        RECEIVER,
                        "GET",
                        f"/data/{file_id}/content",
                        headers={"Range": f"bytes={offset}-{offset + length - 1}"},
                    )
                    blocks, settle = self.settled(RECEIVER, file_id)
                    show(
                        "a range read through the receiver",
                        file_bytes=size,
                        read_bytes=length,
                        read_offset=offset,
                        status=answer.status_code,
                        body_bytes=len(answer.content),
                        receiver_saw_file_after_s=waited,
                        placed=blocks,
                        placed_bytes=sum(b[1] for b in blocks),
                        settled_after_s=settle,
                        covers_read=any(s <= offset and offset + length <= s + n for s, n in blocks),
                        aligned_4k=aligned(blocks, 4 * KIB),
                        aligned_64k=aligned(blocks, 64 * KIB),
                        aligned_1m=aligned(blocks, MIB),
                    )
                    placed.append(file_id)
            for index, file_id in enumerate(placed):
                via = RECEIVER if index % 2 == 0 else HOLDER
                self.settled(RECEIVER, file_id)
                show("an eviction from the receiver", scheduled_through=via, **self.evict(via, file_id))
        finally:
            for file_id in self.made:
                self.call(HOLDER, "DELETE", f"/data/{file_id}")
            show("removed", files=len(self.made))


def main() -> None:
    layout = Layout.from_environment()
    rest = Rest(layout.ca_pem)
    try:
        boot = Bootstrap(rest, layout)
        boot.ensure_spaces()
        for service in PROVIDERS:
            url = service.url + PROVIDER_API + "/configuration"
            boot.ids.providers[service.name] = rest.json("GET", url, label="configuration")["providerId"]
        Probe(rest, boot).run()
    finally:
        rest.close()


if __name__ == "__main__":
    main()
