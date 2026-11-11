"""Measure the distribution of a space's root directory, and one file's distribution read, on the running federation.

    uv run python deploy/compose-federation/measure_dir_distribution.py

As its admin, in the study space, without the core's reader (which takes a file's answer only):

1. `GET /data/{space root}/distribution` through each provider: the answer's shape, every field with its type, the
   providers by name and the storage backends as `<storage>`.
2. The truth to compare it with: the sum, per provider, of every regular file's `physicalSize`, from each file's own
   `GET /data/{file}/distribution` through the data provider (the space is walked whole, page by page).
3. One placement and one eviction of a known file on a provider that does not hold it, through `POST /transfers`, each
   waited to its end. From that moment the root's distribution is read through each provider until its per-provider
   physical sizes equal the truth moved by the file's own new block map, within a bound, and the seconds until they
   did are printed; the whole truth is then counted again to show it is what was expected. The directory-statistics
   service's state per provider (`supportParametersRegistry` of the zone's `GET /spaces/{space}`) is printed beside.
   The transfer's end is seen by polling its record every half second, so a lag can hide up to that much.
4. 200 reads of one file's `GET /data/{file}/distribution`, at concurrency 1 and 4, through the provider that holds the
   file and through one that does not: the median and the 95th percentile.

Each step prints one JSON line; never a token or an id. The space ends as it began: the evicted copy is gone again.
"""

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from fedboot import Bootstrap
from fedcontent import Content, provider
from fedlayout import HOLDER, PROVIDERS, STUDY_SPACE, Layout
from fedrest import PROVIDER_API, Rest, bearer

PLACED_FILE = ("samples", "sample-1.bin")  # held by the data provider alone in the baseline
RECEIVER = "cedar"  # supports the study space and holds none of the baseline files
LATENCY_FILE = "hello.txt"
NON_OWNER = "birch"
READS, WORKERS = 200, (1, 4)
BOUND_S, POLL_S = 180.0, 0.5
ANY = tuple(range(200, 600))


def show(step: str, **facts) -> None:
    print(json.dumps({"step": step, **facts}), flush=True)


def shape(value, names: dict[str, str]):
    """Every field of an answer with its type; a provider id becomes its name and any other id `<storage>`."""
    if isinstance(value, dict):
        return {names.get(k, "<storage>" if len(k) > 30 else k): shape(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0], names)] if value else []
    return type(value).__name__


def physical(entry: dict) -> int:
    """A provider's physical bytes in a distribution entry: the sum over its storage backends."""
    return sum((s or {}).get("physicalSize") or 0 for s in (entry.get("distributionPerStorageBackend") or {}).values())


class Probe:
    def __init__(self, rest: Rest, boot: Bootstrap) -> None:
        self.rest, self.boot = rest, boot
        self.token = bearer(boot.admin_token())
        self.names = {pid: name for name, pid in boot.ids.providers.items()}
        self.space = boot.ids.spaces[STUDY_SPACE]

    def call(self, name: str, method: str, path: str, **options):
        url = f"{provider(name).url}{PROVIDER_API}{path}"
        return self.rest.call(method, url, label=f"{name} {method}", ok=ANY, headers=self.token, **options)

    def entries(self, name: str, directory: str) -> list[dict]:
        out, params = [], {"limit": 1000, "attribute": ["file_id", "name", "type"]}
        while True:
            page = self.call(name, "GET", f"/data/{directory}/children", params=params).json()
            out += page["children"]
            if page.get("isLast", True):
                return out
            params = {**params, "token": page["nextPageToken"]}

    def files(self, directory: str, prefix: str = "") -> dict[str, str]:
        found = {}
        for entry in self.entries(HOLDER, directory):
            path = f"{prefix}{entry['name']}"
            if entry["type"] == "DIR":
                found.update(self.files(entry["file_id"], path + "/"))
            elif entry["type"] == "REG":
                found[path] = entry["file_id"]
        return found

    def per_provider(self, answer: dict) -> dict[str, dict]:
        out = {}
        for pid, entry in (answer.get("distributionPerProvider") or {}).items():
            out[self.names.get(pid, "other")] = {
                "physicalSize": physical(entry),
                "logicalSize": entry.get("logicalSize"),
                "virtualSize": entry.get("virtualSize"),
                "success": entry.get("success"),
            }
        return out

    def truth(self, files: dict[str, str]) -> dict[str, int]:
        sums = dict.fromkeys(self.boot.ids.providers, 0)
        for file_id in files.values():
            answer = self.call(HOLDER, "GET", f"/data/{file_id}/distribution").json()
            for name, entry in self.per_provider(answer).items():
                sums[name] = sums.get(name, 0) + entry["physicalSize"]
        return sums

    def timed_read(self, via: str, path: str) -> tuple[float, int]:
        began = time.perf_counter()
        status = self.call(via, "GET", path).status_code
        return (time.perf_counter() - began) * 1000, status

    def root_view(self, root: str) -> dict[str, dict[str, int]]:
        """Each provider's answer for the root, reduced to every provider's physical size."""
        return {
            via.name: {
                name: e["physicalSize"]
                for name, e in self.per_provider(
                    self.call(via.name, "GET", f"/data/{root}/distribution").json()
                ).items()
            }
            for via in PROVIDERS
        }

    def dir_stats_service(self) -> dict:
        record = self.boot.zone("GET", f"/spaces/{self.space}", "space")
        return {
            self.names.get(pid, "other"): params
            for pid, params in (record.get("supportParametersRegistry") or {}).items()
        }

    def transfer(self, body: dict) -> str:
        tid = self.call(HOLDER, "POST", "/transfers", json=body).json()["transferId"]
        began = time.monotonic()
        while time.monotonic() - began < BOUND_S:
            record = self.call(HOLDER, "GET", f"/transfers/{tid}").json()
            state = record.get("transferStatus")
            if state in ("completed", "failed", "cancelled", "skipped"):
                return state
            time.sleep(POLL_S)
        return "not ended in the bound"

    def lag(self, step: str, root: str, expected: dict[str, int]) -> None:
        began, matched, last = time.monotonic(), {}, {}
        while time.monotonic() - began < BOUND_S and len(matched) < len(PROVIDERS):
            for via, view in self.root_view(root).items():
                last[via] = view
                if via not in matched and all(view.get(n) == expected[n] for n in expected):
                    matched[via] = round(time.monotonic() - began, 1)
            time.sleep(POLL_S)
        show(
            step,
            expected_physical=expected,
            matched_after_s=matched,
            unmatched={v: last[v] for v in last if v not in matched},
            dir_stats_service=self.dir_stats_service(),
        )

    def latency(self, file_id: str) -> None:
        for via in (HOLDER, NON_OWNER):
            path = f"/data/{file_id}/distribution"
            self.call(via, "GET", path)  # one warm-up read, not counted
            for workers in WORKERS:
                began = time.perf_counter()
                with ThreadPoolExecutor(workers) as pool:
                    results = list(pool.map(lambda _, v=via, p=path: self.timed_read(v, p), range(READS)))
                wall = time.perf_counter() - began
                times = sorted(t for t, _ in results)
                show(
                    "one file's distribution read",
                    call=f"GET {PROVIDER_API}/data/{{file}}/distribution",
                    via=via,
                    holds_the_file=via == HOLDER,
                    concurrency=workers,
                    reads=READS,
                    statuses=sorted({s for _, s in results}),
                    median_ms=round(statistics.median(times), 2),
                    p95_ms=round(times[round(0.95 * (len(times) - 1))], 2),
                    max_ms=round(times[-1], 2),
                    reads_per_s=round(READS / wall),
                )

    def run(self) -> None:
        root = Content(self.boot).root(provider(HOLDER), STUDY_SPACE)
        for via in PROVIDERS:
            answer = self.call(via.name, "GET", f"/data/{root}/distribution")
            show(
                "the space root's distribution: shape",
                via=via.name,
                status=answer.status_code,
                shape=shape(answer.json(), self.names),
            )
        files = self.files(root)
        truth = self.truth(files)
        show(
            "the files' physical sizes summed per provider",
            files=len(files),
            physical=truth,
            dir_stats_service=self.dir_stats_service(),
        )
        self.lag("the root's distribution against that sum, before any change", root, truth)
        placed = files["/".join(PLACED_FILE)]
        receiver = self.boot.ids.providers[RECEIVER]
        for kind, body in (
            ("placement", {"replicatingProviderId": receiver}),
            ("eviction", {"evictingProviderId": receiver}),
        ):
            before = self.moved(placed)
            state = self.transfer(
                {
                    "type": "replication" if kind == "placement" else "eviction",
                    "dataSourceType": "file",
                    "fileId": placed,
                    **body,
                }
            )
            now = self.moved(placed)  # the file's own block map at the transfer's end, read at once
            expected = {name: truth[name] - before.get(name, 0) + now.get(name, 0) for name in truth}
            show(
                kind,
                file="/".join(PLACED_FILE),
                provider=RECEIVER,
                transfer=state,
                file_physical_before=before,
                file_physical_after=now,
            )
            self.lag(f"the root's distribution after the {kind}, from the transfer's end", root, expected)
            truth = self.truth(files)
            show(
                f"the files' physical sizes summed per provider, counted again after the {kind}",
                physical=truth,
                equals_expected=truth == expected,
            )
        self.latency(files[LATENCY_FILE])

    def moved(self, file_id: str) -> dict[str, int]:
        """One file's physical bytes per provider, from its own distribution through the data provider."""
        answer = self.call(HOLDER, "GET", f"/data/{file_id}/distribution").json()
        return {name: entry["physicalSize"] for name, entry in self.per_provider(answer).items()}


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
