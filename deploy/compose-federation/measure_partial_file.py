"""Measure a file at its final size with only a prefix written, through the providers' REST content endpoint.

    uv run python deploy/compose-federation/measure_partial_file.py [--layout PATH]

On the running federation, as its admin, in the study space: create `ingest-partial.bin` through the data provider,
write its first 64 KiB, set its size to 1 MiB with an empty write at offset 1 MiB, and read it back through the data
provider and through the study space's first mirror, by range and whole. Each step prints one JSON line: the status,
the headers that matter, the lengths, whether the prefix's bytes and the unwritten range's bytes are what they should
be, the block map per provider, and the seconds; never a token or an id. Whether the data gate would stream an answer
is `gateboundary.streams` of its status and type. The file is removed at the end, and reset removes anything left.
"""

import argparse
import json
import time
from pathlib import Path

from fedboot import Bootstrap
from fedcontent import Content
from fedlayout import Layout
from fedrest import PROVIDER_API, Rest, bearer
from fedspec import load_spec

from expcore_onedata.sandbox import gateboundary

NAME = "ingest-partial.bin"
SIZE = 1 << 20
PREFIX = 64 << 10
ANY = tuple(range(200, 600))
DATA = bytes((i * 7 + 3) % 251 + 1 for i in range(PREFIX))  # no zero byte, so a zero read back is not data


def show(step: str, **facts) -> None:
    print(json.dumps({"step": step, **facts}), flush=True)


class Probe:
    def __init__(self, rest: Rest, boot: Bootstrap) -> None:
        self.rest, self.boot = rest, boot
        self.token = bearer(boot.admin_token())
        self.names = {pid: name for name, pid in boot.ids.providers.items()}
        spec = boot.layout.spec
        self.spec = spec
        self.holder = spec.provider(spec.holder)
        mirrors = spec.space(spec.study_space).mirrors
        if not mirrors:
            raise RuntimeError("the study space has no mirror to read the file through")
        self.remote = spec.provider(mirrors[0])
        self.file_id = ""

    def call(self, service, method, path, **options):
        url = f"{service.url}{PROVIDER_API}{path}"
        return self.rest.call(method, url, label=f"{service.name} {method}", ok=ANY, **options)

    def size(self, service) -> int | None:
        answer = self.call(service, "GET", f"/data/{self.file_id}", headers=self.token, params={"attribute": "size"})
        return answer.json().get("size") if answer.status_code == 200 else None

    def blocks(self, step: str) -> None:
        """The file's size and every provider's blocks and physical size, as the data provider answers."""
        answer = self.call(self.holder, "GET", f"/data/{self.file_id}/distribution", headers=self.token)
        per = {}
        for pid, entry in answer.json().get("distributionPerProvider", {}).items():
            storages = list(entry.get("distributionPerStorageBackend", {}).values())
            per[self.names.get(pid, "other")] = {
                "virtualSize": entry.get("virtualSize"),
                "blocks": [s.get("blocks") for s in storages],
                "physicalSize": [s.get("physicalSize") for s in storages],
            }
        show(step, size=self.size(self.holder), distribution_status=answer.status_code, per_provider=per)

    def read(self, service, step: str, first: int | None = None, last: int | None = None) -> None:
        headers = {**self.token, **({"Range": f"bytes={first}-{last}"} if first is not None else {})}
        began = time.monotonic()
        answer = self.call(service, "GET", f"/data/{self.file_id}/content", headers=headers)
        seconds = round(time.monotonic() - began, 2)
        body, start = answer.content, first or 0
        ok = answer.status_code < 300
        written = body[: max(0, PREFIX - start)] if ok else b""
        unwritten = body[max(0, PREFIX - start) :] if ok else b""
        error = answer.json().get("error", {}) if not ok and body else {}
        show(
            step,
            via=service.name,
            range=None if first is None else [first, last],
            status=answer.status_code,
            content_type=answer.headers.get("content-type"),
            content_range=answer.headers.get("content-range"),
            body_bytes=len(body),
            length_is_size=len(body) == SIZE if first is None and ok else None,
            prefix_bytes_match=written == DATA[start : start + len(written)] if written else None,
            unwritten_bytes=len(unwritten),
            unwritten_all_zero=unwritten.count(0) == len(unwritten) if unwritten else None,
            error=error.get("id"),
            errno=(error.get("details") or {}).get("errno"),
            gate_streams=gateboundary.streams(answer.status_code, "content", answer.headers.get("content-type")),
            seconds=seconds,
        )

    def run(self) -> None:
        holder, remote = self.holder, self.remote
        root = Content(self.boot).root(holder, self.spec.study_space)
        made = self.call(
            holder, "POST", f"/data/{root}/children", headers=self.token, params={"name": NAME, "type": "REG"}
        )
        self.file_id = made.json()["fileId"]
        show("create", via=holder.name, status=made.status_code, file=NAME)
        octets = {**self.token, "Content-Type": "application/octet-stream"}
        put = self.call(
            holder, "PUT", f"/data/{self.file_id}/content", headers=octets, params={"offset": 0}, content=DATA
        )
        show("write the prefix", status=put.status_code, offset=0, bytes=PREFIX)
        self.blocks("after the prefix")
        grow = self.call(
            holder, "PUT", f"/data/{self.file_id}/content", headers=octets, params={"offset": SIZE}, content=b""
        )
        show("set the final size: an empty write at that offset", status=grow.status_code, offset=SIZE, bytes=0)
        self.blocks("after the empty write")
        self.read(holder, "unwritten range", PREFIX, PREFIX + 4095)
        self.read(holder, "across the prefix's end", PREFIX - 16, PREFIX + 15)
        self.read(holder, "whole, by range", 0, SIZE - 1)
        self.read(holder, "whole, no range")
        self.read(remote, "before the other provider sees the file", 0, 4095)
        began, seen = time.monotonic(), None
        while time.monotonic() - began < 120 and (seen := self.size(remote)) != SIZE:
            time.sleep(1)
        show(
            "the other provider sees the file at its size",
            via=remote.name,
            seen=seen,
            seconds=round(time.monotonic() - began, 1),
        )
        self.read(remote, "prefix, other provider", 0, 4095)
        self.read(remote, "unwritten range, other provider", PREFIX, PREFIX + 4095)
        self.read(remote, "whole, no range, other provider")
        self.blocks("after the reads")
        gone = self.call(holder, "DELETE", f"/data/{self.file_id}", headers=self.token)
        show("remove", status=gone.status_code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--layout", type=Path, help="the layout file (default: federation.toml)")
    layout = Layout.from_environment(spec=load_spec(parser.parse_args().layout))
    rest = Rest(layout.ca_pem)
    try:
        boot = Bootstrap(rest, layout)
        boot.ensure_spaces()
        for service in layout.spec.providers:
            url = service.url + PROVIDER_API + "/configuration"
            boot.ids.providers[service.name] = rest.json("GET", url, label="configuration")["providerId"]
        Probe(rest, boot).run()
    finally:
        rest.close()


if __name__ == "__main__":
    main()
