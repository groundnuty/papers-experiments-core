"""O37 live: a read-only condition's tool server sees its transfer and QoS views and is refused every write.

    EXPCORE_LIVE_PLATFORM=/path/to/platform.toml EXPCORE_LIVE_MUTATE=1 uv run pytest tests/live
    EXPCORE_LIVE_OUTSIDE_SPACE=<space id> ...   # also the views of a space the condition does not hold

Skipped unless both variables are set. The platform file needs: `[toolserver]` with the real tool server (the pinned
onedata-mcp server), a condition that declares no write right whose allow-list holds the five view tools and the two
write tools of `live_support` (`VIEW_TOOLS`, `WRITE_TOOLS`) with its frozen manifest; a `[principals.toolserver]` user
holding exactly that condition's rights; and `[placement]` with a provider besides the data provider. Each skip names
what is missing, never a condition, a space or a provider.

What it does, all of it in a scratch directory under the configured prefixes that the provisioner tears down: two
small files; with the admin credential, a replication of the first to the other provider, waited to its end (the
transfer to view), and a QoS requirement on it (the requirement to view, removed at the end). Then, with the
condition's own token, minted and launched as a run's is (`ToolServerLauncher.launch`, the principal given):

- the view tools answer (`get_transfer`, `list_space_transfers` by the space's id, `get_qos_requirement`,
  `get_file_qos_summary`, `get_file_distribution`), and the server's own examination of its token answers 200 with no
  fail-open warning in its log;
- the write tools answer an error and change nothing (`schedule_file_replication`, `add_file_qos_requirement`), read
  back with the admin credential;
- the same token sent straight to the provider is refused (`live_support.refused`: 401, 403 or 400 posix eacces) a
  replication, an eviction, a rerun and a cancel of a transfer, a QoS requirement added and one removed, a file
  created and a file's content written, and none of them happened. The file writes are the refusals the old token
  got from its `data.readonly` caveat; this one gets them from its user, who holds no `space_write_data`;
- with `EXPCORE_LIVE_OUTSIDE_SPACE`, a space the condition's user is no member of: its transfer listing and root are
  not served to the token, through the tool and straight, and a transfer of it, when the admin finds one, is not
  served through `get_transfer`.

The tools' names are this test's choice, their arguments are filled from each tool's own input schema
(`live_support.tool_arguments`), and nothing here changes a tool.
"""

import asyncio
import contextlib
import os
import time

import pytest

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
if not PLATFORM or os.environ.get("EXPCORE_LIVE_MUTATE") != "1":
    pytest.skip(
        "EXPCORE_LIVE_PLATFORM and EXPCORE_LIVE_MUTATE=1 are not both set; these tests change a real deployment",
        allow_module_level=True,
    )

from live_support import (  # noqa: E402 -- only once the module is known to run
    VIEW_TOOLS,
    WRITE_TOOLS,
    mint_for_condition,
    prerequisite_missing,
    refused,
    tool_arguments,
)

from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import AUTH_HEADER, PlatformError, answered_absent  # noqa: E402
from expcore_onedata.toolserver import ToolServerLauncher  # noqa: E402

CEILING_S = 300.0
OUTSIDE_SPACE = os.environ.get("EXPCORE_LIVE_OUTSIDE_SPACE")  # a space id the condition's user is no member of
FAIL_OPEN = "Could not examine Oneprovider token via Onezone"  # the pinned server's warning when its examination fails
STATES = ("waiting", "ongoing", "ended")
CONTENT = bytes(range(256)) * 4


class Context:
    """A call context for the launcher and the tools: every infrastructure wait is entered and nothing is timed."""

    def infrastructure_wait(self, kind):
        return contextlib.nullcontext()


@pytest.fixture(scope="module")
def day0(tmp_path_factory):
    from expcore_onedata.day0.runner import build_context

    return build_context(PLATFORM, tmp_path_factory.mktemp("day0"))


def read_only_condition(config):
    """The first declared condition that declares no write right and allows every tool this test calls."""
    wanted = set(VIEW_TOOLS) | set(WRITE_TOOLS)
    for name, condition in (config.toolserver.conditions if config.toolserver else {}).items():
        if not condition.writes and wanted <= set(condition.allow):
            return name
    return None


def space_transfers(plane):
    """The ids of the space's transfers in every state, the first page of each, read with the admin credential
    through every supporting provider, so one scheduled through any of them is seen whichever shows it first."""
    client, path = plane["client"], f"/spaces/{plane['config'].space.id}/transfers"
    return {
        tid
        for provider in plane["config"].support.providers
        for state in STATES
        for tid in client.get(path, provider=provider, params={"state": state, "limit": 1000})["transfers"]
    }


def requirements_of(plane, file_id):
    """The ids of a file's QoS requirements, read with the admin credential through every supporting provider."""
    client = plane["client"]
    return {
        qid
        for provider in plane["config"].support.providers
        for qid in client.get(f"/data/{file_id}/qos/summary", provider=provider)["requirements"]
    }


def nothing_new(plane, file_id):
    """What the admin sees of the space's transfers and the file's requirements, to compare before and after."""
    return space_transfers(plane), requirements_of(plane, file_id)


@pytest.fixture(scope="module")
def plane(day0, tmp_path_factory):
    """The scratch files, the transfer and the requirement to view, and the read-only condition's token; the
    requirement is removed and the scratch torn down afterwards."""
    from expcore_onedata.provision import Provisioner

    config = day0.config
    missing = prerequisite_missing("round_trip", day0)
    if missing is not None:
        pytest.skip(missing)
    name = read_only_condition(config)
    if name is None:
        pytest.skip(f"no condition declares no write right and allows {', '.join(VIEW_TOOLS + WRITE_TOOLS)}")
    if config.principals.toolserver is None:
        pytest.skip("platform.toml declares no [principals.toolserver] user")
    holder = day0.placement.settings.holder
    receiver = next(p for p in config.support.providers if p != holder)
    log = tmp_path_factory.mktemp("o37") / "log.jsonl"
    provisioner = Provisioner(day0.client, config, day0.placement.settings, provider_ids=day0.provider_ids, log=log)
    directory = f"expcore-live-o37-{int(time.time())}"
    files = [provisioner.put_file(f"{directory}/part-{i}.bin", CONTENT) for i in range(2)]
    qid = None
    try:
        tid = provisioner.schedule_transfer("replication", files[0], receiver)
        done = consistency.wait_transfers(day0.client, receiver, [tid], timeout_s=180, poll_s=2)
        assert not done.timed_out, "the transfer to view did not end"
        body = {"fileId": files[0], "expression": "anyStorage", "replicasNum": 1}
        qid = provisioner.mutate("POST", "/qos_requirements", provider=holder, json=body)["qosRequirementId"]
        token = mint_for_condition(day0.issuer, config, name, config.principals.toolserver, CEILING_S)
        yield {
            "config": config,
            "client": day0.client,
            "issuer": day0.issuer,
            "provisioner": provisioner,
            "condition": name,
            "token": token,
            "holder": holder,
            "receiver": receiver,
            "directory": directory,
            "files": files,
            "tid": tid,
            "qid": qid,
        }
    finally:
        if qid is not None:
            provisioner.mutate("DELETE", f"/qos_requirements/{qid}", provider=holder)
        report = provisioner.teardown()
        assert report.ok, f"the scratch files were not all removed: {len(report.left)} left"


def call_tools(plane, calls):
    """Launch the condition's tool server as a run does and call each `(tool, values)`; return each tool's output.

    A tool whose required arguments the test cannot fill fails the test, naming the tool and the reason."""
    config, token = plane["config"], plane["token"]
    launcher = ToolServerLauncher(config, plane["issuer"], events=lambda kind, detail: None)
    ctx = Context()

    async def go():
        outputs = []
        launch = launcher.launch(
            ctx, plane["condition"], token, ceiling_s=CEILING_S, principal=config.principals.toolserver
        )
        async with launch as provider:
            schemas = {spec.name: spec.input_schema for spec in provider.tools()}
            for tool, values in calls:
                arguments = tool_arguments(schemas[tool], values)
                assert not isinstance(arguments, str), f"{tool}: {arguments}"
                outputs.append(await provider.execute(tool, arguments, timeout_s=60, ctx=ctx))
        return outputs

    return asyncio.run(go())


def values_of(plane, **override):
    """The value of each argument kind a tool may take, for the plane's first file."""
    config = plane["config"]
    receiver_id = plane["provisioner"].provider_id(plane["receiver"])
    return {
        "file": plane["files"][0],
        "transfer": plane["tid"],
        "requirement": plane["qid"],
        "space": config.space.id,
        "state": "ended",
        "provider": receiver_id,
        "expression": "anyStorage",
        "replicas": 1,
        **override,
    }


def as_token(plane, method, path, *, zone=False, headers=None, **options):
    """Send one request straight to the agent's local provider, the one the tool server talks to (or to Onezone with
    `zone`), as the condition's token, never through the mutation gate, whose credential is the admin's; return the
    PlatformError it raised, or None when it was accepted."""
    client, token = plane["client"], plane["token"].reveal()
    where = None if zone else plane["config"].support.local
    try:
        client._send(method, path, provider=where, headers={AUTH_HEADER: token, **(headers or {})}, **options)
    except PlatformError as error:
        return error
    return None


def require_refused(error, what):
    """A write: refused as Onedata refuses one (`live_support.refused`), never failed for another reason."""
    assert error is not None, f"{what}: accepted with the read-only condition's token"
    assert refused(error), f"{what}: failed, but not as a refusal ({error.status}, {error.code}, {error.errno})"


def require_unseen(error, what):
    """A read of a space the condition does not hold: answered, with a refusal or as not there, never served. A
    transport failure or a server error shows nothing either way, so it fails the test."""
    assert error is not None, f"{what}: served to the read-only condition's token"
    assert error.status is not None, f"{what}: no answer"
    assert 400 <= error.status < 500, f"{what}: a server error ({error.status}, {error.code})"


def test_o37_a_read_only_conditions_view_tools_answer_and_its_server_examines_its_token(plane, capfd):
    """O37: the views the 12 Nov run saw refused now serve, the two under `/data/{id}` still serve, and the server's
    own examination of its token answers, so its log holds no fail-open warning."""
    outputs = call_tools(plane, [(tool, values_of(plane)) for tool in VIEW_TOOLS])
    failed = [tool for tool, out in zip(VIEW_TOOLS, outputs, strict=True) if out.is_error or not out.text]
    assert failed == [], f"view tools that did not answer: {', '.join(failed)}"
    assert FAIL_OPEN not in capfd.readouterr().err
    token = plane["token"].reveal()
    assert as_token(plane, "POST", "/tokens/examine", zone=True, json={"token": token}) is None


def test_o37_a_read_only_conditions_write_tools_answer_an_error_and_change_nothing(plane):
    """O37: the replication and the QoS requirement asked for through the tools are refused by Onedata for the
    privilege the user lacks: each tool answers an error, and with the admin credential no transfer of the space and no
    requirement of the file appears."""
    file_id = plane["files"][0]
    before = nothing_new(plane, file_id)
    outputs = call_tools(plane, [(tool, values_of(plane)) for tool in WRITE_TOOLS])
    assert [out.is_error for out in outputs] == [True, True], [out.text[:200] for out in outputs]
    time.sleep(3)
    assert nothing_new(plane, file_id) == before


def test_o37_the_read_only_token_is_refused_every_transfer_and_qos_write_straight_at_the_provider(plane):
    """O37: sent straight to the provider, without the tool server, the token is refused a replication, an eviction,
    a QoS requirement, the removal of the admin's requirement, a rerun of the ended transfer and the cancel of a
    running one, and none of them happened."""
    client, holder, provisioner, file_id = plane["client"], plane["holder"], plane["provisioner"], plane["files"][0]
    running = provisioner.schedule_transfer("replication", plane["files"][1], plane["receiver"])
    time.sleep(3)  # every provider shows the running transfer before the listing is taken
    before = nothing_new(plane, file_id)
    receiver_id, holder_id = provisioner.provider_id(plane["receiver"]), provisioner.provider_id(holder)
    replication = {"type": "replication", "replicatingProviderId": receiver_id, "dataSourceType": "file"}
    eviction = {"type": "eviction", "evictingProviderId": holder_id, "dataSourceType": "file"}
    qos = {"fileId": file_id, "expression": "anyStorage", "replicasNum": 1}
    attempts = {
        "a replication": as_token(plane, "POST", "/transfers", json={**replication, "fileId": file_id}),
        "an eviction": as_token(plane, "POST", "/transfers", json={**eviction, "fileId": file_id}),
        "a QoS requirement": as_token(plane, "POST", "/qos_requirements", json=qos),
        "a requirement's removal": as_token(plane, "DELETE", f"/qos_requirements/{plane['qid']}"),
        "a rerun": as_token(plane, "POST", f"/transfers/{plane['tid']}/rerun"),
        "a cancel": as_token(plane, "DELETE", f"/transfers/{running}"),
    }
    for what, error in attempts.items():
        require_refused(error, what)
    time.sleep(3)
    assert nothing_new(plane, file_id) == before
    assert plane["qid"] in requirements_of(plane, file_id)
    state = consistency.transfer_state(client.get(f"/transfers/{running}", provider=plane["receiver"]))
    assert state != "cancelled", "the running transfer was cancelled"


def test_o37_the_read_only_token_is_refused_a_file_and_a_write_of_content(plane):
    """O37: with no `data.readonly` caveat, the user's lack of `space_write_data` is what refuses a data write: a file
    created in the scratch directory and a write of a file's content are both refused, the file is not there and the
    content is what it was."""
    provisioner, file_id = plane["provisioner"], plane["files"][1]
    directory_id = provisioner.resolve(plane["directory"]).file_id
    before = provisioner.read_file(file_id)
    name = "o37-refused.bin"
    created = as_token(plane, "POST", f"/data/{directory_id}/children", params={"name": name, "type": "REG"})
    written = as_token(
        plane,
        "PUT",
        f"/data/{file_id}/content",
        params={"offset": 0},
        content=bytes(len(CONTENT)),
        headers={"Content-Type": "application/octet-stream"},
    )
    require_refused(created, "a file's creation")
    require_refused(written, "a write of content")
    assert name not in provisioner.children(directory_id)
    assert provisioner.read_file(file_id) == before


OUTSIDE = pytest.mark.skipif(
    not OUTSIDE_SPACE, reason="EXPCORE_LIVE_OUTSIDE_SPACE (a space the condition's user is no member of) is not set"
)


@OUTSIDE
def test_o37_a_read_only_condition_sees_nothing_of_a_space_it_does_not_hold(plane):
    """O37: the token's reach is its user's spaces, so another space's transfer listing is refused through the tool
    and straight at the provider, and so is its root."""
    (listing,) = call_tools(plane, [("list_space_transfers", values_of(plane, space=OUTSIDE_SPACE))])
    assert listing.is_error, "the tool listed the transfers of a space the condition does not hold"
    path = f"/spaces/{OUTSIDE_SPACE}"
    require_unseen(as_token(plane, "GET", f"{path}/transfers", params={"state": "ended"}), "a listing")
    require_unseen(as_token(plane, "GET", path), "the space's root")


@OUTSIDE
def test_o37_a_read_only_condition_cannot_view_a_transfer_of_a_space_it_does_not_hold(plane):
    """O37: a transfer of another space, found with the admin credential through any declared provider that lists it,
    is refused to the condition through `get_transfer`. Skipped, saying so, when no declared provider lists one."""
    client, found = plane["client"], None
    for provider in plane["config"].providers:
        try:
            page = client.get(f"/spaces/{OUTSIDE_SPACE}/transfers", provider=provider, params={"state": "ended"})
        except PlatformError as error:
            if answered_absent(error) or error.status in (400, 403):
                continue
            raise
        found = next(iter(page.get("transfers") or []), None)
        if found is not None:
            break
    if found is None:
        pytest.skip("no declared provider lists a transfer of the outside space")
    (viewed,) = call_tools(plane, [("get_transfer", values_of(plane, transfer=found))])
    assert viewed.is_error, "the tool showed a transfer of a space the condition does not hold"
