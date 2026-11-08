# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/errors.py, blob 862e0cee71a5309cd43f205af2ffed6ba472b9f3
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Infrastructure failures, as opposed to anything the agent or the model did.

`InfraError` is raised by the harness when the model endpoint keeps failing after the transport
retries (5xx, connection, timeout), or when the sandbox or knowledge server crashes for reasons
outside the agent's code. The campaign layer (#16) decides whether to retry the attempt.

Never an InfraError: agent-code exceptions (non-zero exit codes), turn/time/token caps, report
outcomes, or slowness.
"""
from __future__ import annotations


class RequestAbandoned(Exception):
    """A request the endpoint accepted but did not answer before the client's read timeout (which is never
    shorter than the run's time cap). It is never resent: the loop ends the run as time_cap. Not an InfraError
    (slowness is never infra); its usage is unknown and recorded as such."""


class InfraError(RuntimeError):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind          # "endpoint" | "sandbox" | "knowledge" | "unattributed_kill" | "key_rejected" (HTTP 401/403 from the model API)
        self.detail = detail
