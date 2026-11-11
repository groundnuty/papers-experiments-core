"""What the admission probe's reply reported, read back for the declared fingerprints of a model (B18)."""

import asyncio

from expcore.models.adapters import OpenAICompatAdapter


async def _no_sleep(seconds):
    return None


def test_b18_the_probe_reports_the_served_model_and_fingerprint_its_reply_carried(
    make_spec, limits, completion, fake_client
):
    """B18: the one-token completion's served model id and system fingerprint are in the backend's `info()`, where
    admission reads them for a model that declares `accepted_fingerprints`; its usage is still not counted."""
    client = fake_client(completion(usage=(5, 1), model="test-model", fingerprint="deploy-kestrel"))
    backend = OpenAICompatAdapter(make_spec(), 7, limits, client=client, sleep=_no_sleep)
    asyncio.run(backend.probe())
    info = backend.info()
    assert (info["served_model_ids"], info["system_fingerprints"]) == (["test-model"], ["deploy-kestrel"])
    assert info["usage_seen"]["responses"] == 0
