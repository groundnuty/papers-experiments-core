"""The fault gate's federation of scripted pairs and its link check (O35), shared by the tests of the gate's heal.

No injection is open, so no restore will heal the pairs: the pattern seen live is a provider whose rtransfer link
crashed (`providers[0]`), and a reader whose pair reading from it never reconnected (`providers[2]`). `Federation`
scripts the pairs and every action a heal takes; `GateCheck` is the rest of the platform's health check as
`PlatformHealth` runs it inside the gate; `gate_of` builds the fault gate around it on a fake clock.
"""

import time

from fault_support import FakeClock, FakeServices
from heal_support import STORES, four_providers

from expcore.campaign.healthgate import Health
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_matrix import LinkMatrix, PairState
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.testing import write_platform

CRASHED, OTHER, READER, SECOND = (f"providers[{index}]" for index in range(4))
EVERY = (CRASHED, OTHER, READER, SECOND)


class ProcessEnded(BaseException):  # what a kill does to the healing thread, not an error it handles
    """The healing process ending where it stands: nothing in the heal catches it, so its thread unwinds as a killed
    process's would leave things (its locks let go, no line journalled past the point it reached)."""


class Federation(FakeServices):
    """Providers' services with scripted pairs, by label. `stuck` holds the failing pairs as (reader, source) whose
    fetch failed; `untested` those whose probe could not be made ready (failing, with no stuck link). A link restart of
    a reader mends its own stuck pairs unless `link_fixes[reader]` says what it mends; a container restart of a reader
    in `container_fixes` mends its pairs. `reads` notes every all-pairs check with the clock's time: `gate` for the
    gate's own, `heal` for the heal's; `actions` every ping, link restart, stop, start and arming of the session
    gate."""

    def __init__(self, stores=None):
        stores = dict(stores or STORES)
        super().__init__(tuple(stores.values()))
        self.label = {service: f"providers[{index}]" for index, service in enumerate(stores.values())}
        self.stuck: set[tuple[str, str]] = {(READER, CRASHED)}
        self.untested: set[tuple[str, str]] = set()
        self.link_fixes: dict[str, tuple[tuple[str, str], ...]] = {}
        self.container_fixes: set[str] = set()
        self.clock: FakeClock | None = None
        self.ping_s = 0.0
        self.on_ping = None  # called with the provider's label while it is pinged
        self.on_read = None  # called before each of the heal's own reads of every pair
        self.on_restart = None  # called at each link restart, before it mends anything
        self.on_restart_of = None  # called with the provider's label at each link restart, before `on_restart`
        self.restart_s = 0.0  # real seconds a link restart takes: a heal that outlasts the core's wait for a check
        self.end_before_start: set[str] = set()  # providers whose start ends the healing process (`ProcessEnded`)
        self.on_start = None  # called with the provider's label at each start, before it takes effect
        self.actions: list[tuple[str, str, float]] = []
        self.reads: list[tuple[str, float]] = []
        self.gates: list[tuple[str, str | None, float]] = []  # each session gate reading: provider, probe, time

    def now(self) -> float:
        return self.clock.now if self.clock is not None else 0.0

    def matrix(self) -> LinkMatrix:
        labels = list(self.label.values())
        failing = self.stuck | self.untested
        pairs = tuple(
            PairState(reader, source, (reader, source) not in failing, 0.1, (reader, source) in self.stuck)
            for reader in labels
            for source in labels
            if reader != source
        )
        shown = ", ".join(f"{source}->{reader}" for reader, source in sorted(failing))
        return LinkMatrix(not failing, f"stuck: {shown}" if shown else "every pair passed", pairs)

    def _note(self, verb: str, service: str) -> None:
        self.actions.append((verb, self.label[service], self.now()))

    def link_matrix(self, service):
        self.reads.append(("heal", self.now()))
        if self.on_read is not None:
            self.on_read()
        return self.matrix()

    def active_transfers(self):
        return 0

    def sessions_ready(self, service, gate_probe):
        self.gates.append((self.label[service], gate_probe, self.now()))
        return None

    def arm_gate(self, service):
        self._note("arm_gate", service)
        return "gate-probe-id"

    def ping(self, service):
        self._note("ping", service)
        if self.clock is not None:
            self.clock.sleep(self.ping_s)
        if self.on_ping is not None:
            self.on_ping(self.label[service])
        return True

    def restart_link(self, service):
        self._note("restart_link", service)
        if self.on_restart_of is not None:
            self.on_restart_of(self.label[service])
        if self.on_restart is not None:
            self.on_restart()
        time.sleep(self.restart_s)
        reader = self.label[service]
        fixes = self.link_fixes.get(reader)
        mended = {pair for pair in self.stuck if pair[0] == reader} if fixes is None else set(fixes)
        self.stuck -= mended
        return "ok"

    def stop(self, service):
        self._note("stop", service)
        super().stop(service)

    def start(self, service):
        self._note("start", service)
        if self.on_start is not None:
            self.on_start(self.label[service])
        if self.label[service] in self.end_before_start:  # the process ends here: after the stop, before the start
            raise ProcessEnded
        super().start(service)
        if self.label[service] in self.container_fixes:
            self.stuck = {pair for pair in self.stuck if pair[0] != self.label[service]}


class GateCheck:
    """The rest of the platform's health check as `PlatformHealth` runs it inside the gate: the space (`space_ok`),
    then its own link check of every pair. `take` hands the gate the report a call made (once), `expire` makes the next
    call check the links again. With `cache`, as `PlatformHealth` within `every_s`, a call whose last report passed and
    was not expired answers with it and checks nothing. `grow` adds stuck pairs before the check of that number, as a
    pair that sticks between two checks; `report` is the latest report, whoever made it."""

    def __init__(self, federation: Federation) -> None:
        self.federation, self.report, self.made, self.runs = federation, None, None, 0
        self.space_ok, self.cache, self.expired = True, False, False
        self.grow: dict[int, set[tuple[str, str]]] = {}
        self.on_check = None  # called at each check, while the gate holds the federation for it

    def take(self) -> LinkMatrix | None:
        made, self.made = self.made, None
        return made

    def expire(self) -> None:
        self.expired = True

    def __call__(self) -> Health:
        self.runs += 1
        if self.on_check is not None:
            self.on_check()
        if not self.space_ok:
            return Health(False, "the providers supporting the space differ from the declared set")
        if self.cache and self.report is not None and self.report.ok and not self.expired:
            return Health(True, "every provider pair transferred (the last check, still recent)")
        self.expired = False
        self.federation.stuck |= self.grow.get(self.runs, set())
        self.federation.reads.append(("gate", self.federation.now()))
        self.report = self.made = self.federation.matrix()
        if self.report.ok:
            return Health(True, "every provider pair transferred")
        return Health(False, f"no run is admitted until every provider link transfers: {self.report.detail}")


def gate_of(tmp_path, federation=None, **faults):
    """The fault gate around `GateCheck`, as `PlatformHealth` builds it, on a fake clock that is also its wall: four
    providers (`heal_support.four_providers`, with the token margin a declared heal needs) and a short wait for the
    federation."""
    tmp_path.mkdir(parents=True, exist_ok=True)  # a test may build a second federation under a subdirectory
    platform = write_platform(tmp_path, lambda data: four_providers(data, **{"health_wait_s": 0.2, **faults}))
    config = load_platform(platform, require_secrets=False)
    settings = load_faults(platform, config)
    federation = federation or Federation()
    clock = FakeClock()
    federation.clock = clock
    check = GateCheck(federation)
    labels = {config.provider_label(name): service for name, service in settings.services.items()}
    gate = FaultGate(
        settings,
        federation,
        check,
        labels=labels,
        links=check.take,
        relink=check.expire,
        clock=clock,
        sleep=clock.sleep,
        wall=clock,
    )
    return gate, federation, check, settings
