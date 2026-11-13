# Provenance: new
#   merged: [S1] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:113-117,197-204, day0/scripts/od.py:63-77
#   merged: [S2] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:120, day0/scripts/od.py:122
#   merged: [S3] icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:342,345
#   merged: [S4] icsa-2027-experiments @ 6fc798cf, icsa/knowledge/live.py:36,52, icsa/knowledge/tokencheck.py:47-49
#   merged: [S5] onedata-mcp @ 0cc17ce, onedata_mcp/api/tokens.py:24-25, test/unit/api/test_tokens.py:20
#   merged: [S6] icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:494-505, icsa/harness/health.py:57-64
#   merged: [S7] onedata-mcp @ 0cc17ce, onedata_mcp/api/spaces.py:90
#   merged: [S8] icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:114-139,170-171,246-247
#   merged: [S8] icsa-2027-experiments @ 6fc798cf, icsa/scripted/mplus.py:144-152
#   merged: [S9] icsa-2027-experiments @ 6fc798cf, icsa/sandbox/mock_onedata.py:78-95
#   merged: [S10] onedata-mcp @ 0cc17ce, onedata_mcp/api/transfers.py:28-63, benchmark/fixture_runner.py:269-281
#   merged: [S10] onedata-mcp @ 0cc17ce, benchmark/oracles/placement.py:432-440
#   merged: [S11] icsa-2027-experiments @ 6fc798cf, icsa/scripted/provision.py:215-216,236-240
"""The one HTTP client of the platform layer, and the gate every mutation goes through (O09).

Every request the layer sends leaves through `OnezoneClient`; no other module imports httpx (a test holds that). The
wrapper keeps its `httpx.Client` private, so a caller cannot send around it, and the client is injectable: a test hands
it one built on a mock transport.

Reads and mutations are separate calls:
- `get` is a GET, and `query` a POST from a short list that only reads (examining a token, inferring its scope);
- `mutate` is any other method. It takes no name from its caller: the path must lie under `/spaces/{space id}`, the
  client reads that space's name from Onezone (`space_name`) right before sending and refuses unless the name starts
  with one of the configured prefixes (`[space] prefixes`). The name is read afresh for every mutation and never
  cached, because a space can be renamed out of the study's scope after an earlier read; a cache would go on
  authorising it. A file addressed by id (`/data/{id}`, a transfer job's `fileId`, a range read) is first resolved to
  the id of the space it lies in, by walking its parent links up to the root of a space the configuration knows
  (`file_space_id`, below), and that space's name is then read and checked the same way, afresh every time. Anything
  else, a path of a provider among it, resolves to no space and is refused, so a mistaken or hostile target cannot
  reach a space outside the study's. What remains is the interval between the name's read and the platform acting on
  the request, which a client cannot close;
- `mint_own_token`, `own_temporary_generation` and `revoke_own_temporary_tokens` are the operations on credentials,
  each sent as one platform user with its own password (HTTP Basic), on that user's own temporary tokens alone: the
  user mints one, as the earlier code did, reads their generation, or revokes all of them, which needs no privilege
  and reaches no other user's (C19). They act on no space, so they have calls of their own; the password is
  registered as a secret when it is read and the minted value before the call returns (R12), and so is the admin token
  the client itself uses, when the client is built.

**The space of a file.** Onedata 25.0 gives a file no path and no space attribute, so the gate walks the file's
parent links: `GET /data/{id}?attribute=parent_id` through the provider the request is for, from the file up, until it
meets the root of a space the configuration knows (`[space] id`, or each `[[spaces]]` entry's, whose root is the
`fileId` of the provider's `GET /spaces/{id}`; a space the provider answers absent is not among them). The root itself
needs no parent read. A walk that reaches the top of a tree (a null parent) without
meeting a known root, or that has followed `MAX_SPACE_DEPTH` links without meeting one (a cycle among them), resolves to
no space (`OutOfScope`). The space is never decoded from the file's id: that encoding is undocumented and may change
between releases. A file's space does not change, so what a walk read is kept, each fact as the provider that gave it,
never another's: each provider's root of each space, kept once it answered, and the parent links a walk read, kept only
once the walk reached a known root, at most `MAX_KEPT_LINKS` of them (least recently used dropped first), all dropped
by `close()`. A walk that failed or was refused keeps no link, so a later request walks again. A file moved within the
space keeps its space, so a link it left behind still leads to the right root; but a directory moved up keeps its old,
longer ancestry, so a walk that passes the bound over kept links drops them and walks once more, every link read anew,
and refuses only if that walk passes the bound too. A file resolved before through a provider costs no request to it, a
new file beside resolved ones costs one, and a cold walk costs one root read and one parent read per level. The name
is then one read on Onezone, every time.

Every read the gate makes, the walk's and the name's, is the client's own: with the admin credential, through its own
connection pool with the platform's `[tls]`, from the core's process. None passes the data gate, so none is in the
gate's log or counted in what the agent was served, and a write sent as a data-path token (`mutate(..., token=)`) is
still resolved as the admin. When one of those reads fails, the request is refused unsent with `ScopeUnread`, which
keeps the read's status, error id and transport flag; a 404 of the target or of a directory above it is `OutOfScope`.
Both are `NotSent`, so a caller can tell a request the platform never saw from one it answered.

The other halves of O09 (refuse to evict the data provider's own copy, tear down only logged ids, a teardown that finds
files left revokes nothing) belong to provisioning and the sandbox (step 4b); they call `mutate`.

A failure raises `PlatformError` with the status and Onedata's error id only. The message never holds a URL, a body or a
header, and names a provider by its position (`providers[1]`), so neither a token nor a federation name reaches a log
or a record through it.

Every REST path and payload the layer uses, with where it was confirmed. The sources `[S1]` to `[S11]` are the
`merged:` lines of this file's header (repository, commit, file:line); a path is confirmed when working code or a
client test in a source sends it and reads the answer as this layer does. `[L1]` is the live contract tests
(`tests/live/test_contract.py`) and the direct reads recorded beside them, run against Onedata 25.0, the release the
deployments of record run, on the Compose federation of `deploy/compose-federation` on 10 Nov 2026: its day-0 reports,
`day0/2026-11-10-federation-facts.md`, which says which fact a test showed and which a direct read (an eviction's
record was read directly, since no live test ran one), `day0/2026-11-10-federation-facts-parent-walk.md`, the run
after the gate's walk by parent links, with the reads that walk rests on and the posix answers of 25.0, and
`day0/2026-11-11-federation-facts-posix-and-fuse.md`, the run after the core read those answers, and
`day0/2026-11-11-federation-facts.md`, the direct reads of a transfer's record and of a directory's eviction, and
`day0/2026-11-12-federation-facts-listing-end.md`, how a children listing and a transfer listing end (O26).

- Authentication: the header `X-Auth-Token` with an access token. Confirmed by [S2].
- `get`: `GET /spaces/{space}/users/{user}/privileges` and `GET /spaces/{space}/effective_users/{user}/privileges`; the
  answer key is `privileges`. Confirmed by [S3].
- `space_name`: `GET /spaces/{space}` with the admin token, the answer key `name`. Confirmed by [L1].
- `query`: `POST /tokens/examine`, body `{"token"}`, sent with the admin token; the answer keys `id` and `caveats` are
  read. Confirmed by [S4] and [S5]; the subject's id, under `subject`, by [L1].
- `query`: `POST /tokens/infer_access_token_scope`, body `{"token"}`, no header; the answer's
  `dataAccessScope.spaces[*].name` and `.supports` and `.providers[*].domain` are read. Confirmed by [S6]; the
  providers' `name` and `online` by [L1]. A token past its time caveat is answered 400 `tokenCaveatUnverified` [L1].
  For a user's token, `dataAccessScope.spaces` is keyed by space id and holds the user's effective spaces narrowed by
  the token's data-access caveats, every one of them for a token with none: onedata/oz-worker, release/25.0 at
  03af698e5759f9db97a003047eb4e29e4ba7507f, src/logic/token_logic_plugin.erl, `infer_access_token_scope/2`, and
  onedata/ctool, release/25.0 at 019fee12a1638ca516da97a931a2d6138fa25279, src/auth/data_access_caveats.erl,
  `match_available_spaces/2`. The tool server's writing token is checked by its keys (`tokenscope.py`).
- `get`: `GET /users/{id}/effective_privileges` with the admin token, the answer key `privileges`, a user's Onezone
  admin privileges, its own and its groups': named by Onezone's REST description, onedata/onezone-swagger, release/25.0
  at 58c697695fd73ff480f4a6e5fdbf733036465398, paths/users/id/effective_privileges.yaml, which says its reader needs
  `oz_view_privileges`. No source sends it and no live run has made it, so it is in `UNVERIFIED_ENDPOINTS`.
- `get` as the token: `GET /user/effective_clusters` with the token being checked (`token=`), the answer key `clusters`,
  a list of cluster ids: the clusters the token's user is an effective member of. Onezone's REST description has no
  such read of another user, so it is the current user's, sent as that user's token: onedata/onezone-swagger,
  release/25.0 at 58c697695fd73ff480f4a6e5fdbf733036465398, paths/user/effective_clusters.yaml (operation
  `list_user_effective_clusters`, "on behalf of the current user only", the `Clusters` definition with its required
  `clusters`), and the route `/user/effective_clusters` of onedata/oz-worker, release/25.0 at
  03af698e5759f9db97a003047eb4e29e4ba7507f, src/http/rest/routes/user_routes.erl, which needs no specific privilege.
  A writing tool-server token's user must be a member of none (`tokenscope.py`). No source sends it and no live run
  has made it, so it is in `UNVERIFIED_ENDPOINTS`.
- `mint_own_token`: `POST /user/tokens/temporary` with `Authorization: Basic` of the user itself, body
  `{"type": {"accessToken": {}}, "caveats": [...]}`, answer key `token`. Confirmed by [S1].
- The revocation of a run's tokens at its end (`users.SlotTokens.revoke`, C19): `own_temporary_generation` is `GET
  /user/tokens/temporary` and `revoke_own_temporary_tokens` is `DELETE /user/tokens/temporary`, each with
  `Authorization: Basic` of the user itself, the path `mint_own_token` posts to. The DELETE gives the user a new
  secret and a generation one higher, which revokes every temporary token of that user, and answers 204; the GET
  answers `{"generation": N}`. Neither needs a privilege. A temporary token is checked by its generation, so this is
  the only revocation of one; per-token revocation exists for named tokens alone. `query` sends `POST
  /tokens/verify_access_token`, body `{"token"}`, with no header, as the scope inference is sent: it compares the
  token's generation with its user's before any caveat and answers an older one 400 `tokenRevoked`, while
  `/tokens/examine` only decodes a token and never shows it revoked. All three are named by onedata/oz-worker at
  14b5bda7d84bbfe2f25bc9bedb9033af6d1a8b55: src/http/rest/routes/token_routes.erl:163-176 (the two routes, "does not
  require any specific privileges"), src/logic/token_logic_plugin.erl:165-176 (the verification), 330-331 and
  379-380 (the revocation, authorised for the user itself), src/datastore/models/temporary_token_secret.erl:72-89 and
  199-224 (the new generation, and its read), src/auth/token_auth.erl:357-364 (generations compared first); and
  onedata/ctool's `tokenRevoked`, HTTP 400 (src/errors/types/auth/token/od_error_token_revoked.erl:50-52). No live
  run has made them, so they are in `UNVERIFIED_ENDPOINTS`.
- Provider base `/api/v3/oneprovider`, which step 4b uses; step 4a sends nothing to a provider. Confirmed by [S7].
- Step 4b2 sends to a provider (all confirmed by [S8], whose code sends them and reads the answers as the layer does):
  `GET /spaces/{space}` (the key `fileId` is the space's root; [L1] shows it the same on every provider),
  `GET /data/{id}/children` (query `limit`, `attribute`, `token`; answer `children`, `isLast`, `nextPageToken`),
  `POST /data/{parent}/children` (query `name`, `type`; answer
  `fileId`), `PUT /data/{id}/content` (query `offset`, an octet-stream body, marked so: [L1] shows 25.0 answering
  415 to one without a `Content-Type`), `GET /data/{id}`, `DELETE /data/{id}`,
  `GET /data/{id}/distribution`, `POST /transfers` (body `type`, `dataSourceType`, `fileId`, `replicatingProviderId`
  or `evictingProviderId`) and `GET /transfers/{id}`. `read_range` is `GET /data/{id}/content` with a `Range` header,
  answered with a 206 and a `Content-Range`. `content_answer` is the same call whole or with `Range: bytes=0-0`, of
  which only the status and the `Content-Type` are read: the type it measures (`precampaign.py`),
  `application/octet-stream` for both, is confirmed by [L1]. `read_content` is a run of range reads, each streamed and
  hashed as it comes (O25).
- A write of content without the `offset` query, which truncates the file first and which a restarted upload relies
  on: only the provider's API description states it, and no source sends it; see `UNVERIFIED_ENDPOINTS`.
- `file_space_id`: `GET /data/{id}` with the query `attribute=parent_id`, answered `{"parent_id": <id>}`, and
  `{"parent_id": null}` for a space's root. Confirmed by [L1] (a file two levels down, its directory and the root, on
  each of the three providers), which also refuted the `path` attribute the gate read before: [S9], the earlier
  study's mock of what the official client calls, answered it as `/{space name}/...`, and Onedata 25.0 answers
  `attribute=path` with 400 `badValueNotAllowed`. No release at hand answers it, so the gate no longer asks for it.
- The probes' transfer log (`transferlog.py`) reads `GET /spaces/{space}/transfers` on a provider (query `state`,
  `limit`, `page_token`; answer `transfers`, a list of ids, and `nextPageToken`, followed until null), confirmed by
  [S10] for the states `ongoing` and `ended`, and each id's `GET /transfers/{id}`, whose `fileId` [S10] and state and
  counters [S11] are confirmed. The state `waiting`, the listing answer's `Date` header (`get_dated`, the provider's
  system clock, which places nothing), and a record's `type`, provider ids and times (epoch seconds), for a replication
  and an eviction, are confirmed by [L1], which also shows the eviction counter named `filesEvicted` and the state
  `transferStatus`, as [S11] reads them. A job's times are stamped on the platform's global clock, so the transfer log
  and the reset's history also read Onezone's own reading of it, `GET /provider/public/get_current_time`
  (`get_dated` with `anonymous=True`, so no credential header; answer `{"timeMillis": N}`, epoch milliseconds), the
  public call the providers synchronise their clocks to: onedata/oz-worker at 14b5bda7d84bbfe2f25bc9bedb9033af6d1a8b55,
  src/http/rest/routes/provider_routes.erl:192-198 (the route, "does not require any specific privileges"),
  src/logic/provider_logic_plugin.erl:304-305 (answers `global_clock:timestamp_millis()`) and :491-492 (authorised for
  any client), src/http/rest/translators/provider_rest_translator.erl:128-129 (the `timeMillis` body) and
  src/logic/provider_logic.erl:548-554 ("used by providers to synchronize clocks with onezone"). No live run has made
  it, so it is in `UNVERIFIED_ENDPOINTS`; the `Date` header of the same answer is recorded beside it as a cross-check.
- The reset (`reset.py`, `eviction.py`, `occupancy.py`) sends a `POST /transfers` eviction of a directory, which covers
  every file under it, confirmed by [L1] (`day0/2026-11-11-federation-facts.md`: the mirror's block map of a file in
  the directory was empty once the directory's eviction ended), and reads a directory's `GET /data/{id}/distribution`
  (per provider `distributionPerStorageBackend.{storage}.physicalSize` with `success`), its shape confirmed by [L1]
  (`day0/2026-11-11-federation-facts-posix-and-fuse.md`). Its other calls no source sends and no live run has made yet,
  named only by Oneprovider 25.0's REST description, so each is in `UNVERIFIED_ENDPOINTS`: `GET
  /data/{id}/qos/summary` (answer `requirements`, keyed by requirement id), `GET` and `DELETE /qos_requirements/{qid}`
  (answer `fileId`), `POST /qos_requirements` (body `fileId`, `expression`; a live test's only), `DELETE
  /transfers/{id}` (a cancel: 204, or 400 for a job that has ended) and, with `[placement] qos_scan = "listing"`, the
  recursive listing `GET /data/{id}/files` (query `attributes`, `include_directories`, `limit`, `token`; answer
  `files`, `inaccessiblePaths`, `nextPageToken`), read from op-worker's source (`resetqos.py`). A cancel's and a
  removal's space is read from the
  record right before the mutation: a transfer's `spaceId`, which [L1] shows in a record, or the space of a
  requirement's `fileId`, found by its parent links (`_record_space`). A caller bound to one space (`space`, a
  provisioner's own) has every target checked against that space alone, its id, its name and its own prefixes.
(All Onezone paths are under `/api/v3/onezone`, confirmed by [S1].)

The gate's own reads, the ones that find a target's space, are plumbing: a caller that is the harness's own passes its
policy (`gate_retry` of `mutate`, `read_range` and `read_content`; `retry` of `space_name`, `file_space_id` and
`file_space_name`), and a read that timed out is tried again, boundedly and in the log (`readretry.py`, O29), while a
caller that measures the platform leaves the default, one attempt. The request the gate guards is never retried. The
gate stays closed: each attempt that is answered is read afresh and decides, and after the last timeout the mutation
or the range read is refused as not sent (`ScopeUnread`).

A token ends with the lifetime it was minted with, which derives from the attempt's whole-time ceiling (O06), unless
its user revokes it first: the one revocation this client sends is a slot user's own, of all its temporary tokens, at
the end of a run that held the slot (C19). `UNVERIFIED_ENDPOINTS` lists what neither a source nor [L1] confirms: only
the reset's QoS calls and its cancel, the truncation by a write of content without an offset, the reads of a user's
Onezone admin privileges and of the clusters a token's user is a member of, Onezone's reading of the global clock, and
the three calls of a run's revocation, since [L1] confirmed every other call and detail the layer relies on.
Each entry, and each call [L1] confirmed, keeps its live contract test (`tests/live/test_contract.py`), which fails if a
deployment answers otherwise.
"""

import base64
import hashlib
import logging
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from expcore.config.loading import ConfigError
from expcore.records.redact import register_secret
from expcore_onedata.config import PlatformConfig, Space
from expcore_onedata.credentials import Principal
from expcore_onedata.readretry import ONE_ATTEMPT, Attempt, Exhausted, ReadRetry, Trace, run_attempts
from expcore_onedata.streamed import Digest, media_type

API_ZONE = "/api/v3/onezone"  # [S1] the prefix of every Onezone call
API_PROVIDER = "/api/v3/oneprovider"  # [S7]
AUTH_HEADER = "X-Auth-Token"  # [S2]
MUTATING = ("POST", "PUT", "PATCH", "DELETE")
UPLOAD_CONTENT_TYPE = "application/octet-stream"  # of every raw body a mutation sends; [L1]: 25.0 refuses one without
# POSTs that only read, and whether they are sent with the admin credential: the examination is [S4], [S5], called as
# an admin; the scope inference is [S6], called without a header, and so is the verification of an access token, which
# a revocation is confirmed by (C19).
VERIFY_TOKEN_PATH = "/tokens/verify_access_token"
READING_POSTS: Mapping[str, bool] = {
    "/tokens/examine": True,
    "/tokens/infer_access_token_scope": False,
    VERIFY_TOKEN_PATH: False,
}
# A user's own temporary tokens: POST mints one, GET reads their generation, DELETE revokes all of them (HTTP Basic).
TEMPORARY_TOKENS_PATH = "/user/tokens/temporary"
# Onezone's reading of the global clock, which a job's times are stamped on: a GET it answers to anyone, so `get_dated`
# sends it with no credential header when asked to (`anonymous`), and for this path alone.
ZONE_TIME_PATH = "/provider/public/get_current_time"
PUBLIC_READS: tuple[str, ...] = (ZONE_TIME_PATH,)

# What no source and no live run confirms, or a live run refuted: (method, path, what). Each has a test in
# tests/live/test_contract.py marked `covers:` with its method and path; a unit test holds that none is missing there.
# The entries [L1] confirmed left the list, and so did the `path` attribute it refuted, which the gate no longer reads;
# their live tests stay, so a later release that answers otherwise fails. What is left are the reset's calls that no
# live run has made yet, the truncating write of content an upload's restart relies on, and the reads of a writing
# tool-server user's Onezone admin privileges and of the clusters its token's user is a member of, and Onezone's
# reading of the global clock, which the transfer log and the reset's history place their window by.
UNVERIFIED_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    (
        "PUT",
        "/data/{id}/content",
        "a provider call: a write without the offset query truncates the file to the bytes it sends, which a "
        "restarted upload relies on; named only by the provider's API description (25.0), and every source sends an "
        "offset [S8]",
    ),
    (
        "DELETE",
        "/transfers/{id}",
        "a provider call: the cancel of a transfer that has not ended, answered 204, and 400 for one that has; named "
        "only by the provider's API description (25.0), which the reset's transfer stage relies on",
    ),
    (
        "GET",
        "/data/{id}/qos/summary",
        "a provider call: the requirements on an entry and its ancestors, keyed by requirement id under requirements, "
        "which the reset's QoS stage lists; named only by the provider's API description (25.0)",
    ),
    (
        "GET",
        "/qos_requirements/{qid}",
        "a provider call: a requirement's fileId, which the gate of its removal resolves its space by; named only by "
        "the provider's API description (25.0)",
    ),
    (
        "DELETE",
        "/qos_requirements/{qid}",
        "a provider call: the removal of a requirement, answered 204, after which reading it answers that it is not "
        "there; named only by the provider's API description (25.0)",
    ),
    (
        "GET",
        "/data/{id}/files",
        "a provider call: the recursive listing the reset's QoS scan reads with qos_scan = listing, with "
        "include_directories, which the provider's API description omits, and effQosInheritancePath; read from the "
        "source of the federation's build (resetqos.py), never sent by a live run, and not the default until it is",
    ),
    (
        "GET",
        "/users/{id}/effective_privileges",
        "a Onezone call with the admin token: a user's effective admin privileges in Onezone under privileges, which "
        "a writing tool-server user must have none of; named only by Onezone's REST description (25.0), which says "
        "the reader needs oz_view_privileges",
    ),
    (
        "GET",
        "/user/effective_clusters",
        "a Onezone call sent as the token being checked: the clusters its user is an effective member of under "
        "clusters, which a writing tool-server user must have none of; named only by Onezone's REST description "
        "(25.0), which says it is for the current user, and by oz-worker's route of that path",
    ),
    (
        "GET",
        ZONE_TIME_PATH,
        "a Onezone call sent with no credential: the global clock, which a job's times are stamped on, in epoch "
        "milliseconds under timeMillis, which the transfer log and the reset's history place their window by; named "
        "only by oz-worker's source (14b5bda, provider_routes.erl:192-198), which says it needs no privilege",
    ),
    (
        "GET",
        TEMPORARY_TOKENS_PATH,
        "a Onezone call sent as the user itself (HTTP Basic): the generation of its temporary tokens under "
        "generation, read before and after a revocation, which must make it rise; named only by oz-worker's source "
        "(14b5bda, token_routes.erl:163-169, temporary_token_secret.erl:199-224)",
    ),
    (
        "DELETE",
        TEMPORARY_TOKENS_PATH,
        "a Onezone call sent as the user itself (HTTP Basic), with no privilege: revokes every temporary token of "
        "that user by a new generation, answered 204, at the end of a run that held the user's slot; named only by "
        "oz-worker's source (14b5bda, token_routes.erl:170-176, token_logic_plugin.erl:330-331, 379-380)",
    ),
    (
        "POST",
        VERIFY_TOKEN_PATH,
        "a Onezone call sent with no credential, as the scope inference is: a token older than its user's generation "
        "answered 400 tokenRevoked before any caveat is checked, which confirms a revocation; named only by "
        "oz-worker's source (14b5bda, token_logic_plugin.erl:165-176, token_auth.erl:357-364) and ctool's error",
    ),
)
# The most parent links the gate follows from a file up to its space's root. A file deeper than this, or links that
# loop, resolve to no space: the walk is bounded in requests, and a study's tree is far shallower.
MAX_SPACE_DEPTH = 64
# The most parent links one client keeps, least recently used dropped first. A link is two ids of a few hundred bytes
# at most, so this stays under some tens of megabytes over a whole campaign; a dropped link is read again when needed.
MAX_KEPT_LINKS = 50_000
_ERROR_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_ERRNO = re.compile(r"[a-z][a-z0-9_]{0,31}")  # a POSIX code as Onedata writes it: enoent, eacces
# How Onedata 25.0 answers a file or directory that is not there, beside the 404 other answers use ([L1]).
POSIX_ABSENT = (400, "posix", "enoent")
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_FILE_ID = re.compile(r"[A-Za-z0-9_=-]{1,512}")  # a file's object id: one plain path segment
_SPACE_ID = re.compile(r"[A-Za-z0-9_@=-][A-Za-z0-9_.@=-]{0,127}")  # one plain path segment, never "." or ".."
DEFAULT_TIMEOUT_S = 15.0
log = logging.getLogger(__name__)
# The only failures a read is retried on (`readretry.py`): a wait that ran out for the answer, the connection or a slot
# of the pool. A write timeout, an error answer and every other transport failure are final.
RETRIED_TIMEOUTS = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout)
ERROR_BODY_BYTES = 64 * 1024  # the most of a streamed answer's error body read to find Onedata's error id


@dataclass(frozen=True)
class _GateReads:
    """How the gate's own reads are made: the policy for a read that timed out, and the `deadline_s` that bounds each
    read's retries (`readretry.py`). Every internal call of the gate names it, so none can be made without a choice.

    `bound` is the caller's `deadline_s`, the seconds each read may have, or, for a call with one deadline over a gate
    of several reads, a function that says how many are left now. `deadline_s` is read when a read begins, so each read
    of the gate has the time left at its own start and not a share worked out once for them all."""

    retry: ReadRetry = ONE_ATTEMPT
    bound: float | Callable[[], float] | None = None

    @property
    def deadline_s(self) -> float | None:
        """The seconds a read that begins now may spend, its retries and pauses included; None for no deadline."""
        return self.bound() if callable(self.bound) else self.bound


_ONE_ATTEMPT = _GateReads()  # the choice of whatever measures the platform, and the default of every caller


_REQUEST_COUNT = re.compile(r"Request Count: (\d+)$")  # the end of httpcore's `ConnectionInterface.info()`


def _build_http(config: PlatformConfig, timeout_s: float) -> httpx.Client:
    """The HTTP client the layer sends through when none is injected, checking certificates as `[tls]` says. A retried
    read is sent through one more of them, made for that read alone, so it has a connection of its own
    (`readretry.py`)."""
    return httpx.Client(timeout=timeout_s, verify=config.tls.client_verify())


class PlatformError(RuntimeError):
    """A request failed or was answered with something unreadable; the message holds no URL, body or header.

    `code` is Onedata's error id and `errno` the POSIX code it gives with the id `posix` (`enoent`, `eacces`), each
    kept only when it has the plain shape of one; neither ever holds a value."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        transport: bool = False,
        errno: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.errno = errno
        self.transport = transport  # True only when no answer came back: a reset, a refusal or a timeout (O08)


class NotSent(PlatformError):
    """A request the client refused, or could not check, before sending it: the platform never saw it."""


class MintNotSent(NotSent):
    """A token request failed before it was sent (no credential for the user), so no token can exist."""


class OutOfScope(NotSent):
    """A mutation's target is no study space, or a path leaves the API; the mutation was not sent."""


class ScopeUnread(NotSent):
    """A read the gate makes to find a request's target space failed, so the request was not sent (fail closed).

    It keeps that read's status, error id and transport flag, so a caller can tell an outage from an answer."""


def _error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    """Return Onedata's error id and its POSIX errno from an error answer, each None unless it has a plain shape."""
    try:
        error = response.json()["error"]
        code = error["id"]
    except (ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(code, str) or not _ERROR_ID.fullmatch(code):
        return None, None
    details = error.get("details") if isinstance(error, dict) else None
    errno = details.get("errno") if isinstance(details, dict) else None
    return code, errno if isinstance(errno, str) and _ERRNO.fullmatch(errno) else None


def _answered(label: str, method: str, status: int, code: str | None, errno: str | None) -> str:
    """The message of an error answer: the label, the method, the status, and the error id with its errno."""
    shown = f"{code}: {errno}" if code and errno else code
    return f"{label}: {method} answered {status}" + (f" ({shown})" if shown else "")


def answered_absent(error: PlatformError) -> bool:
    """Tell whether a provider answered that a file or directory is not there: a 404, or exactly `POSIX_ABSENT`.

    Any other answer, another 400 or one whose body could not be read included, is not absence, and neither is a
    request the client never sent (`NotSent`), which nothing answered. The answer is one provider's: it says nothing
    of another provider or of the storage behind it, where a directory's delete may still be under way (#114)."""
    if isinstance(error, NotSent):
        return False
    return error.status == 404 or (error.status, error.code, error.errno) == POSIX_ABSENT


def _streamed_error_fields(response: httpx.Response) -> tuple[str | None, str | None]:
    """Return Onedata's error id and its POSIX errno from a streamed error answer, as `_error_fields` does, reading at
    most `ERROR_BODY_BYTES` of it."""
    body = b""
    for piece in response.iter_bytes():
        body += piece
        if len(body) > ERROR_BODY_BYTES:
            return None, None
    return _error_fields(httpx.Response(response.status_code, content=body))


def _check_path(path: str) -> str:
    """Refuse a path that is not a plain absolute path of the API, so a caller cannot name another host or climb out."""
    if not path.startswith("/") or path.startswith("//") or "://" in path or ".." in path.split("/") or "\\" in path:
        raise OutOfScope("the request path is not a plain path of the API")
    return path


def _target_space_id(path: str) -> str:
    """Return the id of the space a mutation's path lies under, or raise OutOfScope when it lies under none.

    Only `/spaces/{id}` and what is below it name a space. The path must be plain, with no percent-escape, query or
    fragment that the server could read as another path than the one read here.
    """
    if any(mark in path for mark in "%?#"):
        raise OutOfScope("the request path of a mutation must be plain, without an escape, query or fragment")
    parts = path.split("/")
    if len(parts) < 3 or parts[1] != "spaces" or not _SPACE_ID.fullmatch(parts[2]) or "" in parts[2:-1]:
        raise OutOfScope("the mutation does not lie under a space, so it resolves to no space; nothing was sent")
    return parts[2]


def _plain(path: str) -> list[str]:
    """Split a mutation's path, refusing an escape, query or fragment the server could read as another path."""
    if any(mark in path for mark in "%?#"):
        raise OutOfScope("the request path of a mutation must be plain, without an escape, query or fragment")
    return path.split("/")


def _parent_link(answer: Any) -> str | None:
    """Return the parent link of a provider's answer to `attribute=parent_id`: a plain file id, or None at the top.

    Anything else (no `parent_id` key, an empty or non-plain id, another type) is unreadable, a PlatformError, so the
    gate refuses the request unsent rather than guess where the file lies (O09)."""
    if not isinstance(answer, dict) or "parent_id" not in answer:
        raise PlatformError("the provider's answer for a parent link holds no parent_id")
    parent = answer["parent_id"]
    if parent is None:
        return None
    if not isinstance(parent, str) or not _FILE_ID.fullmatch(parent):
        raise PlatformError("the provider's parent link is not a plain file id")
    return parent


def _file_target(method: str, path: str, body: Any) -> str | None:
    """Return the file id a request acts on, or None when its path names no file.

    A file is named by `/data/{id}` or what is below it, or, for a transfer job or a QoS requirement, by the `fileId`
    in the body of a POST to `/transfers` or `/qos_requirements`. Anything else (`/lookup-file-id/..`,
    `/replicas-id/..`) names none and is refused by the caller, since nothing maps it to a space; a transfer's cancel
    and a requirement's removal are resolved through their records (`_record_target`).
    """
    parts = _plain(path)
    if len(parts) >= 3 and parts[1] == "data" and "" not in parts[2:-1]:
        return parts[2] if _FILE_ID.fullmatch(parts[2]) else None
    if method == "POST" and path in ("/transfers", "/qos_requirements") and isinstance(body, dict):
        file_id = body.get("fileId")
        return file_id if isinstance(file_id, str) and _FILE_ID.fullmatch(file_id) else None
    return None


def _record_target(method: str, path: str) -> tuple[str, str] | None:
    """Return `(kind, id)` for a DELETE of a transfer (its cancel) or of a QoS requirement (its removal), whose space
    only its record names; None for any other request. An id that is not one plain segment resolves to no space."""
    parts = _plain(path)
    if method != "DELETE" or len(parts) != 3 or parts[1] not in ("transfers", "qos_requirements"):
        return None
    if not _FILE_ID.fullmatch(parts[2]):
        raise OutOfScope("the target record's id is not a plain id, so it resolves to no space; nothing was sent")
    return parts[1], parts[2]


class OnezoneClient:
    """Reads, token operations and scoped mutations against Onezone and the declared providers."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        http: httpx.Client | None = None,
        admin_token: str | None = None,
        environ: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        kept_links: int = MAX_KEPT_LINKS,
    ) -> None:
        """Build the client; the admin credential comes from `admin_token` or else from the configured source.

        The client it builds checks certificates as the platform's `[tls]` says (`tls.py`): its own default CAs, the
        configured CA bundle alone, or not at all. An injected `http` is the caller's, with its own TLS. `kept_links`
        bounds the parent links the gate keeps (`MAX_KEPT_LINKS` by default)."""
        if kept_links < 1:
            raise ValueError("the client keeps at least one parent link")
        self._config = config
        self._http = http if http is not None else _build_http(config, timeout_s)
        self._owns_http = http is None
        self._timeout_s = timeout_s
        self._environ = environ
        credential = admin_token if admin_token is not None else config.admin.read(environ)
        register_secret(credential)
        self._admin = {AUTH_HEADER: credential}
        # What the gate's walks read and keep (`file_space_id`), each by the provider that answered it: a file's space
        # does not change. Guarded by the lock, since one client may serve several threads; only a walk that reached
        # a known root adds links, and they are dropped least recently used first past `kept_links`.
        self._parents: OrderedDict[tuple[str, str], str] = OrderedDict()  # (provider, file id) -> its parent's id
        self._kept_links = kept_links
        self._roots: dict[tuple[str, str], str] = {}  # (provider, space id) -> the space's root file id
        self._walk_lock = threading.Lock()

    def close(self) -> None:
        """Drop what the gate's walks kept, and close the connection pool when the client built it."""
        with self._walk_lock:
            self._parents.clear()
            self._roots.clear()
        if self._owns_http:
            self._http.close()

    def kept_links(self) -> int:
        """How many parent links the gate keeps now; at most `kept_links`."""
        with self._walk_lock:
            return len(self._parents)

    def __repr__(self) -> str:
        """Name the client without its credential."""
        return f"OnezoneClient(zone={self._config.zone.name!r})"

    def _base(self, provider: str | None) -> tuple[str, str]:
        """Return the base URL and the label used in messages: `zone`, or `providers[i]` for the i-th declared one.

        The label is a role or a position in the file, never the provider's name, so a message holds no federation name.
        """
        if provider is None:
            return self._config.zone.url + API_ZONE, "zone"
        if provider not in self._config.providers:
            raise OutOfScope("the request names a provider that is not declared")
        return self._config.providers[provider].url + API_PROVIDER, self._config.provider_label(provider)

    def provider_label(self, provider: str) -> str:
        """Name a declared provider for a message by its position in the file, `providers[i]`, never by its name."""
        return self._base(provider)[1]

    def _served_before(self) -> int | None:
        """How many requests the connection carrying the request just sent had served before it, from the pool's request
        count; None when the pool cannot be seen (an injected transport that has none) or when more than one connection
        is active, so that this request's own cannot be told (`readretry.py`).

        httpx keeps its pool private; the connections it lists and their `info()` ("..., ACTIVE, Request Count: N") are
        httpcore's documented view of it, and the count includes the request itself, so one less is what it served."""
        pool = getattr(getattr(self._http, "_transport", None), "_pool", None)
        connections = getattr(pool, "connections", None)
        if connections is None:
            return None
        active = [info for info in (c.info() for c in connections) if ", ACTIVE, " in info]
        found = _REQUEST_COUNT.search(active[0]) if len(active) == 1 else None
        return int(found.group(1)) - 1 if found else None

    def _request(
        self,
        method: str,
        path: str,
        *,
        provider: str | None,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
        retry: ReadRetry = ONE_ATTEMPT,
        deadline_s: float | None = None,
        fresh: bool = False,
    ) -> httpx.Response:
        """Send one request and return the answer; a transport failure or a status of 400 or more is a PlatformError.

        A transport failure (a reset, a refusal, a timeout) has `transport` set and a real answer has its status, so
        a caller that retries can tell the two apart (O08). Only a GET under a policy of more than one attempt is
        retried, and only when it timed out (`readretry.py`); anything else is sent once. With `fresh` the request,
        sent once, goes on a connection of its own, as a retry does, when the client owns its pool.
        """
        base, label = self._base(provider)
        url = base + _check_path(path)
        wait = timeout_s if timeout_s is not None else self._timeout_s

        def send(timeout: float, trace: Trace | None, new_connection: bool) -> httpx.Response:
            options: dict[str, Any] = {
                "headers": dict(headers),
                "json": json,
                "params": dict(params) if params else None,
                "content": content,
                "timeout": timeout,
                "extensions": None if trace is None else {"trace": trace},
            }
            if new_connection and self._owns_http:  # a connection of its own, never one of the pool's idle ones
                with _build_http(self._config, self._timeout_s) as one_shot:
                    return one_shot.request(method, url, **options)
            return self._http.request(method, url, **options)

        try:
            if method == "GET" and retry.attempts > 1:
                response = run_attempts(
                    send,
                    retry=retry,
                    label=label,
                    timeout_s=wait,
                    timeouts=RETRIED_TIMEOUTS,
                    deadline_s=deadline_s,
                    served=self._served_before,
                )
            elif method == "PUT" and content is not None:
                response = self._send_content(send, label, len(content), wait, fresh)
            else:
                response = send(wait, None, fresh)
        except Exhausted as spent:
            raise PlatformError(
                f"{label}: {method} failed ({type(spent.error).__name__}) after {spent.attempts} attempts",
                transport=True,
            ) from None
        except httpx.HTTPError as error:
            raise PlatformError(f"{label}: {method} failed ({type(error).__name__})", transport=True) from None
        if response.status_code >= 400:
            code, errno = _error_fields(response)
            raise PlatformError(
                _answered(label, method, response.status_code, code, errno),
                status=response.status_code,
                code=code,
                errno=errno,
            )
        return response

    def _send_content(
        self,
        send: Callable[[float, Trace | None, bool], httpx.Response],
        label: str,
        size: int,
        wait: float,
        fresh: bool,
    ) -> httpx.Response:
        """Send a write of content once, and say in the log how its connection began (`new`, or `reused` with the
        requests it had served), with the seconds it took, so that a write that stalls can be set against its connection
        (#186). The line names the provider by its label and holds no path, id or token."""
        probe, began = Attempt(self._served_before), time.monotonic()
        try:
            response = send(wait, probe.trace, fresh)
        except httpx.HTTPError as error:
            log.warning(
                "%s: a PUT of %d bytes failed (%s) after %.2f s (connection %s)",
                label,
                size,
                type(error).__name__,
                time.monotonic() - began,
                probe.connection,
            )
            raise
        log.info(
            "%s: a PUT of %d bytes was answered with %d after %.2f s (connection %s)",
            label,
            size,
            response.status_code,
            time.monotonic() - began,
            probe.connection,
        )
        return response

    def _send(
        self,
        method: str,
        path: str,
        *,
        provider: str | None,
        headers: Mapping[str, str],
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
        retry: ReadRetry = ONE_ATTEMPT,
        deadline_s: float | None = None,
        fresh: bool = False,
    ) -> Any:
        """Send one request and return the decoded JSON; map every failure to a PlatformError without detail."""
        response = self._request(
            method,
            path,
            provider=provider,
            headers=headers,
            json=json,
            params=params,
            content=content,
            timeout_s=timeout_s,
            retry=retry,
            deadline_s=deadline_s,
            fresh=fresh,
        )
        return self._decoded(response, method, provider)

    def _decoded(self, response: httpx.Response, method: str, provider: str | None) -> Any:
        """Return an answer's decoded JSON, or a PlatformError without detail when it is not JSON."""
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise PlatformError(f"{self._base(provider)[1]}: {method} answered something that is not JSON") from None

    def get_dated(
        self,
        path: str,
        *,
        provider: str | None = None,
        params: Mapping[str, Any] | None = None,
        retry: ReadRetry = ONE_ATTEMPT,
        anonymous: bool = False,
    ) -> tuple[Any, str | None]:
        """GET as `get` does with the admin credential, and return with the decoded answer its `Date` header as sent,
        or None when it has none. The transfer log and the reset's history estimate a provider's system-clock offset
        from it, and read Onezone's global clock through it (`transferlog.py`, `reset.py`). `retry` is the call site's
        choice, as for `get`: the header is the answered attempt's.

        With `anonymous` the request carries no credential header: it is for the calls Onezone answers to anyone
        (`PUBLIC_READS`, a path of Onezone's, not a provider's), so a token's caveats can never refuse it; any other
        path is refused, unsent."""
        if anonymous and (provider is not None or path not in PUBLIC_READS):
            raise OutOfScope("only a public read of Onezone's is sent with no credential; nothing was sent")
        headers = {} if anonymous else self._admin
        response = self._request("GET", path, provider=provider, headers=headers, params=params, retry=retry)
        return self._decoded(response, "GET", provider), response.headers.get("date")

    def get(
        self,
        path: str,
        *,
        provider: str | None = None,
        token: str | None = None,
        params: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        retry: ReadRetry = ONE_ATTEMPT,
        deadline_s: float | None = None,
    ) -> Any:
        """GET a path of Onezone or of a declared provider, with the admin credential or as the token given.

        The privilege reads of tokens.py and the listings, block maps and transfer records of step 4b2 are its callers.

        `retry` is the call site's choice, named by every caller: a `ReadRetry` for the harness's own plumbing, which
        tries a read that timed out again, boundedly and in the log, and `ONE_ATTEMPT` (the default) for whatever
        measures the platform, so that a timeout stays the recorded timeout it is. Of the plain reads it is on `get`
        and `get_dated` alone. `deadline_s` bounds the whole call, its retries and pauses included (`readretry.py`).
        """
        headers = {AUTH_HEADER: token} if token is not None else self._admin
        return self._send(
            "GET",
            path,
            provider=provider,
            headers=headers,
            params=params,
            timeout_s=timeout_s,
            retry=retry,
            deadline_s=deadline_s,
        )

    def query(self, path: str, body: Mapping[str, Any]) -> Any:
        """POST to one of the endpoints that only read (`READING_POSTS`); any other path is refused."""
        if path not in READING_POSTS:
            raise OutOfScope("the path is not one of the read-only POSTs; use mutate for a request that changes state")
        headers = self._admin if READING_POSTS[path] else {}
        return self._send("POST", path, provider=None, headers=headers, json=dict(body))

    def space_name(self, space_id: str, *, retry: ReadRetry = ONE_ATTEMPT, deadline_s: float | None = None) -> str:
        """Read the name Onezone gives a space now; the answer is never kept, so none can authorise a later request.

        A space Onezone does not know, or an answer without a name, raises OutOfScope; any other failure of the read
        raises PlatformError. Neither message holds the id or a name. A read that timed out is tried again under
        `retry`, within `deadline_s` (`readretry.py`), and what decides is the answer of the attempt that was answered.
        """
        return self._space_name(space_id, _GateReads(retry, deadline_s))

    def _space_name(self, space_id: str, reads: _GateReads) -> str:
        """`space_name`, its reads made as `reads` says."""
        if not isinstance(space_id, str) or not _SPACE_ID.fullmatch(space_id):
            raise OutOfScope("the target space id is not a plain id, so it resolves to no space")
        try:
            answer = self._send(
                "GET",
                f"/spaces/{space_id}",
                provider=None,
                headers=self._admin,
                retry=reads.retry,
                deadline_s=reads.deadline_s,
            )
        except PlatformError as error:
            if error.status == 404:
                raise OutOfScope("the target space is unknown to Onezone, so it resolves to no space") from None
            raise
        name = answer.get("name") if isinstance(answer, dict) else None
        if not isinstance(name, str) or not name:
            raise OutOfScope("Onezone's answer for the target space holds no name, so it resolves to no space")
        return name

    def _space_roots(self, provider: str, reads: _GateReads) -> dict[str, str]:
        """Return root file id -> space id for the spaces the configuration knows, their roots as `provider` gives them.

        The provider is asked for each declared space whose support set names it (`[[spaces]]`, or the one `[space]`
        with `[support]`): a file a provider can see lies in a space it supports (O04), and how a provider answers for a
        space it does not support is unmeasured, so the gate does not depend on it where the configuration says which
        spaces the provider supports. A declared provider that no support set names is asked for every declared space,
        as the one-space gate always asked for its space. So a file resolves to its own space whichever it is, and a
        caller bound to one space can refuse a file of another (`_require_in_scope`). A root is the `fileId` of the
        provider's `GET /spaces/{id}`, read once per space and provider and kept once it was answered. An absence
        (`answered_absent`) is a space the provider does not hold, left out and asked again next time; when the
        provider holds none of them, the target resolves to no space (OutOfScope). Any other failure, or an answer
        without a plain id, raises PlatformError and keeps nothing of that space."""
        roots: dict[str, str] = {}
        declared = self._config.spaces
        supported = [entry for entry in declared if provider in entry.support.providers] or declared
        for space_id in dict.fromkeys(entry.space.id for entry in supported):
            root = self._space_root(provider, space_id, reads)
            if root is not None:
                roots[root] = space_id
        if not roots:
            raise OutOfScope("the configured space is unknown to that provider, so the target resolves to no space")
        return roots

    def _space_root(self, provider: str, space_id: str, reads: _GateReads) -> str | None:
        """Return one declared space's root file id on a provider, kept once answered; None when it answers absent."""
        with self._walk_lock:
            root = self._roots.get((provider, space_id))
        if root is not None:
            return root
        try:
            answer = self._send(
                "GET",
                f"/spaces/{space_id}",
                provider=provider,
                headers=self._admin,
                retry=reads.retry,
                deadline_s=reads.deadline_s,
            )
        except PlatformError as error:
            if answered_absent(error):
                return None
            raise
        root = answer.get("fileId") if isinstance(answer, dict) else None
        if not isinstance(root, str) or not _FILE_ID.fullmatch(root):
            raise PlatformError(f"{self.provider_label(provider)}: the space's answer holds no plain root file id")
        with self._walk_lock:
            self._roots[(provider, space_id)] = root
        return root

    def _kept_parent(self, provider: str, node: str) -> str | None:
        """The parent link this provider gave for a file in an earlier walk, marked as just used; None if not kept."""
        with self._walk_lock:
            parent = self._parents.get((provider, node))
            if parent is not None:
                self._parents.move_to_end((provider, node))
            return parent

    def _keep(self, provider: str, links: Mapping[str, str]) -> None:
        """Keep the links a walk that reached a known root read, dropping the least recently used past the bound."""
        with self._walk_lock:
            for node, parent in links.items():
                self._parents[(provider, node)] = parent
                self._parents.move_to_end((provider, node))
            while len(self._parents) > self._kept_links:
                self._parents.popitem(last=False)

    def _forget(self, provider: str, nodes: Sequence[str]) -> None:
        """Drop the links this provider gave for these files, so the next walk reads them again."""
        with self._walk_lock:
            for node in nodes:
                self._parents.pop((provider, node), None)

    def _parent_of(self, node: str, provider: str, read: dict[str, str], reads: _GateReads) -> str | None:
        """Return a file's parent id, from what this walk read or a read now; None at the top of a tree.

        The read is `GET /data/{id}?attribute=parent_id` with the admin credential. An absence (`answered_absent`) is
        a file or directory the provider does not know, which resolves to no space (OutOfScope); any other failure, or
        an answer that is no plain id and no null, raises PlatformError. A link read now goes into `read`, which the
        caller keeps only once its walk has reached a known root."""
        if node in read:
            return read[node]
        try:
            answer = self._send(
                "GET",
                f"/data/{node}",
                provider=provider,
                headers=self._admin,
                params={"attribute": "parent_id"},
                retry=reads.retry,
                deadline_s=reads.deadline_s,
            )
        except PlatformError as error:
            if answered_absent(error):
                raise OutOfScope(
                    "the target file, or a directory above it, is unknown to its provider, so it resolves to no space"
                ) from None
            raise
        try:
            parent = _parent_link(answer)
        except PlatformError as error:
            raise PlatformError(f"{self.provider_label(provider)}: {error}") from None
        if parent is not None:
            read[node] = parent
        return parent

    def file_space_id(
        self, file_id: str, provider: str, *, retry: ReadRetry = ONE_ATTEMPT, deadline_s: float | None = None
    ) -> str:
        """Return the id of the configured space a file lies in, found by walking its parent links up to its root.

        See "The space of a file" in this module's docstring. A file id that is not plain, a file or directory on the
        way that the provider does not know, a walk that ends above every known root, and one longer than
        `MAX_SPACE_DEPTH` raise OutOfScope; a read that fails or is answered with something unreadable raises
        PlatformError. No message holds an id, a path or a name. A read of the walk that timed out is tried again under
        `retry`, within `deadline_s` (`readretry.py`).
        """
        return self._file_space_id(file_id, provider, _GateReads(retry, deadline_s))

    def _file_space_id(self, file_id: str, provider: str, reads: _GateReads) -> str:
        """`file_space_id`, its reads made as `reads` says."""
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise OutOfScope("the target file id is not a plain id, so it resolves to no space")
        roots = self._space_roots(provider, reads)
        found = self._walk(file_id, provider, roots, reads, use_kept=True)
        if isinstance(found, list) and found:  # past the bound over links kept from earlier walks, which may be stale:
            self._forget(provider, found)  # a directory moved up since keeps its old ancestry; walk once more, fresh
            found = self._walk(file_id, provider, roots, reads, use_kept=False)
        if isinstance(found, list):
            raise OutOfScope(
                f"the target file lies more than {MAX_SPACE_DEPTH} directories below a known space's root, or its "
                "parent links loop, so it resolves to no space"
            )
        return found

    def _walk(
        self, file_id: str, provider: str, roots: Mapping[str, str], reads: _GateReads, *, use_kept: bool
    ) -> str | list[str]:
        """Follow a file's parent links up to a known root and return its space's id, keeping the links it read.

        Past `MAX_SPACE_DEPTH` links it returns the files of the chain whose kept links it used, so the caller can drop
        them and walk fresh; an empty list says the walk used none, so the file itself lies too deep (or its links
        loop). A null parent raises OutOfScope."""
        read: dict[str, str] = {}  # the links this walk read: kept only once it reaches a known root
        used: list[str] = []  # the files whose kept links this walk followed
        node = file_id
        for depth in range(MAX_SPACE_DEPTH + 1):
            if node in roots:
                self._keep(provider, read)
                return roots[node]
            if depth == MAX_SPACE_DEPTH:
                break
            parent = self._kept_parent(provider, node) if use_kept else None
            if parent is not None:
                used.append(node)
            else:
                parent = self._parent_of(node, provider, read, reads)
            if parent is None:
                raise OutOfScope(
                    "the target file lies in no space this configuration knows, so it resolves to no space"
                )
            node = parent
        return used

    def file_space_name(
        self, file_id: str, provider: str, *, retry: ReadRetry = ONE_ATTEMPT, deadline_s: float | None = None
    ) -> str:
        """Return the name Onezone gives, now, the space a file lies in: its space's id (`file_space_id`), then that
        space's name, read afresh (`space_name`), so a space renamed since an earlier call changes the answer (O09).
        A read that timed out is tried again under `retry`, within `deadline_s` for each."""
        reads = _GateReads(retry, deadline_s)
        return self._space_name(self._file_space_id(file_id, provider, reads), reads)

    def _record_space(self, kind: str, record_id: str, provider: str | None, reads: _GateReads) -> tuple[str, str]:
        """Return the id and the name of the space a transfer or a QoS requirement acts in, read now from its record
        through the provider the request goes to: a transfer's `spaceId`, named by Onezone, or the space of a
        requirement's `fileId`, found by its parent links (`file_space_id`). The record is never kept. A record the
        provider answers absent (`answered_absent`), or one without the field, raises OutOfScope; any other failure of
        the read propagates. A transfer's `spaceId` is confirmed by [L1]; a requirement's record is not
        (`UNVERIFIED_ENDPOINTS`)."""
        if provider is None:
            raise OutOfScope(
                "the mutation names a record but no provider, so it resolves to no space; nothing was sent"
            )
        try:
            record = self._send(
                "GET",
                f"/{kind}/{record_id}",
                provider=provider,
                headers=self._admin,
                retry=reads.retry,
                deadline_s=reads.deadline_s,
            )
        except PlatformError as error:
            if answered_absent(error):
                raise OutOfScope("the target record is unknown to its provider, so it resolves to no space") from None
            raise
        key = "spaceId" if kind == "transfers" else "fileId"
        value = record.get(key) if isinstance(record, dict) else None
        if not isinstance(value, str) or not value:
            raise OutOfScope(f"the target record holds no {key}, so it resolves to no space; nothing was sent")
        space_id = value if kind == "transfers" else self._file_space_id(value, provider, reads)
        return space_id, self._space_name(space_id, reads)

    def _target(self, method: str, path: str, provider: str | None, body: Any, reads: _GateReads) -> tuple[str, str]:
        """Return the id and the name of the space a request acts on: the space in its path, the space of the file it
        names (`file_space_id`), or the space its transfer's or QoS requirement's record names (`_record_space`).

        A read made for it that fails is refused as not sent (ScopeUnread), keeping that read's status, error id and
        transport flag; a target that resolves to no space stays OutOfScope. A read that timed out is tried again as
        `reads` says, the gate's reads alone, and each attempt that is answered is the answer that decides: nothing an
        earlier attempt or call saw is reused, and after the last timeout the target is unread and nothing is sent."""
        parts = _plain(path)
        try:
            if len(parts) >= 3 and parts[1] == "spaces":
                space_id = _target_space_id(path)
                return space_id, self._space_name(space_id, reads)
            record = _record_target(method, path)
            if record is not None:
                return self._record_space(*record, provider, reads)
            file_id = _file_target(method, path, body)
            if file_id is None or provider is None:
                raise OutOfScope(
                    "the mutation names no space and no file, so it resolves to no space; nothing was sent"
                )
            space_id = self._file_space_id(file_id, provider, reads)
            return space_id, self._space_name(space_id, reads)
        except NotSent:
            raise
        except PlatformError as error:
            raise ScopeUnread(
                f"the gate could not read the target's space ({error}); nothing was sent",
                status=error.status,
                code=error.code,
                errno=error.errno,
                transport=error.transport,
            ) from None

    def _require_in_scope(self, target: tuple[str, str], space: Space | None) -> None:
        """Refuse a target space whose name starts with no declared space's prefix (every `[[spaces]]` entry's), and,
        for a request bound to one space (`space`), a target that is not that space: another id, another name, or a
        name outside its own prefixes, whatever the other spaces' prefixes allow."""
        space_id, name = target
        if space is not None and (space_id != space.id or name != space.name or not name.startswith(space.prefixes)):
            raise OutOfScope("the target lies in another space than the one the request is bound to; nothing was sent")
        if not name.startswith(self._config.prefixes):
            raise OutOfScope("the target space is not one of the configured prefixes; nothing was sent")

    def mutate(
        self,
        method: str,
        path: str,
        *,
        provider: str | None = None,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
        token: str | None = None,
        space: Space | None = None,
        content_type: str | None = None,
        gate_retry: ReadRetry = ONE_ATTEMPT,
        fresh: bool = False,
    ) -> Any:
        """Send a request that can change state; refuse it, before it is sent, unless it acts on a study space.

        The target is the space id in the path, the file the path or the body of a transfer job or QoS requirement
        names, or, for a transfer's cancel or a requirement's removal, the space its record names. The space id is
        resolved to a name by a read on Onezone made for this mutation alone; the caller names no space and no name,
        and no earlier answer is reused. The mutation is refused unless that name starts with a prefix of a declared
        space, and when the request names no space, no file and no such record.

        It is sent with the admin credential, or as `token` when one is given: day-0 sends a write as a data-path token
        to see it refused. The gate applies either way, and is resolved with the admin credential. A raw body
        (`content`) is sent as `content_type`, a plain `type/subtype`, or as `UPLOAD_CONTENT_TYPE` when none is named,
        since Onedata 25.0 requires one of a content write ([L1]: 415 without it). With `space` (a provisioner's, or its
        reset's, own space) the target must be that space alone: its id, its name, and its own prefixes.

        `gate_retry` is the policy of the gate's own reads, the ones that find the target's space, and of those alone:
        a read that timed out is tried again under it (`readretry.py`), each attempt answered is read afresh, and after
        the last timeout the target is unread and nothing is sent (`ScopeUnread`). The request itself is sent once,
        whatever the policy, and a caller that measures the platform leaves the default, one attempt.

        With `fresh` the request goes on a connection of its own, opened for it by a one-shot client built as the pool's
        is (the same `[tls]`) and closed when the answer is in, never on an idle connection of the pool: a write of a
        large body stalled on a reused keep-alive connection and never on a new one (#186). It is a client's own pool
        that this can steer; a client given an HTTP client by its caller sends through that, whose connections are the
        caller's. A write of content, `fresh` or not, is logged with how its connection began (`_send_content`).
        """
        if method not in MUTATING:
            raise OutOfScope(f"{method} is not a mutating method; use get or query")
        path = _check_path(path)
        media = UPLOAD_CONTENT_TYPE if content_type is None else media_type(content_type)
        self._base(provider)  # an undeclared provider is refused before anything is read
        self._require_in_scope(self._target(method, path, provider, json, _GateReads(gate_retry)), space)
        headers = {AUTH_HEADER: token} if token is not None else dict(self._admin)
        if content is not None:
            headers["Content-Type"] = media
        return self._send(
            method,
            path,
            provider=provider,
            headers=headers,
            json=json,
            params=params,
            content=content,
            timeout_s=timeout_s,
            fresh=fresh,
        )

    def read_range(
        self,
        path: str,
        *,
        provider: str,
        offset: int,
        length: int,
        timeout_s: float | None = None,
        space: Space | None = None,
        gate_retry: ReadRetry = ONE_ATTEMPT,
    ) -> bytes:
        """Read `length` bytes from `offset` of a file's content through a declared provider; return the bytes.

        This is a read, but a read through a provider that lacks the bytes makes it fetch them: the file's block map on
        that provider grows (on-the-fly replication). It changes state, so it passes the same gate as a mutation: the
        path must be `/data/{id}/content`, and the file's space must have a name that starts with a configured prefix,
        or nothing is sent; with `space`, that space alone, as for `mutate`. The answer must be a 206 whose
        `Content-Range` starts at `offset` and matches the body, and the body must be `length` bytes unless the range
        ends at the end of the file; anything else is refused. [S8]

        `gate_retry` is the policy of the gate's reads alone, as for `mutate`; the range read itself is one request,
        since it may have made the provider fetch bytes.
        """
        parts = _plain(path)
        if len(parts) != 4 or parts[1] != "data" or parts[3] != "content":
            raise OutOfScope("a range read is of /data/{id}/content only")
        if offset < 0 or length < 1:
            raise OutOfScope("a range read needs a non-negative offset and a positive length")
        path = _check_path(path)
        self._base(provider)
        self._require_in_scope(self._target("GET", path, provider, None, _GateReads(gate_retry)), space)
        headers = {**self._admin, "Range": f"bytes={offset}-{offset + length - 1}"}
        response = self._request(
            "GET", path, provider=provider, headers=headers, timeout_s=timeout_s, retry=ONE_ATTEMPT
        )
        label = self._base(provider)[1]
        found = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
        if response.status_code != 206 or found is None:
            raise PlatformError(f"{label}: a range read was not answered with a 206 and a Content-Range")
        first, last, total = (int(found.group(i)) for i in (1, 2, 3))
        body = response.content
        if (
            first != offset
            or last - first + 1 != len(body)
            or len(body) > length
            or (len(body) < length and last + 1 != total)
        ):
            raise PlatformError(f"{label}: a range read was answered with another range than the one asked for")
        return body

    def read_content(
        self,
        path: str,
        *,
        provider: str,
        size: int,
        chunk_bytes: int,
        deadline_s: float,
        sink: Callable[[bytes], None] | None = None,
        timeout_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        space: Space | None = None,
        gate_retry: ReadRetry = ONE_ATTEMPT,
    ) -> Digest:
        """Read a file of `size` bytes through a declared provider, range by range, hashing it as it comes; return the
        size and the SHA-256 of what was read (O25).

        Nothing holds the file: each range of at most `chunk_bytes` is a streamed GET whose pieces, as the connection
        delivers them, are hashed, handed to `sink` when one is given, and dropped. Each range passes the gate of
        `read_range`, the file's space read afresh before it, since a read through a provider that lacks the bytes
        makes it fetch them; with `space`, that space alone, as for `mutate`.

        Bounds. On bytes: every answer must be a 206 whose `Content-Range` is exactly the range asked for, of a file of
        `size` bytes, carrying exactly that range's bytes; a byte more is refused as soon as it arrives. On time: the
        deadline, `deadline_s` from the start, is checked before every range and after every piece, and no single wait
        for the platform lasts longer than the smaller of `timeout_s` and what remains of the deadline, so the read
        ends within twice the deadline whatever the platform does. A refusal raises PlatformError (OutOfScope before
        anything is sent); what `sink` received by then is not the file. A file of no bytes needs no range, and its
        space is still checked, so a read of a file outside the study never succeeds.

        `gate_retry` is the policy of the gate's reads alone, as for `mutate`. A range's gate makes several reads, and
        each is given what remains of the deadline when that read begins: its retries and pauses stay within it, so
        they never carry the gate past the deadline, and a read that begins after it was passed is made once. A range
        is streamed once, whatever the policy.
        """
        parts = _plain(path)
        if len(parts) != 4 or parts[1] != "data" or parts[3] != "content":
            raise OutOfScope("a content read is of /data/{id}/content only")
        if size < 0 or chunk_bytes < 1 or not deadline_s > 0:
            raise OutOfScope("a streamed read needs its bounds: a size of zero or more, a positive chunk and deadline")
        path = _check_path(path)
        base, label = self._base(provider)
        hasher, began = hashlib.sha256(), clock()
        late = f"{label}: a streamed read passed its deadline of {deadline_s:g} s"
        # Every read of every gate is asked what is left as it begins: one number worked out for the gate would give
        # each of its several reads the whole of it, and a gate of k reads could spend k times the deadline.
        gate = _GateReads(gate_retry, lambda: deadline_s - (clock() - began))

        def consume(piece: bytes) -> None:
            hasher.update(piece)
            if sink is not None:
                sink(piece)
            if clock() - began > deadline_s:
                raise PlatformError(late)

        if size == 0:
            self._require_in_scope(self._target("GET", path, provider, None, gate), space)
        for first in range(0, size, chunk_bytes):
            self._require_in_scope(self._target("GET", path, provider, None, gate), space)
            remaining = deadline_s - (clock() - began)
            if remaining <= 0:
                raise PlatformError(late)
            wait = min(timeout_s if timeout_s is not None else self._timeout_s, remaining)
            self._stream_range(base + path, label, (first, min(chunk_bytes, size - first), size), wait, consume)
        return Digest(size, hasher.hexdigest())

    def _stream_range(
        self, url: str, label: str, span: tuple[int, int, int], wait: float, consume: Callable[[bytes], None]
    ) -> None:
        """Stream one range, `span` = (first byte, length, the file's size), to `consume` piece by piece; refuse an
        answer that is not exactly that range of a file of that size."""
        first, length, total = span
        last = first + length - 1
        got = 0
        try:
            with self._http.stream(
                "GET", url, headers={**self._admin, "Range": f"bytes={first}-{last}"}, timeout=wait
            ) as response:
                status = response.status_code
                if status >= 400:
                    code, errno = _streamed_error_fields(response)
                    raise PlatformError(
                        _answered(label, "GET", status, code, errno), status=status, code=code, errno=errno
                    )
                found = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if status != 206 or found is None:
                    raise PlatformError(f"{label}: a streamed read's range was not answered with a 206 and a range")
                if tuple(int(found.group(i)) for i in (1, 2, 3)) != (first, last, total):
                    raise PlatformError(f"{label}: a streamed read's range was answered with another range or size")
                for piece in response.iter_bytes():
                    got += len(piece)
                    if got > length:
                        raise PlatformError(f"{label}: a streamed read's range carried more bytes than it asked for")
                    consume(piece)
        except httpx.HTTPError as error:
            raise PlatformError(f"{label}: GET failed ({type(error).__name__})", transport=True) from None
        if got != length:
            raise PlatformError(f"{label}: a streamed read's range carried fewer bytes than it asked for")

    def content_answer(
        self, path: str, *, provider: str, first_byte: bool, timeout_s: float | None = None
    ) -> tuple[int, str | None]:
        """Ask a provider for a file's content, whole or its first byte; return the answer's status and Content-Type.

        The body is never read: the answer is closed once its head has arrived, so a large file costs nothing. It passes
        the same gate as `read_range`, since a read through a provider that lacks the bytes fetches them: the path must
        be `/data/{id}/content` and the file's space must have a configured prefix, or nothing is sent. A transport
        failure or a status of 400 or more is a `PlatformError`. The pre-campaign check is its caller (O05).
        """
        parts = _plain(path)
        if len(parts) != 4 or parts[1] != "data" or parts[3] != "content":
            raise OutOfScope("a content read is of /data/{id}/content only")
        path = _check_path(path)
        base, label = self._base(provider)
        self._require_in_scope(self._target("GET", path, provider, None, _ONE_ATTEMPT), None)
        headers = {**self._admin, **({"Range": "bytes=0-0"} if first_byte else {})}
        try:
            with self._http.stream(
                "GET", base + path, headers=headers, timeout=timeout_s if timeout_s is not None else self._timeout_s
            ) as response:
                status, content_type = response.status_code, response.headers.get("Content-Type")
                code, errno = (None, None)
                if status >= 400 and response.read() is not None:
                    code, errno = _error_fields(response)
        except httpx.HTTPError as error:
            raise PlatformError(f"{label}: GET failed ({type(error).__name__})", transport=True) from None
        if status >= 400:
            raise PlatformError(_answered(label, "GET", status, code, errno), status=status, code=code, errno=errno)
        return status, content_type

    def mint_own_token(self, principal: Principal, caveats: Sequence[Mapping[str, Any]]) -> str:
        """Mint a temporary access token for a user, as that user, with exactly these caveats; return the value.

        The user authenticates with its own password (HTTP Basic), read from its credential file the moment it is
        needed and registered as a secret, and the minted value is registered before it is returned (R12). [S1]
        """
        body = {"type": {"accessToken": {}}, "caveats": [dict(c) for c in caveats]}
        headers = self._as_user(principal, MintNotSent, "mint a token")
        answer = self._send("POST", TEMPORARY_TOKENS_PATH, provider=None, headers=headers, json=body)
        value = answer.get("token") if isinstance(answer, dict) else None
        if not isinstance(value, str) or not value:
            raise PlatformError("zone: the answer to a token request holds no token")
        register_secret(value)
        return value

    def own_temporary_generation(self, principal: Principal) -> int:
        """Read, as the user itself, the generation of its temporary tokens: a whole number that a revocation raises.

        An answer without one is a PlatformError; a user whose credential cannot be read is `NotSent`.
        """
        headers = self._as_user(principal, NotSent, "read its tokens")
        # one attempt: a failed read writes no revoked line, so the slot stays held by its expiry (fail closed)
        answer = self._send("GET", TEMPORARY_TOKENS_PATH, provider=None, headers=headers, retry=ONE_ATTEMPT)
        generation = answer.get("generation") if isinstance(answer, dict) else None
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise PlatformError("zone: the answer to a read of a user's temporary tokens holds no generation")
        return generation

    def revoke_own_temporary_tokens(self, principal: Principal) -> None:
        """Revoke, as the user itself, every temporary token of that user, and of no other: Onezone gives the user a
        new generation, so each token of an older one is refused. It needs no privilege; a failure is a PlatformError
        (`NotSent` when the user's credential cannot be read), and only a later read of the generation confirms it."""
        headers = self._as_user(principal, NotSent, "revoke its tokens")
        self._request("DELETE", TEMPORARY_TOKENS_PATH, provider=None, headers=headers)

    def _as_user(self, principal: Principal, refused: type[NotSent], what: str) -> dict[str, str]:
        """The header that logs in as the user (HTTP Basic), its password read now; `refused` when it cannot be."""
        users = self._config.users
        if users is None:
            raise refused(f"zone: platform.toml declares no [credentials.users], so no user can {what}")
        try:
            password = users.read(principal.username, self._environ)
        except ConfigError as error:
            raise refused(f"zone: a user's credential cannot be used ({error})") from None
        basic = base64.b64encode(f"{principal.username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}


def tcp_connect(host: str, port: int, timeout_s: float) -> float:
    """Open a TCP connection to a host and return the milliseconds it took; raise OSError when it cannot be opened.

    A raw connection sends no request, so it cannot change anything; it lives here because this module is the only one
    of the layer that touches the network (a test holds that). Day-0's reachability check samples it.
    """
    began = time.perf_counter()
    with socket.create_connection((host, port), timeout=timeout_s):
        pass
    return (time.perf_counter() - began) * 1000
