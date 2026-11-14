"""Shared helpers of the link check's tests (O35, #221): three providers and a probe space, a check on the test's clock,
and the probe files and markers a check leaves in the fake federation's probe space."""

from expcore_onedata.linkcheck import check_links
from expcore_onedata.linkprobe import RETIRED_PREFIX, generation_of, probe_key
from expcore_onedata.testing import ALPHA_ID, BETA_ID, SPACE_NAME, with_placement
from expcore_onedata.testing.world import PROBE_SPACE_ID, PROBE_SPACE_NAME, with_link_check

GAMMA_ID = "pr-gamma"
DELTA_ID = "pr-delta"  # declared, supports the probe space and no study space, and is not in [link_check].providers
OMEGA_ID = "pr-omega"  # supports the probe space and is not in platform.toml at all
BOUND_S = 5.0
CONFIGURED = (PROBE_SPACE_NAME, PROBE_SPACE_ID, "alpha", "beta", "gamma", "delta", "omega", SPACE_NAME, ".test")
PROVIDERS = {"alpha": ALPHA_ID, "beta": BETA_ID, "gamma": GAMMA_ID}  # by name; alpha is the local provider


def three_providers(data, *, link_check=True, **override):
    """The study space supported by alpha (local), beta and gamma; the probe space by all three, and by delta, which is
    declared and left out of `[link_check].providers`, and by omega, which platform.toml does not declare."""
    with_placement(data)
    data["providers"]["gamma"] = {"url": "https://gamma.test", "id": GAMMA_ID}
    data["providers"]["delta"] = {"url": "https://delta.test", "id": DELTA_ID}
    data["support"] = {"providers": ["alpha", "beta", "gamma"], "local": "alpha"}
    if link_check:
        with_link_check(data, ["alpha", "beta", "gamma"], timeout_s=BOUND_S, **override)


def add_providers(fake):
    """Add gamma, delta and omega to the fake federation, and the probe space they all support with alpha and beta."""
    fake.supports[BETA_ID] = {"readonly": False}
    fake.add_provider(GAMMA_ID, "gamma.test")
    fake.add_provider(DELTA_ID, "delta.test", supports=False)
    fake.add_provider(OMEGA_ID, "omega.test", supports=False)
    fake.add_data_space(PROBE_SPACE_ID, PROBE_SPACE_NAME, [ALPHA_ID, BETA_ID, GAMMA_ID, DELTA_ID, OMEGA_ID])
    return fake


def run(config, client, ticker):
    """One check on the test's clock, its fetches one at a time so that a hang's wait moves no other pair's clock."""
    return check_links(config, client, clock=ticker.clock, sleep=ticker.sleep, now=ticker.clock, workers=1)


def key_of(reader, source):
    """The id the probe of the pair `reader` <- `source` (provider names) is named and made from: the source's own
    when `reader` is the local provider, so that its probe is the check's original one, else the source's and the
    reader's."""
    return probe_key(PROVIDERS[source], PROVIDERS[reader], ALPHA_ID)


def pair_of(report, local, source):
    """The result of the pair read through `local` from `source`, both by provider name, by position in the report."""
    names = list(PROVIDERS)
    found = [p for p in report.pairs if (p.local, p.source) == (f"providers[{names.index(local)}]", _label(source))]
    assert len(found) == 1, f"the report holds exactly one pair {local} <- {source}"
    return found[0]


def _label(name):
    return f"providers[{list(PROVIDERS).index(name)}]"


def probes_of(fake, provider_id):
    """The probe files of a source in the probe space's tree, by generation, as the local provider reads them."""
    root = fake.space_roots[PROBE_SPACE_ID]
    found = {generation_of(n.name, provider_id): n.file_id for n in fake.nodes.values() if n.parent == root}
    found.pop(None, None)
    return found


def markers_of(fake):
    """The names of the retirement markers in the probe space's tree: the durable record of the retired files."""
    root = fake.space_roots[PROBE_SPACE_ID]
    return sorted(n.name for n in fake.nodes.values() if n.parent == root and n.name.startswith(RETIRED_PREFIX))


def probe_of(fake, provider_id):
    """The probe file of a source read by the local provider: the one probe it has, found by its name in the probe
    space's tree."""
    found = probes_of(fake, provider_id)
    assert len(found) == 1, "each source has exactly one probe once a check has passed"
    return next(iter(found.values()))


def reads_through(fake, reader_id, start=0):
    """The ids of the files whose content `reader_id` was asked for, in order, from the `start`th call on."""
    calls = fake.provider_calls[start:]
    return [c[2].split("/")[2] for c in calls if c[0] == reader_id and c[1] == "GET" and c[2].endswith("/content")]


def held(fake, file_id, provider_id):
    return sum(n for _, n in fake.blocks.get((file_id, provider_id), []))
