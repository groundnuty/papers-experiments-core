"""C19: the operator's mount-record tool clears a record only on the operator's word and never over a live mount."""

import json

from expcore_onedata.sandbox import mountrecords


def write_record(world, sid: str, mounts: list[dict]) -> None:
    directory = world.s.work_root / "_mounts"
    directory.mkdir(parents=True, exist_ok=True)
    document = {"sid": sid, "lease": "login-data", "mounts": mounts}
    (directory / f"{sid}.json").write_text(json.dumps(document), encoding="utf-8")


def test_c19_a_record_whose_mount_the_table_still_shows_is_not_cleared_even_when_verified(world, capsys):
    """C19: the operator's word is not enough while the mount table shows a mount of the record, by its path or its
    identity; the tool refuses and the record stays. Once the mount is gone, the record goes."""
    path = str(world.s.work_root / "login-data-0123abcd" / "work")
    world.host.mounts[path] = "ext4"
    write_record(
        world, "login-data-0123abcd", [{"path": path, "id": None, "dev": None, "source": None, "fstype": None}]
    )
    record = world.s.work_root / "_mounts" / "login-data-0123abcd.json"
    assert mountrecords.main(["clear", "0123abcd", "--verified"], work_root=world.s.work_root, host=world.host) == 1
    assert record.exists()
    assert "still shows 1 mount" in capsys.readouterr().out
    world.host.mounts.clear()
    assert mountrecords.main(["clear", "0123abcd", "--verified"], work_root=world.s.work_root, host=world.host) == 0
    assert not record.exists()


def test_c19_the_tool_names_records_by_id_and_refuses_an_id_that_names_none(world, capsys):
    """C19, O12: the list names each record by its id and counts, never a path or a lease key; an unknown id is
    refused."""
    write_record(world, "login-data-0123abcd", [])
    assert mountrecords.main(["list"], work_root=world.s.work_root, host=world.host) == 0
    out = capsys.readouterr().out
    assert out == "0123abcd mounts=0 unidentified=0 in_table=0\n"
    assert mountrecords.main(["clear", "ffffffff", "--verified"], work_root=world.s.work_root, host=world.host) == 1


def test_c19_an_unreadable_or_malformed_record_is_never_cleared_even_when_verified(world, capsys):
    """C19: a record the tool cannot read, or that is not a record, says nothing of what is mounted, so the tool never
    clears it, with a live mount in the table or not: it tells the operator to inspect it and fix it by hand."""
    path = str(world.s.work_root / "login-data-0123abcd" / "work")
    world.host.mounts[path] = "ext4"
    directory = world.s.work_root / "_mounts"
    directory.mkdir(parents=True)
    for text in ("{not json", '{"sid": "login-data-0123abcd", "mounts": [{"id": 1}]}'):
        record = directory / "login-data-0123abcd.json"
        record.write_text(text, encoding="utf-8")
        assert mountrecords.main(["clear", "0123abcd", "--verified"], work_root=world.s.work_root, host=world.host) == 1
        assert record.exists()
        assert "inspect it and fix it by hand" in capsys.readouterr().out
