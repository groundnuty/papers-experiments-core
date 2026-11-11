# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/probe_reachability.py:1-52
#   merged: icsa-2027-experiments @ 6fc798cf, day0/scripts/health_onsite.py:1-16,34-52
#   merged: icsa-2027-experiments @ 6fc798cf, day0/REPORT.md:345-358
"""The read-only day-0 checks: reachability, health, tokens and listing.

Each check reads and reports; none changes a space, a file or a privilege. Tokens are minted (the examination of a
token needs one) and, as everywhere in the layer, never revoked: they end with their short lifetime. Facts are by
provider name as platform.toml gives it, in numbers and words; no host, address or URL is ever put in a result.

Their sources: the reachability check samples TCP connect times, reads the provider's public configuration and Onezone's
online flag, as the earlier study's reachability script did; the listing check reads a directory whole and again page by
page, which the earlier report found must agree.
"""

import statistics
from typing import Any
from urllib.parse import urlsplit

from expcore_onedata.client import PlatformError
from expcore_onedata.day0.registry import Context, Result, failed, passed, register, unknown
from expcore_onedata.health import PlatformHealth
from expcore_onedata.probes import run_probe
from expcore_onedata.tokencheck import PrivilegesMismatch, TokenRefused
from expcore_onedata.tokens import Plane, data_path_spec, tool_server_spec

CONNECT_TIMEOUT_S = 5.0
LISTING_LIMIT = 1000
WALK_LIMIT = 2
WALK_CAP = 200  # a directory with more entries is not walked two at a time


def _attempts(ctx: Context) -> tuple[int, float]:
    """The probe's attempts and backoff: the placement settings when there are any, else the defaults."""
    settings = ctx.placement.settings if ctx.placement else None
    return (settings.probe_attempts, settings.probe_backoff_s) if settings else (3, 1.0)


def _probe(ctx: Context, read):
    attempts, backoff = _attempts(ctx)
    return run_probe(read, attempts=attempts, backoff_s=backoff, sleep=ctx.sleep)


def _tcp(ctx: Context, name: str) -> dict[str, Any]:
    """Sample TCP connect times to a provider; the facts hold milliseconds and counts, never the host."""
    if ctx.tcp_connect is None:
        return {"measured": False}
    parts = urlsplit(ctx.config.providers[name].url)
    host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    times: list[float] = []
    errors = 0
    for _ in range(ctx.samples):
        try:
            times.append(ctx.tcp_connect(host, port, CONNECT_TIMEOUT_S))
        except OSError:
            errors += 1
        ctx.sleep(0.1)
    return {
        "measured": True,
        "samples": len(times),
        "errors": errors,
        "min_ms": round(min(times), 2) if times else None,
        "median_ms": round(statistics.median(times), 2) if times else None,
    }


@register("reachability", "Reachability of each declared provider")
def reachability(ctx: Context) -> Result:
    """TCP connect times, the provider's public configuration and Onezone's online flag, per declared provider."""
    rows: dict[str, Any] = {}
    hard, soft = [], []
    for name, provider in ctx.config.providers.items():
        tcp = _tcp(ctx, name)
        found = _probe(ctx, lambda n=name: ctx.client.get("/configuration", provider=n))
        configuration = found.value if found.ok and isinstance(found.value, dict) else {}
        online = None
        if provider.id or name in ctx.provider_ids:
            pid = provider.id or ctx.provider_ids[name]
            seen = _probe(ctx, lambda p=pid: ctx.client.get(f"/providers/{p}"))
            online = seen.value.get("online") if seen.ok and isinstance(seen.value, dict) else None
            online = online if isinstance(online, bool) else None
        rows[name] = {
            "tcp": tcp,
            "configuration": found.to_json(),
            "version": configuration.get("version"),
            "build": configuration.get("build"),
            "online": online,
        }
        if not found.ok or tcp.get("errors") or online is False:
            hard.append(name)
        if not tcp["measured"] or online is None:
            soft.append(name)
    if hard:
        return failed(
            f"{len(hard)} of {len(rows)} provider(s) did not answer as expected: {', '.join(hard)}", providers=rows
        )
    if soft:
        return unknown(f"reachable, but {len(soft)} provider(s) could not be fully measured", providers=rows)
    return passed(f"all {len(rows)} declared provider(s) answered", providers=rows)


@register("health", "Health against the declared support set")
def health(ctx: Context) -> Result:
    """The platform's own health check: the providers supporting the space equal the declared set."""
    if ctx.config.principals.service is None:
        return unknown("no [principals] service user is declared, so the health check cannot mint its token")
    verdict = PlatformHealth(ctx.config, ctx.client, ctx.issuer, clock=ctx.now)()
    detail = verdict.detail
    return passed(detail) if verdict.ok else failed(detail)


@register("tokens", "Token minting and examination, per plane")
def tokens(ctx: Context) -> Result:
    """Mint the data-path token and each declared condition's tool-server token; each is examined fail-closed."""
    principals, space = ctx.config.principals, ctx.config.space.id
    planes: dict[str, Any] = {}
    jobs = []
    if principals.data_path is not None:
        jobs.append(("data_path", data_path_spec(space), principals.data_path))
    server = ctx.config.toolserver
    if server is not None and principals.toolserver is not None:
        jobs.extend(
            (f"tool_server:{name}", tool_server_spec(space, condition), principals.toolserver)
            for name, condition in server.conditions.items()
        )
    if not jobs:
        return unknown("no fixed [principals] user is declared for the data path, so no token can be minted")
    problems, skipped = [], []
    for label, spec, principal in jobs:
        if spec.plane == Plane.TOOL_SERVER:
            try:  # a fixed user can stand for one condition's rights only; a run leases a user that matches
                ctx.issuer.check_user(spec, principal.user_id)
            except PrivilegesMismatch:
                planes[label] = {
                    "examined": None,
                    "skipped": "the fixed tool-server user does not hold exactly these rights",
                }
                skipped.append(label)
                continue
            except (TokenRefused, PlatformError) as error:  # an outage or an unreadable answer is a failure, not a skip
                planes[label] = {"examined": False, "refusal": str(error)[:200]}
                problems.append(label)
                continue
        try:
            minted = ctx.issuer.mint(spec, principal, ceiling_s=ctx.ceiling_s)
        except (TokenRefused, PlatformError) as error:
            planes[label] = {"examined": False, "refusal": str(error)[:200]}
            problems.append(label)
            continue
        planes[label] = {
            "examined": True,
            "caveats": dict(spec.caveats.shape()),
            "seconds_left": round(minted.expires_at - ctx.now(), 1),
        }
    if problems:
        return failed(f"{len(problems)} token(s) were refused: {', '.join(problems)}", planes=planes)
    if skipped:
        return unknown(
            f"{len(skipped)} condition(s) not checked: the fixed tool-server user holds other rights",
            planes=planes,
        )
    return passed(f"{len(planes)} token(s) minted and examined against their exact caveat set", planes=planes)


def _listing(ctx: Context, provider: str, root: str) -> dict[str, Any]:
    """Read a directory by the server's own pages and again two entries at a time; the two must agree."""
    whole = _names(ctx, provider, root, LISTING_LIMIT)
    if whole is None:
        return {"entries": None, "walked": False, "consistent": None}
    walked = _names(ctx, provider, root, WALK_LIMIT)
    return {
        "entries": len(whole[0]),
        "pages": whole[1],
        "walked": walked is not None,
        "consistent": walked is not None and sorted(walked[0]) == sorted(whole[0]),
    }


def _names(ctx: Context, provider: str, root: str, limit: int) -> tuple[list[str], int] | None:
    """Follow a directory's pages to the end; return the names and the page count, or None past `WALK_CAP` entries."""
    names: list[str] = []
    token, pages = None, 0
    while True:
        params: dict[str, Any] = {"limit": limit, "attribute": ["name", "type"]}
        if token:
            params["token"] = token
        page = ctx.client.get(f"/data/{root}/children", provider=provider, params=params)
        names += [c["name"] for c in page["children"]]
        pages += 1
        if len(names) > WALK_CAP:
            return None
        if page.get("isLast", True):
            return names, pages
        token = page.get("nextPageToken")
        if not token:
            raise PlatformError("a listing is not on its last page but names no next page")


@register("content_type", "The content type of a file's content, read whole and by range on the local provider")
def content_type(ctx: Context) -> Result:
    """The pre-campaign check (`precampaign.measure_content_type`): the type the data gate streams on, when a data path
    is REST. A type that differs fails. A check that measured nothing is unknown, which is not a pass: one that could
    not read the type, and one with no REST data path to read it for, which a campaign still admits (O08)."""
    from expcore_onedata.precampaign import measure_content_type

    verdict = measure_content_type(ctx.config, ctx.client)
    if not verdict.measured:
        return unknown(verdict.detail)
    return passed(verdict.detail) if verdict.ok else failed(verdict.detail)


@register("listing", "Listing of the space root on each supporting provider")
def listing(ctx: Context) -> Result:
    """The root lists on every provider in the support set, and a paged walk agrees with the single read. A root with no
    entry has nothing to agree on, so it is unknown, never a pass."""
    from expcore_onedata import consistency

    rows: dict[str, Any] = {}
    bad = []
    for name in ctx.config.support.providers:
        try:
            root = consistency.space_root(ctx.client, name, ctx.config.space.id, tries=3, wait_s=1.0, sleep=ctx.sleep)
            rows[name] = _listing(ctx, name, root)
        except (PlatformError, KeyError, TypeError) as error:
            rows[name] = {"error": type(error).__name__, "status": getattr(error, "status", None)}
            bad.append(name)
            continue
        if rows[name]["consistent"] is False:
            bad.append(name)
    if bad:
        return failed(f"the listing failed or disagreed on {len(bad)} provider(s): {', '.join(bad)}", providers=rows)
    empty = [name for name, row in rows.items() if row["entries"] == 0]
    if empty:
        return unknown(
            f"the root lists no entry on {len(empty)} supporting provider(s): {', '.join(empty)}; no baseline file is "
            "in the space (one declared baseline = false in the federation's layout holds none until a study "
            "ingests), so there is nothing to list or to walk page by page and a pass would prove nothing",
            providers=rows,
        )
    if any(row["consistent"] is None for row in rows.values()):
        return unknown("the root lists, but it is too large to walk page by page", providers=rows)
    return passed(f"the root lists on all {len(rows)} supporting provider(s), and a paged walk agrees", providers=rows)
