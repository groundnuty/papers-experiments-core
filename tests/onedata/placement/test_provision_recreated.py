"""A name made again while its delete is under way, read through the core's own resolution (`Provisioner.resolve`).

The fake's pending delete (`delete_delay_s`) must show a provider that still lists the old entry only that entry, as
the 12 Nov live check saw: the core's listing keeps one entry per name, the last one listed, so a mirror that listed
both would hand the new entry to a study that read through it without waiting.
"""

from expcore_onedata.testing import ROOT_ID

DELAY_S = 5.0


def test_a_resolution_through_a_mirror_finds_the_old_entry_until_the_delete_lands_and_the_new_one_after(
    fake, provisioner, ticker
):
    fake.delete_delay_s = DELAY_S
    fake.add_file("study/f0.bin", b"OLD")
    old, old_file = fake.id_of("study"), fake.id_of("study/f0.bin")
    provisioner.mutate("DELETE", f"/data/{old}", provider="alpha")
    fresh = provisioner.mutate(
        "POST", f"/data/{ROOT_ID}/children", provider="alpha", params={"name": "study", "type": "DIR"}
    )["fileId"]
    new_file = provisioner.mutate(
        "POST", f"/data/{fresh}/children", provider="alpha", params={"name": "f0.bin", "type": "REG"}
    )["fileId"]
    assert fake.id_of("study/f0.bin") == new_file
    assert provisioner.resolve("study", provider="alpha").file_id == fresh  # the deleting provider: the new one
    assert provisioner.resolve("study/f0.bin", provider="alpha").file_id == new_file
    for _ in range(int(DELAY_S)):
        assert provisioner.resolve("study", provider="beta").file_id == old  # the lagging one: the old, and only it
        assert provisioner.resolve("study/f0.bin", provider="beta").file_id == old_file
        ticker.sleep(1)
    assert provisioner.resolve("study", provider="beta").file_id == fresh
    assert provisioner.resolve("study/f0.bin", provider="beta").file_id == new_file
