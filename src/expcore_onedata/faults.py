# Provenance:
#   source: icsoc-2026-code-agent @ c1e76c2e1b7cea56a88ad70efca2d5048aa9a4ea, harness/provider_restart.py, blob 9c1013c507a9bfae7e318ba6c69bc80f68c6097b
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Provider-restart helper for the dataset_77 harness — OFFLINE-TESTABLE automation
of the operator-verified "restart the mesh together" transfer-wedge recovery
(see the `dataset-188` memory + `onedata-issues/0001`).

Mechanism (verified live 2026-07-06): SSH to the ops host — the SAME host +
option shape the harness already uses for the verifier's REST-token mint (see
`harness/verifier.py::SSH_HOST` / `KNOWN_HOSTS_PATH` + `Verifier._mint_token_ssh`).
On the ops host, each provider has a kubeconfig at `$HOME/.kube/<cfg>`, a
namespace, a single `oneprovider-0` pod, an `oneprovider` container, and an
`oneprovider` StatefulSet. Restarting ONE provider is
`kubectl delete pod oneprovider-0` — the StatefulSet recreates it
(ArgoCD-safe). Restarting the MESH (multiple providers TOGETHER) is REQUIRED
for transfer-wedge recovery: restarting one provider alone does NOT reliably
clear a wedge (live-observed 2026-07-06: both providers Ready ~65s after
delete, when deleted together).

Sequencing is load-bearing: `restart_providers` issues a `--wait=false`
delete for EVERY named provider FIRST, then polls ALL of them for
readiness — never delete-then-poll-then-delete-next, which would let an
earlier provider finish recovering while a later one hasn't even gone down
yet, defeating the "together" requirement.

This module is destructive (it deletes live provider pods) and is NEVER
auto-run: the `__main__` CLI requires explicit provider names and refuses to
default to "all providers" or run with none.

Stdlib-only. No top-level side effects on import; no network at import — the
default SSH runner is a callable FACTORY (mirrors `harness/netem.py::ssh_runner`
exactly: an injected `remote_cmd -> (rc, stdout, stderr)` callable), never
invoked at import or module-definition time. Kubeconfig CONTENTS are never
read or handled by this module — only the kubeconfig FILENAME is ever
embedded into a remote command (the ops host resolves it).
"""
from __future__ import annotations

import dataclasses
import math
import subprocess
import time
from typing import Callable, Dict, Optional

from . import verifier


class ProviderRestartError(RuntimeError):
    """Raised for a missing operator sanction (`operator_sanctioned` not exactly
    `True`), an unknown provider name, or an empty `providers` sequence. NEVER
    raised for a provider that fails to become ready within `wait_timeout_s` —
    that is a normal, reportable outcome (see the `ready: False` field
    `restart_providers` returns), not an error."""


@dataclasses.dataclass(frozen=True)
class ProviderCluster:
    """Where + how to reach one provider's kubeconfig context on the ops host.

    `kubeconfig` is the FILENAME only (e.g. "de-config") — the remote command
    always builds the full path as `$HOME/.kube/<kubeconfig>`, embedded
    UNQUOTED so the remote shell expands `$HOME` itself. This is the opposite
    convention from `netem.py`'s `ProviderTarget.kubeconfig` (which requires
    an ABSOLUTE path precisely BECAUSE its commands single-quote the value,
    which kills `~`/`$HOME` expansion on the remote host — see that module's
    docstring). Do NOT single-quote `$HOME/.kube/<kubeconfig>` here, or the
    remote shell will look for a literal path named `$HOME/.kube/...` and
    kubectl will fail."""
    provider: str
    kubeconfig: str
    namespace: str
    pod: str = "oneprovider-0"
    container: str = "oneprovider"
    statefulset: str = "oneprovider"


# Verified live 2026-07-06 provider -> (kubeconfig filename, namespace) map.
PROVIDER_CLUSTERS: Dict[str, ProviderCluster] = {
    "provider_f": ProviderCluster(provider="provider_f", kubeconfig="de-config", namespace="provider-r"),
    "provider-a": ProviderCluster(
        provider="provider-a", kubeconfig="provider-a-config", namespace="provider-t-pl"
    ),
    "provider_e": ProviderCluster(provider="provider_e", kubeconfig="fr-config", namespace="provider-s"),
    "provider_d": ProviderCluster(provider="provider_d", kubeconfig="provider_d-config", namespace="provider-u"),
    "cloud": ProviderCluster(provider="cloud", kubeconfig="cloud-config", namespace="provider-t"),
}

# jsonpath to capture a pod's IDENTITY (uid) BEFORE a restart delete. After
# the delete we require this uid to CHANGE before calling a provider ready.
# WHY (live-caught 2026-07-06): a `kubectl delete pod --wait=false` leaves the
# OLD pod `phase=Running, ready=true` for its termination grace period (a few
# seconds) BEFORE it goes Terminating. A phase:ready-only poll sees that OLD,
# still-Running pod and reports "ready" in ~4-5s WITHOUT the genuine restart
# ever happening — the real restart (fresh pod Running:true) took ~45-65s. The
# uid-change check is the race-free confirmation that we are looking at the
# NEW pod, not the lingering old one.
_IDENTITY_JSONPATH = "{.metadata.uid}"

# jsonpath to poll AFTER the delete: the pod's uid + phase + container-ready in
# ONE shot. Readiness = uid differs from the pre-delete capture AND the
# `<phase>:<ready>` tail == _READY_VALUE. A k8s uid contains no colon, so the
# combined output `<uid>:<phase>:<ready>` splits unambiguously on the FIRST
# colon (see `_is_genuinely_restarted`).
_STATUS_JSONPATH = "{.metadata.uid}:{.status.phase}:{.status.containerStatuses[0].ready}"

# The `<phase>:<ready>` tail (after the uid) that means the pod is up.
_READY_VALUE = "Running:true"


def _kubeconfig_expr(cluster: ProviderCluster) -> str:
    """`$HOME/.kube/<cfg>` — UNQUOTED (see `ProviderCluster.kubeconfig`
    docstring for why quoting it would break remote `$HOME` expansion)."""
    return f"$HOME/.kube/{cluster.kubeconfig}"


def build_delete_pod_cmd(cluster: ProviderCluster) -> str:
    """Remote shell command: delete the provider's pod WITHOUT waiting for
    termination (`--wait=false`) — load-bearing for a mesh restart. ALL named
    providers' deletes must be issued before ANY readiness poll, so the pods
    go down together rather than one recovering before the next goes down.
    The StatefulSet recreates the pod (ArgoCD-safe, self-serve)."""
    return (
        f"KUBECONFIG={_kubeconfig_expr(cluster)} kubectl -n {cluster.namespace} "
        f"delete pod {cluster.pod} --wait=false"
    )


def build_pod_identity_cmd(cluster: ProviderCluster) -> str:
    """Remote shell command: read-only capture of the pod's current identity
    (`{.metadata.uid}`) BEFORE a restart delete. The post-delete poll requires
    this uid to change before reporting ready — see `_STATUS_JSONPATH` /
    `_is_genuinely_restarted` for why (the --wait=false grace-period race)."""
    return (
        f"KUBECONFIG={_kubeconfig_expr(cluster)} kubectl -n {cluster.namespace} "
        f"get pod {cluster.pod} -o jsonpath='{_IDENTITY_JSONPATH}'"
    )


def build_pod_status_cmd(cluster: ProviderCluster) -> str:
    """Remote shell command: read-only uid + pod-phase + container-ready check
    used to poll for a GENUINE restart after a delete. Returns
    `<uid>:<phase>:<ready>` so the poller can require the uid to differ from the
    pre-delete capture AND `<phase>:<ready>` == `Running:true` (guards the
    grace-period race — see `_STATUS_JSONPATH`)."""
    return (
        f"KUBECONFIG={_kubeconfig_expr(cluster)} kubectl -n {cluster.namespace} "
        f"get pod {cluster.pod} -o jsonpath='{_STATUS_JSONPATH}'"
    )


def ssh_runner(
    ssh_host: Optional[str] = None,
    known_hosts_path: Optional[str] = None,
    timeout_s: int = 90,
) -> Callable:
    """Default runner: SSH to the ops host and run a remote shell command,
    returning (rc, stdout, stderr). Mirrors `netem.py::ssh_runner`'s exact
    factory contract (a callable taking one remote-command string, returning
    a (rc, stdout, stderr) tuple) so the two modules' runners are
    interchangeable — but additionally pins `StrictHostKeyChecking=yes` +
    `UserKnownHostsFile=<KNOWN_HOSTS_PATH>`, the fuller invocation shape
    `verifier.py` already uses for its own SSH calls (see
    `Verifier._mint_token_ssh`). Defaults come from `harness.verifier.SSH_HOST`
    / `KNOWN_HOSTS_PATH` — never hardcoded here. Kept separate + injectable so
    tests never touch SSH; never invoked at import or definition time (this
    factory only builds the closure — no subprocess runs until the returned
    callable is actually called)."""
    host = ssh_host or verifier.SSH_HOST
    known_hosts = known_hosts_path or verifier.KNOWN_HOSTS_PATH

    def _run(remote_cmd: str):
        proc = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts}", host, remote_cmd,
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
        return proc.returncode, proc.stdout, proc.stderr

    return _run


def _is_ready(phase_ready: str) -> bool:
    """True when the `<phase>:<ready>` tail is exactly `Running:true`."""
    return phase_ready.strip() == _READY_VALUE


def _is_genuinely_restarted(status_output: str, pre_uid: str) -> bool:
    """True only when the polled pod is a GENUINELY NEW pod — its uid differs
    from the pre-delete capture `pre_uid` AND it is `Running:true`. This is the
    race-free guard against the `--wait=false` grace-period window where the OLD
    pod still reports `Running:true` for a few seconds before it goes
    Terminating (live-caught 2026-07-06 — see `_IDENTITY_JSONPATH`).

    `status_output` is `<uid>:<phase>:<ready>`; a k8s uid contains no colon, so
    partitioning on the FIRST colon yields the uid and the `<phase>:<ready>`
    tail cleanly.

    If `pre_uid` is empty (the pre-delete identity capture failed — e.g. the pod
    was momentarily absent), the uid-change gate degrades to the phase:ready-only
    check for the first observed pod: no worse than the pre-fix behaviour, and
    the pre-capture normally succeeds because the pod being restarted exists."""
    new_uid, _sep, phase_ready = status_output.strip().partition(":")
    if not new_uid or new_uid == pre_uid:
        return False
    return _is_ready(phase_ready)


def restart_providers(
    providers,
    *,
    operator_sanctioned: bool = False,
    runner: Optional[Callable] = None,
    wait_timeout_s: float = 360.0,
    poll_interval_s: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> Dict[str, Dict[str, object]]:
    """Mesh-restart ALL of `providers` TOGETHER — the verified remedy for an
    inter-provider transfer wedge (restarting one provider alone does NOT
    reliably clear it; see the `dataset-188` memory +
    `onedata-issues/0001`).

    **Operator-sanction gate (structural, mirrors
    `sweep.LiveFaultInjector(operator_sanctioned=True)`):** `operator_sanctioned`
    is REQUIRED to be exactly `True` — if it is `False`, omitted, or any other
    value, this raises `ProviderRestartError` BEFORE any runner invocation and
    before any pod is deleted. Restarting Onedata providers is destructive and
    NEVER-without-operator-sanction (see `.claude/CLAUDE.md`); making the gate a
    structural precondition (rather than a positional accident of the module not
    being wired anywhere) means a future "auto-recover the wedge" driver that
    calls this without sanction fails LOUD at the boundary, not on reviewer
    memory.

    Sequencing: captures each pod's pre-delete identity (uid), issues a
    `--wait=false` delete for EVERY named provider FIRST, then polls ALL of
    them for readiness — see module docstring for why the delete-before-poll
    order is load-bearing.

    Readiness = a GENUINE restart, not just phase:ready. A provider is ready
    only when its pod's uid DIFFERS from the pre-delete capture AND it is
    `Running:true`. This guards the `--wait=false` grace-period race where the
    OLD pod lingers `Running:true` for a few seconds before going Terminating —
    a phase:ready-only check would report ready in ~4-5s without the genuine
    restart happening (live-caught 2026-07-06; real restart took ~45-65s).

    Never raises on a provider that fails to become ready within
    `wait_timeout_s` — that is a normal, reportable outcome (`ready: False`
    in the return value). Never hangs unbounded: the poll loop is bounded
    BOTH by wall-clock (`now_fn`) AND by iteration count. The iteration-count
    bound matters independently of whether `sleep_fn` actually advances
    `now_fn` — a fully-deterministic offline test can inject a no-op
    `sleep_fn` alongside a fake `now_fn` and still get a bounded, finite
    number of runner calls rather than a busy-loop.

    Returns
    `{provider: {"terminated": bool, "ready": bool, "identity_confirmed": bool, "elapsed_s": float}}`.
    `terminated` reflects whether the delete command itself was issued
    successfully (`rc == 0`) — NOT confirmed pod termination, since the
    delete is `--wait=false` by design (see `build_delete_pod_cmd`).

    `identity_confirmed` disambiguates the TWO strengths a `ready: True` can
    have (surfacing the degraded-mode mini silent-fallback):
      - `True`  — readiness confirmed via a genuine uid CHANGE (the pre-delete
        uid was captured non-empty AND the polled pod's uid differs). Full
        confidence that a real restart happened.
      - `False` — the pre-delete uid capture failed (empty `pre_uid`), so the
        gate degraded to phase:ready-only for this provider; `ready: True` is
        lower-confidence (it could in principle be a lingering old pod).
    `identity_confirmed` is only meaningful when `ready: True`; it stays
    `False` for a not-ready provider.

    Raises `ProviderRestartError` for a missing operator sanction, an unknown
    provider name, or an empty `providers` sequence — NEVER for a
    not-ready-in-time provider.

    Never reads, prints, or returns kubeconfig file CONTENTS — only the
    kubeconfig FILENAME (`ProviderCluster.kubeconfig`) is ever embedded into
    a remote command; the ops host resolves the file itself.
    """
    # Operator-sanction gate FIRST — before any runner call / any delete.
    # `is not True` rejects False, omission (default False), and truthy
    # non-True values (e.g. 1, "yes") — sanction must be the explicit bool True.
    if operator_sanctioned is not True:
        raise ProviderRestartError(
            "restart_providers refuses to run without explicit operator sanction; "
            "pass operator_sanctioned=True (restarting Onedata providers is destructive "
            "and NEVER-without-operator-sanction — see .claude/CLAUDE.md)"
        )
    if not providers:
        raise ProviderRestartError("restart_providers requires at least one explicit provider name")
    unknown = sorted(set(providers) - set(PROVIDER_CLUSTERS))
    if unknown:
        raise ProviderRestartError(
            f"unknown provider(s) {unknown!r}; known providers: {sorted(PROVIDER_CLUSTERS)}"
        )

    run = runner if runner is not None else ssh_runner()

    # Phase 0 — capture each pod's pre-delete identity (uid) so Phase 2 can
    # confirm a GENUINE restart (new uid) rather than the OLD pod lingering
    # `Running:true` through its --wait=false grace period. Best-effort: a
    # failed capture (rc != 0) records an empty uid, which degrades the Phase-2
    # gate to phase:ready-only for that provider (see `_is_genuinely_restarted`).
    pre_uid: Dict[str, str] = {}
    for p in providers:
        rc, out, _err = run(build_pod_identity_cmd(PROVIDER_CLUSTERS[p]))
        pre_uid[p] = out.strip() if rc == 0 else ""

    start = now_fn()

    # Phase 1 — issue ALL deletes before ANY readiness poll (mesh-together).
    result: Dict[str, Dict[str, object]] = {}
    for p in providers:
        rc, _out, _err = run(build_delete_pod_cmd(PROVIDER_CLUSTERS[p]))
        result[p] = {
            "terminated": rc == 0, "ready": False,
            "identity_confirmed": False, "elapsed_s": 0.0,
        }

    # Phase 2 — poll ALL pending providers for a GENUINE restart (uid changed
    # from the pre-delete capture AND Running:true), bounded by BOTH wall-clock
    # AND iteration count (see docstring above for why both).
    max_polls = max(1, math.ceil(wait_timeout_s / poll_interval_s)) if poll_interval_s > 0 else 1
    pending = dict.fromkeys(providers)  # ordered "set" (insertion order, values unused)
    for _poll in range(max_polls):
        for p in list(pending):
            rc, out, _err = run(build_pod_status_cmd(PROVIDER_CLUSTERS[p]))
            if rc == 0 and _is_genuinely_restarted(out, pre_uid[p]):
                del pending[p]
                result[p]["ready"] = True
                # Full confidence only when a non-empty pre-delete uid was
                # captured (so readiness came from a genuine uid CHANGE). An
                # empty pre_uid means the gate degraded to phase:ready-only.
                result[p]["identity_confirmed"] = bool(pre_uid[p])
                result[p]["elapsed_s"] = now_fn() - start
        if not pending:
            break
        if now_fn() - start >= wait_timeout_s:
            break
        sleep_fn(poll_interval_s)

    for p in pending:
        result[p]["elapsed_s"] = now_fn() - start
    return result


def _cli(argv, **restart_kwargs) -> int:
    """Argv-driving core, factored out of `__main__` so tests can inject a
    fake `runner` (+ optionally `wait_timeout_s`/`poll_interval_s`/`sleep_fn`/
    `now_fn`) without touching SSH. `python -m harness.provider_restart`
    calls this with no `restart_kwargs`, so the real default SSH runner is
    used.

    Operator sanction is opt-in ONLY via the explicit `--operator-sanctioned`
    flag in argv — absent, the sanction gate in `restart_providers` raises and
    a bare `python -m harness.provider_restart de provider-a` refuses. This
    mirrors the module-level policy: destructive, explicit, never-by-default."""
    argv = list(argv)
    sanctioned = "--operator-sanctioned" in argv
    providers = [a for a in argv if not a.startswith("--")]
    if not providers:
        print("usage: python -m harness.provider_restart --operator-sanctioned <provider> [<provider> ...]")
        print(f"known providers: {', '.join(sorted(PROVIDER_CLUSTERS))}")
        print("DESTRUCTIVE — restarts live provider pods together. No default-all; name explicit providers.")
        print("Requires --operator-sanctioned (restarting Onedata providers is never-without-operator-sanction).")
        return 2
    try:
        result = restart_providers(providers, operator_sanctioned=sanctioned, **restart_kwargs)
    except ProviderRestartError as e:
        print(f"error: {e}")
        return 2
    all_ready = True
    for p in providers:
        info = result[p]
        print(f"{p}: terminated={info['terminated']} ready={info['ready']} elapsed_s={info['elapsed_s']:.1f}")
        all_ready = all_ready and bool(info["ready"])
    return 0 if all_ready else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
