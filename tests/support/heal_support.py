"""The four-provider federation of the link heal's tests (O35), shared by the tests that run a heal.

The pattern is the one seen live: `beta` (providers[1]) is the provider the injector takes down; during the outage
`alpha` (providers[0]) fetched from it and its link crashed, so after the restore readers of `alpha` fail while every
pair `alpha` reads passes, and the restored provider's own pairs pass. `HealServices` scripts each pair, each reader's
`ping` and `restart_link`, and what a link or a container restart mends or breaks.
"""

from fault_support import FakeClock, FakeServices, faults_table, injector_for

from expcore_onedata.fault_heal import LinkMatrix, PairState
from expcore_onedata.testing import write_platform

NAMES = ("alpha", "beta", "gamma", "delta")
STORES = {name: f"store-{name}" for name in NAMES}
LABEL = {STORES[name]: f"providers[{index}]" for index, name in enumerate(NAMES)}
SERVICE = {label: service for service, label in LABEL.items()}
CRASHED, RESTORED, READER, SECOND = (f"providers[{index}]" for index in range(4))
TARGET = RESTORED


class HealServices(FakeServices):
    """Four providers' services with a scripted link check, session gate, ping, link restart and transfer listings.

    `stuck` holds the failing pairs as (reader, source) labels. `link_fixes[reader]` is what a link restart of that
    reader mends: by default its own stuck pairs and nothing else, the shape seen live (each stale reader of a crashed
    source is cleared by its own `restart_link` alone, never by the source's or another reader's); `()` mends nothing.
    `link_breaks[reader]` is the pairs it breaks, `container_fixes` the readers a container restart mends, and `grow`
    adds pairs to `stuck` before the given check.

    `wedge_s` is the rule the live data settled: after a provider's link restarts, a fetch from it (any link check)
    within `wedge_s` seconds wedges every pair with it as the source, failing until that pair's reader's own link
    restarts; a fetch after the window finds the link up. (Live: a check 23 s after a restart left a reverse pair
    stale, while a clean 3-minute settle left every reverse pair healthy, even one minutes old.) `transfers` is the
    script of active-transfer readings, 0 once it runs out.
    """

    def __init__(self, **knobs):
        super().__init__(tuple(STORES.values()), **knobs)
        self.stuck: set[tuple[str, str]] = {(READER, CRASHED)}
        self.pong: dict[str, bool] = {}
        self.link_result: dict[str, str] = {}
        self.link_fixes: dict[str, tuple[tuple[str, str], ...]] = {}
        self.link_breaks: dict[str, tuple[tuple[str, str], ...]] = {}
        self.container_fixes: set[str] = set()
        self.grow: dict[int, set[tuple[str, str]]] = {}
        self.gate_detail: str | None = None
        self.gate_after: int = 0  # readings of the gate that answer not ready first
        self.checks = 0
        self.clock: FakeClock | None = None
        self.link_s = 0.0  # seconds a link restart takes on the clock
        self.ping_s = 0.0  # seconds a ping takes on the clock
        self.on_ping = None  # called with the reader's label while it is pinged
        self.on_check = None  # called before each link check
        self.wedge_s = 0.0
        self.restarted_at: dict[str, float] = {}  # each provider's last link restart, on the clock
        self.transfers: list[int] = []
        self.never_serving: set[str] = set()  # providers whose container, restarted, never serves
        self.actions: list[tuple[str, str]] = []
        self.at: list[tuple[str, str, float]] = []  # each action with the clock's time
        self.checked_at: list[float] = []  # each link check's time

    def _matrix(self) -> LinkMatrix:
        labels = list(LABEL.values())
        pairs = tuple(
            PairState(reader, source, (reader, source) not in self.stuck, 0.1, (reader, source) in self.stuck)
            for reader in labels
            for source in labels
            if reader != source
        )
        shown = [f"{source}->{reader}" for reader, source in sorted(self.stuck)]
        return LinkMatrix(not self.stuck, "stuck: " + ", ".join(shown) if shown else "every pair passed", pairs)

    def _note(self, verb: str, label: str) -> None:
        self.actions.append((verb, label))
        self.at.append((verb, label, self.clock.now if self.clock is not None else 0.0))

    def _fetch_through_restarting_links(self) -> None:
        """A link check fetches every pair: those from a provider whose link restarted under `wedge_s` ago wedge."""
        now = self.clock.now if self.clock is not None else 0.0
        for source, began in self.restarted_at.items():
            if now < began + self.wedge_s:
                self.stuck |= {(reader, source) for reader in LABEL.values() if reader != source}

    def link_matrix(self, service):
        self.checks += 1
        self.checked_at.append(self.clock.now if self.clock is not None else 0.0)
        if self.on_check is not None:
            self.on_check()
        self.stuck |= self.grow.get(self.checks, set())
        self._fetch_through_restarting_links()
        return self._matrix()

    def active_transfers(self):
        return self.transfers.pop(0) if self.transfers else 0

    def serves(self, service):
        if LABEL[service] in self.never_serving:
            self.calls.append(("serves", service))
            return False
        return super().serves(service)

    def sessions_ready(self, service, gate_probe):
        if self.gate_after > 0:
            self.gate_after -= 1
            return "providers[3] holds an error for the restored provider"
        return self.gate_detail

    def ping(self, service):
        self._note("ping", LABEL[service])
        if self.clock is not None:
            self.clock.sleep(self.ping_s)
        if self.on_ping is not None:
            self.on_ping(LABEL[service])
        return self.pong.get(LABEL[service], True)

    def restart_link(self, service):
        reader = LABEL[service]
        self._note("restart_link", reader)
        if self.clock is not None:
            self.clock.sleep(self.link_s)
        self.restarted_at[reader] = self.clock.now if self.clock is not None else 0.0
        fixes = self.link_fixes.get(reader)
        mended = {pair for pair in self.stuck if pair[0] == reader} if fixes is None else set(fixes)
        self.stuck = (self.stuck - mended) | set(self.link_breaks.get(reader, ()))
        return self.link_result.get(reader, "ok")

    def stop(self, service):
        self._note("stop", LABEL[service])
        super().stop(service)

    def start(self, service):
        self._note("start", LABEL[service])
        super().start(service)
        if LABEL[service] in self.container_fixes:
            self.stuck = {pair for pair in self.stuck if pair[0] != LABEL[service]}


def four_providers(data, tokens=None, **faults):
    """Four providers, each with a service; short commands and settles, so a declared heal fits its default bound.
    `tokens` replaces keys of `[tokens]` (a study's long margin, say)."""
    for name, index in (("gamma", 3), ("delta", 4)):
        data["providers"][name] = {"url": f"https://{name}.test", "id": f"pr-{name}-{index}"}
    data["faults"] = faults_table(**{"services": dict(STORES), "command_timeout_s": 10, "heal_settle_s": 30, **faults})
    if tokens is not None:
        data["tokens"] = {**data.get("tokens", {}), **tokens}


def heal_injector(tmp_path, services=None, tokens=None, **faults):
    """A sanctioned injector for providers[1] of the four-provider federation, on its own fake clock, with its
    services; `tokens` as for `four_providers`."""
    tmp_path.mkdir(parents=True, exist_ok=True)  # a test may build a second federation under a subdirectory
    platform = write_platform(tmp_path, lambda data: four_providers(data, tokens, **faults))
    services = services or HealServices()
    clock = FakeClock()
    services.clock = clock
    return injector_for(platform, services, target=TARGET, clock=clock), services


def stale():
    """The live pattern under the settled rule: the crashed source's readers stale, and a fetch from a provider within
    20 s of its link restart (the tests' settle is 30 s) wedging the pair."""
    services = HealServices()
    services.stuck = {(READER, CRASHED), (SECOND, CRASHED)}
    services.wedge_s = 20.0
    return services
