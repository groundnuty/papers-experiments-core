"""Record the shapes of the calls a per-run reset depends on: an eviction's request and answer, a transfer's record,
whether evicting a directory empties what a mirror holds of the files in it, and the space's transfer listing by state.

    uv run python deploy/compose-federation/measure_transfer_shapes.py [--layout PATH]

On the running federation, as its admin, in the study space: make `transfer-shapes/` through the data provider with
one file in it, replicate the file to the study space's first mirror and evict it from there (a file's eviction), then
replicate it again and evict the directory (the form `reset --replicas` uses), reading the mirror's block map after
each step. Then list the space's transfers on the data provider and on the mirror, `GET /spaces/{space}/transfers`
with each `state` (waiting, ongoing, ended) as the core's transfer log reads it, once with a page of one to make a
`nextPageToken` appear and follow it.

Each step prints one JSON line: the request's method and path template, its query and body as shapes, the status,
the answer's shape, and whether a `Date` header came with it. A shape is each field's name with its JSON type, a list
shown by its length and its first element's shape; a key that is not a field name (an id) is shown as `<id>`. No value
is printed, so no token, id or name. The scratch directory is removed at the end, and `reset --clean` removes anything
left.
"""

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from fedboot import Bootstrap
from fedcontent import Content
from fedlayout import BaselineFile, Layout
from fedrest import PROVIDER_API, Rest, bearer
from fedspec import Service, load_spec

from expcore_onedata.placement import held_bytes, provider_blocks

DIRECTORY = "transfer-shapes"
FILE = BaselineFile(f"{DIRECTORY}/one.bin", 256 << 10)
STATES = ("waiting", "ongoing", "ended")
ENDED = {"completed", "skipped", "failed", "cancelled", "aborted"}
STATE_FIELDS = ("transferStatus", "replicationStatus", "evictionStatus")
FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,23}")
ANY = tuple(range(200, 600))
WAIT_S = 180.0


def shape(value: Any) -> Any:
    """The value's structure with every value replaced by its JSON type, and every key that is not a field name by
    `<id>`: what a call's answer looks like, never what it holds."""
    if isinstance(value, dict):
        return {(key if FIELD.fullmatch(key) else "<id>"): shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"list_of": len(value), "first": shape(value[0]) if value else None}
    if value is None:
        return "null"
    return {bool: "bool", int: "int", float: "float", str: "str"}.get(type(value), type(value).__name__)


def show(step: str, **facts: Any) -> None:
    print(json.dumps({"step": step, **facts}), flush=True)


class Shapes:
    def __init__(self, rest: Rest, boot: Bootstrap) -> None:
        self.rest, self.boot = rest, boot
        self.spec = boot.layout.spec
        self.token = bearer(boot.admin_token())
        self.holder = self.spec.provider(self.spec.holder)
        mirrors = self.spec.space(self.spec.study_space).mirrors
        if not mirrors:
            raise RuntimeError("the study space has no mirror to evict from")
        self.mirror = self.spec.provider(mirrors[0])
        self.space_id = boot.ids.spaces[self.spec.study_space]

    def call(self, service: Service, method: str, path: str, template: str, step: str, **options: Any) -> Any:
        """Send one request, print its shapes and its answer's, and return the decoded answer (None when not JSON)."""
        began = time.monotonic()
        answer = self.rest.call(
            method, service.url + PROVIDER_API + path, label=f"{service.name} {template}", ok=ANY,
            headers={**self.token, **options.pop("headers", {})}, **options,
        )  # fmt: skip
        try:
            body = answer.json() if answer.content else None
        except ValueError:
            body = None
        role = "data provider" if service is self.holder else "mirror"
        show(
            step,
            via=role,
            request={
                "method": method,
                "path": template,
                "query": shape(options.get("params")),
                "body": shape(options.get("json")),
            },
            status=answer.status_code,
            answer=shape(body),
            date_header="date" in answer.headers,
            seconds=round(time.monotonic() - began, 2),
        )
        return body

    def wait_ended(self, service: Service, tid: str, step: str) -> None:
        """Poll a transfer's record on the provider it was posted to until it ends; print the final record's shape."""
        began = time.monotonic()
        while time.monotonic() - began < WAIT_S:
            record = self.boot.provider(service, "GET", f"/transfers/{tid}", "transfer")
            state = next((record[f] for f in STATE_FIELDS if isinstance(record.get(f), str)), None)
            if state in ENDED:
                self.call(service, "GET", f"/transfers/{tid}", "/transfers/{transfer}", f"{step}: its record, ended")
                show(f"{step}: ended", state=state, seconds=round(time.monotonic() - began, 1))
                return
            time.sleep(1)
        show(f"{step}: did not end", seconds=WAIT_S)

    def mirror_holds(self, file_id: str) -> int | None:
        """What the mirror holds of the file, in bytes, by its own block map; None when that is unknown."""
        answer = self.boot.provider(self.mirror, "GET", f"/data/{file_id}/distribution", "distribution")
        try:
            return held_bytes(provider_blocks(answer, self.boot.ids.providers[self.mirror.name]))
        except ValueError:
            return None

    def wait_held(self, file_id: str, wanted: int, step: str) -> None:
        """Poll the mirror's block map until it holds `wanted` bytes of the file; print what it held and when."""
        began, held = time.monotonic(), None
        while time.monotonic() - began < WAIT_S and (held := self.mirror_holds(file_id)) != wanted:
            time.sleep(1)
        show(step, mirror_holds_bytes=held, as_wanted=held == wanted, seconds=round(time.monotonic() - began, 1))

    def transfer(self, kind: str, service: Service, file_id: str, step: str) -> None:
        field = "replicatingProviderId" if kind == "replication" else "evictingProviderId"
        body = {"type": kind, field: self.boot.ids.providers[self.mirror.name], "dataSourceType": "file"}
        answer = self.call(service, "POST", "/transfers", "/transfers", step, json={**body, "fileId": file_id})
        tid = answer.get("transferId") if isinstance(answer, dict) else None
        if not isinstance(tid, str):
            raise RuntimeError(f"{step}: the answer holds no transferId")
        self.wait_ended(service, tid, step)

    def listing(self) -> None:
        path, template = f"/spaces/{self.space_id}/transfers", "/spaces/{space}/transfers"
        for service in (self.holder, self.mirror):
            for state in STATES:
                self.call(service, "GET", path, template, f"list {state}", params={"state": state, "limit": 100})
            one = {"state": "ended", "limit": 1}
            first = self.call(service, "GET", path, template, "list ended, a page of one", params=one)
            token = first.get("nextPageToken") if isinstance(first, dict) else None
            if isinstance(token, str) and token:
                params = {"state": "ended", "limit": 1, "page_token": token}
                self.call(service, "GET", path, template, "list ended, the next page", params=params)

    def run(self) -> None:
        content = Content(self.boot)
        root = content.root(self.holder, self.spec.study_space)
        directory = content.make(self.holder, root, DIRECTORY, "DIR")
        try:
            file_id = content.write(self.holder, directory, FILE)
            if file_id is None:
                raise RuntimeError("the scratch file could not be written")
            began = time.monotonic()
            while time.monotonic() - began < WAIT_S and self.mirror_holds(file_id) is None:
                time.sleep(1)
            show("the mirror sees the file", seconds=round(time.monotonic() - began, 1))
            self.transfer("replication", self.holder, file_id, "replicate the file")
            self.wait_held(file_id, FILE.size, "after the replication")
            self.transfer("eviction", self.mirror, file_id, "evict the file")
            self.wait_held(file_id, 0, "after the file's eviction")
            self.transfer("replication", self.holder, file_id, "replicate the file again")
            self.wait_held(file_id, FILE.size, "after the second replication")
            self.transfer("eviction", self.mirror, directory, "evict the directory")
            self.wait_held(file_id, 0, "after the directory's eviction")
            self.listing()
        finally:
            for entry in content.children(self.holder, directory):
                content.remove(self.holder, [entry["file_id"]])
            content.remove(self.holder, [directory])
            show("remove the scratch directory")


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
        Shapes(rest, boot).run()
    finally:
        rest.close()


if __name__ == "__main__":
    main()
