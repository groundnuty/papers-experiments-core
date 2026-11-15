# The sandbox's REST path through the data gate, live, 13 Nov 2026

**Fail.** On the **25.0 development build (op-worker f118ed9)**, the live test of the REST path
(`tests/live/test_rest_path.py`, #203) passed 4 of its 5 tests and failed `test_o08`: through the gate, Onezone answered
**401** to the token-scope inference (`POST /api/v3/onezone/tokens/infer_access_token_scope`). The file read itself
succeeded: the lookup and the content answered 200, and the agent's 64 bytes had the SHA-256 the host read through
the local provider. Neither of #203's two defects is behind the failure: the gate read its keys and started, and it
reached the zone and the local provider on the federation's network. The likely cause, not confirmed, is the gate's
own `X-Auth-Token` on the zone's call (see "What the 401 points to").

The run used `main` with #210 merged (efe2ff8), on a fresh `up` of the default layout (`federation.toml`: one zone,
three providers, `pine` the local provider), taken down afterwards. The agent's image, and so the gate's, was a
study's agent image, CPython 3.12.13. The test's derived `[sandbox]` table names the agent's image for the gate too,
which is what an absent `[sandbox.images.gate]` gives. "Unknown" means not measured. No token, id, address, address
range or host path is recorded here.

The commands, outside any sandbox (`<pool>` is a private range the host routes nowhere; `<image>` is the agent
image's id, resolved on the host and never pulled):

```sh
uv run python deploy/compose-federation/federation.py up
EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml EXPCORE_LIVE_CONTENT_PATH=hello.txt \
EXPCORE_LIVE_SANDBOX_POOL=<pool> EXPCORE_LIVE_AGENT_IMAGE=<image> \
timeout 900 uv run pytest tests/live/test_rest_path.py -rA --durations=0
# once more, the one retry, with -vv -l: the first run's assertion message cut the read's size and hash
uv run python deploy/compose-federation/federation.py down
```

## Results

| Run | Result |
|---|---|
| first | 4 passed, 1 failed in 11.41 s (the fixture's setup 10.35 s) |
| retry, `-vv -l` | 4 passed, 1 failed in 14.31 s: the same failure, with the read's size and hash shown |

| Test | Both runs |
|---|---|
| `test_o05`: the gate runs, as root, every capability dropped but `NET_BIND_SERVICE` (#203's keys) | pass |
| `test_o03`: the gate alone on the federation's network, the agent on the sandbox's alone, the gate's keys for the zone's and the local provider's domains (#203's network) | pass |
| `test_o08`: the zone answers, and the file read through the gate is the file's bytes | **fail**: `assert 401 == 200` on `zone_status` |
| `test_o22`: the gate refused nothing, and counted the file's bytes as served | pass: 0 refusals, 64 content bytes |
| `test_o15`: nothing of the sandbox left, the federation's network still there | pass |

## The read

| Fact | First run | Retry |
|---|---|---|
| Sandbox start (UTC, the run's metadata) | 12:13:53 | 12:17:01 |
| Gate container started (UTC, `docker inspect`) | 12:13:55.1 | 12:17:03.4 |
| Scope inference through the gate | 401, 921 B | 401, 921 B |
| Lookup of `hello.txt` | 200, 193 B | 200, 193 B |
| Content | 200, 64 B | 200, 64 B |
| Size and SHA-256 against the host's read | 64 B; hash unknown (cut from the message) | 64 B each; **SHA-256 equal** |
| Run-start mark to the content's answer | 0.55 s | 0.52 s |
| Lookup's answer to the content's answer | 0.07 s | 0.08 s |

The seconds come from the gate log's timestamps, which mark when each answer was logged, not when its request was
sent. The span from the run-start mark includes starting the agent's cell, so the content request alone is the
second row. The time of the gate's "gate ready" line is not in what the run collects: unknown.

## The gate's log

Each run's `gate-requests.jsonl` holds five lines, here with the content path's file id left out:

| Event or call | Decision | Upstream | Status | Bytes |
|---|---|---|---|---|
| `run_start`, 0 connections | | | | |
| `POST /api/v3/onezone/tokens/infer_access_token_scope`, tag `scope` | allow | new | 401 | 921 |
| `POST /api/v3/oneprovider/lookup-file-id/alpha-space/hello.txt`, tag `lookup` | allow | new | 200 | 193 |
| `GET /api/v3/oneprovider/data/<id>/content`, tag `content` | allow | new | 200 | 64 |
| `teardown_complete`, 0 connections | | | | |

No error, reset or upstream retry was counted, and the gate stopped with exit code 0. The log records the 401's body
only by its kind (`other`), so its error id is **unknown**. The 401 is Onezone's own answer: the gate allowed the call
and forwarded it on a new upstream connection, and it answers 400 itself when it cannot put its token in a scope body.

## What the 401 points to

A new cause, not #203's. The gate sends its token as `X-Auth-Token` on every upstream call, the zone's scope inference
included (`sandbox/gate.py`, `_fwd_headers`), besides putting it in that call's body. **[inference]** Onezone
authenticates the call by the header and refuses it: the data-path token carries data-access caveats (`data.path` to
the study space, read-only, interface `rest`), and on this build such a token is usable for data access alone (#177:
its writes answered 401 `unauthorized`, `tokenCaveatUnverified`). The same kind of token in the body alone, with no
header, is answered 200: the platform's health check reads the scope that way, with a data-path token
(`health.py`, `client.py` `query`), and the day-0 `health` check passed on 11 Nov (`day0-20261111T144521Z.md`). A
token whose caveat fails inside the body is answered 400, not 401 (`client.py`). The error id would confirm the
cause; it was not recorded.

## The federation

| Step | Result |
|---|---|
| `up` | 245 s (prepare 3 s, zone 66 s, providers 79 s, spaces 8 s, baseline 89 s) |
| `down` | 35 s (containers 33 s, data 2 s) |
| Peak memory at down | `pine` 1.15 GiB, `birch` 0.99 GiB, `cedar` 0.98 GiB, `zone` 0.93 GiB |
| After down | no container, network or volume of the project or the sandbox; the root gone; no anonymous volume before or after |
