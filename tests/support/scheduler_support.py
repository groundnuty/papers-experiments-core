"""Helpers for the scheduler tests: a fake executor, a stub backend factory and a small campaign. No network."""

import collections
import tempfile
import threading
from pathlib import Path
from typing import Any

from campaign_support import make_fingerprint, make_manifest, make_schema
from loop_support import FakeProvider
from record_support import CODE, make_config

from expcore.campaign.executor import Endpoint, RunRequest, record_run
from expcore.campaign.factors import FactorSchema
from expcore.campaign.scheduler import Scheduler
from expcore.campaign.settings import SchedulerConfig
from expcore.campaign.store import Store
from expcore.config.models import ModelSpec, ModelTable
from expcore.config.prices import Price
from expcore.errors import InfraError
from expcore.models.adapters import StubAdapter

HEAVY = {"text": "x" * 300_000, "stop": "stop"}  # a reply of about 75,000 output tokens


class ReportingBackend(StubAdapter):
    """A stub that reports the base URL it was built for, can be scripted per run, and can claim a served identity."""

    def __init__(self, name: str, seed: int, base_url: str | None, probe_error: BaseException | None = None):
        super().__init__([], name=name, seed=seed, probe_error=probe_error)
        self.base_url = base_url
        self.served: tuple[str | None, str | None] = (None, None)
        self.closed = False

    def info(self) -> dict[str, Any]:
        return {**super().info(), "base_url": self.base_url}

    def use(self, script: list) -> None:
        self._script, self._i = list(script), 0

    async def step(self):
        turn = await super().step()
        turn.served_model, turn.system_fingerprint = self.served
        return turn

    async def aclose(self) -> None:
        self.closed = True


class Factory:
    """The backend factory: remembers every backend it built, and which models' probes should fail."""

    def __init__(self, refuse: dict[str, BaseException] | None = None):
        self.refuse = refuse or {}
        self.calls: list[tuple[str, int, str | None]] = []
        self.built: list[ReportingBackend] = []

    def __call__(self, spec, *, seed, limits, base_url, time_left):
        self.calls.append((spec.key, seed, base_url))
        backend = ReportingBackend(spec.key, seed, base_url, self.refuse.get(spec.key))
        self.built.append(backend)
        return backend


class FakeExecutor:
    """The study's executor, with a queue of behaviours per run key; a run with none finishes.

    A behaviour is a name or a tuple: "finish", "heavy", ("infra", kind) for a run the loop ends as infrastructure,
    ("raise_infra", kind) for an `InfraError` outside the loop, ("defect",) for a crash in our code, ("served", model,
    fingerprint) for a run served by that identity, or a function of the request.
    """

    def __init__(self, behaviours: dict[str, list] | None = None, config=None, default: Any = "finish"):
        self.behaviours = {k: collections.deque(v) for k, v in (behaviours or {}).items()}
        self.config, self.default = config or make_config(), default
        self.requests: list[RunRequest] = []
        self.records: list = []
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def calls_of(self, key: str) -> int:
        return sum(1 for r in self.requests if r.run_key == key)

    async def __call__(self, request: RunRequest):
        with self._lock:
            self.requests.append(request)
            queue = self.behaviours.get(request.run_key)
            behaviour = queue.popleft() if queue else self.default
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            return await self._run(request, behaviour)
        finally:
            with self._lock:
                self.live -= 1

    async def _run(self, request: RunRequest, behaviour: Any):
        script: list = []
        kind = behaviour[0] if isinstance(behaviour, tuple) else behaviour
        if callable(behaviour):
            return await behaviour(request)
        if kind == "raise_infra":
            raise InfraError(behaviour[1], "failed before the loop")
        if kind == "defect":
            raise ValueError("a crash in our own code")
        if kind == "infra":
            script = [InfraError(behaviour[1], "the endpoint failed")]
        elif kind == "infra_after_turn":
            script = [[("look", {})], InfraError(behaviour[1], "the endpoint failed")]
        elif kind == "heavy":
            script = [HEAVY]
        elif kind == "served":
            request.backend.served = (behaviour[1], behaviour[2])
        request.backend.use(script)
        record = await record_run(
            request,
            [FakeProvider({"look": lambda arguments: "ok"})],
            "be brief",
            "do the task",
            self.config,
            code=dict(CODE),
            core_version="0.1.0",
        )
        self.records.append(record)
        return record


PRICE = Price(3.0, 15.0)


def models_for(*keys: str, price: Price | None = PRICE, free: bool = False) -> ModelTable:
    """A model table of stub models, all with one price (or free, or none)."""
    return ModelTable(
        {
            k: ModelSpec(key=k, backend="stub", api_id=k, status="frozen", price=None if free else price, free=free)
            for k in keys
        }
    )


def settings_for(**changes: Any) -> SchedulerConfig:
    """Settings that never wait: no retry delay, a quick poll, and `run` returns when only held runs are left."""
    base = {"retry_delay_s": 0.0, "poll_interval_s": 0.01, "exit_when_blocked": True}
    return SchedulerConfig(**{**base, **changes})


_EMPTY_STORES: dict[str, bytes] = {}  # schema digest -> the bytes of a closed, empty database


def seed_store(root: Path, schema: FactorSchema) -> None:
    """Put a closed, empty database for the schema where the scheduler will open it, unless one is there.

    Creating a database costs a dozen fsyncs; opening an existing one costs almost none. The scheduler still opens it
    through the real code.
    """
    root.mkdir(parents=True, exist_ok=True)
    target = root / "state.sqlite"
    if target.exists():
        return
    if schema.digest not in _EMPTY_STORES:
        scratch = Path(tempfile.mkdtemp())
        Store(scratch / "empty.sqlite", schema).close()
        _EMPTY_STORES[schema.digest] = (scratch / "empty.sqlite").read_bytes()
    target.write_bytes(_EMPTY_STORES[schema.digest])


def build(
    root: Path,
    *,
    schema: FactorSchema | None = None,
    executor: FakeExecutor | None = None,
    factory: Factory | None = None,
    settings: SchedulerConfig | None = None,
    models: ModelTable | None = None,
    base_urls: dict[str, str | None] | None = None,
    plan: dict[str, Any] | None = None,
    **options: Any,
) -> Scheduler:
    """A scheduler over a small campaign on the support schema; keyword options go to `Scheduler`."""
    schema = schema or make_schema()
    plan = plan or {}
    seed_store(root, schema)
    manifest = make_manifest(schema, **plan)
    names = {"task_ids": "tasks", "model_keys": "models", "seed_ids": "seeds"}
    fingerprint = make_fingerprint(schema, **{name: list(plan[key]) for name, key in names.items() if key in plan})
    models_in_plan = plan.get("models", ("m1",))
    executor = executor or FakeExecutor()
    endpoints = {
        m: Endpoint(m, f"unit-{m}", (base_urls or {}).get(m, f"https://llm.example.invalid/{m}"))
        for m in models_in_plan
    }
    return Scheduler(
        root,
        schema,
        manifest,
        fingerprint,
        executor,
        agent_config=executor.config,
        models=models or models_for(*models_in_plan),
        endpoints=endpoints,
        settings=settings or settings_for(),
        backend_factory=factory or Factory(),
        **options,
    )


def key_of(schema: FactorSchema, condition: str, task: str = "task-a", seed: int = 1, model: str = "m1") -> str:
    """The run key text of a condition, task, seed and model."""
    return schema.key(condition, task, model, seed).text


def attempts_of(store, key: str) -> list[tuple[str, int, int]]:
    """The (state, charged, attempt) of every attempt of a run, in order."""
    rows = store.q("SELECT state, charged, attempt FROM attempts WHERE run_key=? ORDER BY id", (key,))
    return [(r["state"], r["charged"], r["attempt"]) for r in rows]


def event_kinds(store) -> list[str]:
    return [r["kind"] for r in store.q("SELECT kind FROM events ORDER BY id")]
