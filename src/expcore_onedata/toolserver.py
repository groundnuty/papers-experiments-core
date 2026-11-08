# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/launch.py, blob dca6fb5c0ea76ff212b4b898f2cd5459614f3df0
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Start the knowledge plane for a run (#6). The same call for every option, layout, model and seed.

`knowledge_factory(cfg_knowledge, token_for_run)` returns the zero-argument factory the harness runner takes
(`RunContext.knowledge_factory`). Nothing here depends on the option: the command, the arguments, the allow-list
and the environment keys are fixed by the config and `protocol/knowledge/pin.json`; only the token value is per run.
"""
from __future__ import annotations

import base64
import os
import time
from typing import Callable, Mapping

from dataset_76.harness.knowledge import KnowledgePlane

from . import manifest, pin, tokencheck

PINNED = "@pinned"     # the value of [knowledge].command: the executable of the verified pinned clone (pin.server_command)

# Onedata federation hosts for the server child. The data provider is fr (AGENTS.md).
ONEZONE_HOST = "https://zone.example.org"
DATA_PROVIDER_NAME = "host-5.example.org"


def data_provider_url(port: int | None = None) -> str:
    """The server's ONEDATA_ONEPROVIDER_HOST: fr on the campaign's provider route (`[network] provider_port`), so nothing in an agent
    run depends on Traefik. `port` overrides it for the route-equality check only (443 = Traefik)."""
    from dataset_76.sandbox.route import TRAEFIK_PORT, configured_provider_port
    p = configured_provider_port() if port is None else port
    return f"https://{DATA_PROVIDER_NAME}" + ("" if p == TRAEFIK_PORT else f":{p}")


def scoped_caveats(space_id: str, ttl_s: int) -> list[dict]:
    """Caveats of the ONE token the server gets (used for both its Onezone and Oneprovider settings): expires, read-only
    data access, REST interface only, and only the study space's subtree. Never an unrestricted admin token."""
    return [
        {"type": "time", "validUntil": int(time.time()) + ttl_s},
        {"type": "data.readonly"},
        {"type": "interface", "interface": "rest"},
        {"type": "data.path", "whitelist": [base64.b64encode(f"/{space_id}".encode()).decode()]},
    ]


def server_environment(token: str, *, onezone: str = ONEZONE_HOST, provider: str | None = None,
                       insecure_tls: bool = False) -> dict[str, str]:
    env = {"ONEDATA_ONEZONE_HOST": onezone, "ONEDATA_ONEZONE_TOKEN": token,
           "ONEDATA_ONEPROVIDER_HOST": provider or data_provider_url(), "ONEDATA_ONEPROVIDER_TOKEN": token}
    if insecure_tls:
        env["ONEDATA_ALLOW_INSECURE_TLS"] = "true"
    return pin.server_env(env)


class FrozenKnowledgePlane(KnowledgePlane):
    """A KnowledgePlane that refuses to start unless the server reports exactly the frozen tool definitions
    (names, descriptions, input schemas). A drifted server fails the run start as an infrastructure error, not silently."""

    def __init__(self, command, args=(), env=None, allowed_tools=()):
        # No client-side filter: tool_defs is what the SERVER lists, so a server that still offers a listing tool
        # (allow-list not applied) fails check_frozen instead of being hidden by a filter.
        super().__init__(command, args, env, ())
        self.configured_allowed = tuple(allowed_tools)

    async def __aenter__(self) -> "FrozenKnowledgePlane":
        await super().__aenter__()
        try:
            check_frozen(self.tool_defs)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self


def check_frozen(tool_defs: list[dict]) -> None:
    want = {t["name"]: t for t in manifest.frozen()["tools"]}
    got = {d["name"]: d for d in tool_defs}
    if set(got) != set(want):
        raise RuntimeError(f"knowledge server exposes {sorted(got)}, frozen {sorted(want)}")
    for n, d in got.items():
        if d["description"] != want[n]["description"] or d["input_schema"] != want[n]["input_schema"]:
            raise RuntimeError(f"knowledge tool {n!r} differs from protocol/knowledge/tools.json")


def knowledge_factory(cfg: Mapping, token_for_run: Callable[[], str], *, space_id: str, verify: bool = True,
                      examine: Callable[[str], dict] | None = None, time_cap_s: float | None = None,
                      **env_kw) -> Callable[[], KnowledgePlane]:
    """`cfg` is `HarnessConfig.knowledge` (command, args, allowed_tools). The command must be the pinned server."""
    if verify:
        pin.verify_pin()
    command, launcher_args = pin.launch_spec()
    if cfg.get("command") != PINNED or list(cfg.get("args", [])):
        raise ValueError(f"[knowledge].command must be {PINNED!r} with no args (the verified clone's launcher), "
                         f"not {cfg.get('command')!r} {list(cfg.get('args', []))}")
    if tuple(cfg.get("allowed_tools", ())) != tuple(pin.allowed_tools()) and set(cfg.get("allowed_tools", ())) != set(pin.allowed_tools()):
        raise ValueError("[knowledge].allowed_tools differs from protocol/knowledge/pin.json")

    def make() -> KnowledgePlane:
        token = token_for_run()
        tokencheck.require_scoped_token(token, space_id, examine=examine, time_cap_s=time_cap_s)      # an unscoped token cannot start the plane
        return FrozenKnowledgePlane(command, launcher_args,
                              env=server_environment(token, **env_kw),
                              allowed_tools=cfg["allowed_tools"])
    return make
