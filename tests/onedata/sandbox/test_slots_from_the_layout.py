"""O83: the slots a platform template declares, rendered, are slots the sandbox platform leases from, mints for,
journals and revokes.

The platform file's `[slots]` is the template's text filled by the renderer both federations use
(`deploy/compose-federation/fedfacts.py`), not a document typed in a test. The tests that follow are the existing ones
of the sandbox platform (`test_lease_groups`, `test_provider`) run over a platform whose `[slots]` that renderer wrote,
so a slot the renderer writes is one the lease pool, the token journal and the revocation take as they take a
document's.
"""

import importlib.util
import json
import os
import sys
import tomllib
from pathlib import Path

import pytest
from federation_support import DEPLOY
from sandbox_support import SLOT_USERS, add_sandbox, slots_data

from expcore_onedata.config import load_platform
from expcore_onedata.testing import BASELINE, CREDENTIALS_DIR, write_platform
from expcore_onedata.users import GROUP, SlotProbe, TokenJournal, load_slots

MODULES = {}


def sibling(name: str):
    """A test module of this directory, loaded from its file as a module of its own (the tests import by path, so none
    is importable by name): its tests and fixtures are the existing ones, and the `platform_file` they build their
    platform through is the copy's, which a fixture below replaces."""
    spec = importlib.util.spec_from_file_location(f"o83_{name}", Path(__file__).with_name(f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


groups = sibling("test_lease_groups")
provider = sibling("test_provider")


@pytest.fixture(scope="module", autouse=True)
def renderer():
    """The renderer both platforms use, imported by its plain name while this module runs and no longer."""
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(DEPLOY))
        import fedfacts

        MODULES.update(facts=fedfacts)
        yield
    MODULES.clear()


def slot_tables(rights) -> str:
    """The `[slots]` a platform template declares for two slots, `slot-a` and `slot-b`, whose users are those of the
    sandbox tests' document, each user's id the placeholder `@user_id.<name>@` the renderer fills from the facts, the
    tool-server user of each declaring the rights `rights` gives it (the baseline when it gives none)."""
    parts = []
    for slot in ("slot-a", "slot-b"):
        held = json.dumps(list(rights.get(slot, BASELINE)))
        parts.append(f'[slots.{slot}.data_path]\nusername = "{slot}-data"\nuser_id = "@user_id.{slot}-data@"\n')
        tools = f'[slots.{slot}.toolserver]\nusername = "{slot}-tools"\nuser_id = "@user_id.{slot}-tools@"\n'
        parts.append(tools + f"rights = {held}\n")
    return "\n".join(parts)


def rendered_slots(rights) -> dict:
    """The `[slots]` table the renderer writes for that template, as the platform layer reads it: `fedfacts.render`, the
    one both platforms call, over the ids the sandbox tests' users have."""
    ids = {f"user_id.{login}": user_id for login, (_, user_id) in SLOT_USERS.items()}
    return tomllib.loads(MODULES["facts"].render(slot_tables(rights), ids))["slots"]


def platform_from_the_layout(tmp_path, fake, rights):
    """`test_lease_groups.platform_file` with the slots the layout renders in place of the document's."""

    def edit(data):
        add_sandbox(data)
        data["slots"] = rendered_slots(rights)
        conditions = data["toolserver"]["conditions"]
        conditions["write"] = {**conditions["manage"], "rights": groups.WRITE}

    path = write_platform(tmp_path, edit)
    for login, (password, user_id) in SLOT_USERS.items():
        file = tmp_path / CREDENTIALS_DIR / f"{login}.pass"
        file.write_text(password + "\n", encoding="utf-8")
        os.chmod(file, 0o600)
        fake.add_user(login, password, user_id)
        slot, plane = login.rsplit("-", 1)
        fake.grant(user_id, rights.get(slot, BASELINE) if plane == "tools" else BASELINE)
    return path


@pytest.fixture(autouse=True)
def the_slots_the_layout_renders(monkeypatch):
    """The existing tests build their platform through `test_lease_groups.platform_file`: they get rendered slots."""
    monkeypatch.setattr(groups, "platform_file", platform_from_the_layout)


@pytest.fixture
def config(tmp_path, fake):
    """The sandbox tests' platform, its slots the layout's."""
    return load_platform(
        write_platform(tmp_path, lambda data: (add_sandbox(data), data.update(slots=rendered_slots({}))))
    )


@pytest.fixture
def slots(config, fake):
    """The slots of that platform, their users known to the fake federation with view and read, and each user's
    credential file beside the others (as the sandbox tests' own `slots` fixture makes them)."""
    directory = config.file.parent / CREDENTIALS_DIR
    for login, (password, user_id) in SLOT_USERS.items():
        file = directory / f"{login}.pass"
        file.write_text(password + "\n", encoding="utf-8")
        os.chmod(file, 0o600)
        fake.add_user(login, password, user_id)
        fake.grant(user_id, BASELINE)
    return load_slots(config.file, config)


mixed = groups.mixed
stage = provider.stage


def normal(slots):
    """A `[slots]` table with each slot's rights in one order, which the platform layer sorts anyway."""
    return {
        name: {**s, "toolserver": {**s["toolserver"], "rights": sorted(s["toolserver"]["rights"])}}
        for name, s in slots.items()
    }


def test_o83_the_slots_the_renderer_writes_are_the_document_the_sandbox_tests_declare(tmp_path):
    """O83: for the default rights and for a read-only slot beside a writing one, the table is the one the tests'
    document holds: the same slots, users, ids and rights."""
    assert normal(rendered_slots({})) == normal(slots_data())
    mixed_rights = {"slot-a": BASELINE, "slot-b": groups.WRITE}
    assert normal(rendered_slots(mixed_rights)) == normal(slots_data(mixed_rights))


def test_o83_a_slot_the_renderer_wrote_is_leased_minted_journalled_revoked_and_freed(mixed, tmp_path):
    """O83: a lease of the writing group gives the writing slot; its tokens are minted and journalled; the revocation
    after the run is confirmed and the slot frees after the settle: the whole life of a slot, on rendered slots."""
    condition = {"data_path": "via-gate", "tools": "write"}
    lease = mixed.pool.acquire(mixed.platform.lease_group(condition), timeout_s=0)
    assert lease.key == "slot-b"
    assert mixed.slots.users_of(lease).rights == tuple(sorted(groups.WRITE))
    tokens = mixed.minter.mint(lease, ceiling_s=groups.CEILING_S, condition="write")
    assert tokens.tool_server.subject == mixed.slots.users_of(lease).tool_server.user_id
    assert TokenJournal(mixed.pool.dir).latest_expiry("slot-b") is not None
    done = mixed.minter.revoke("slot-b", tokens)
    assert set(done.generations) == {"data_path", "tool_server"}
    assert SlotProbe(TokenJournal(mixed.pool.dir), clock=lambda: done.settle_until + 1).resource_gone("slot-b", {})
    assert mixed.slots.groups[0] == GROUP


# the existing tests, run over the rendered slots (their fixtures `mixed` and `stage` are the ones above)
test_o31_one_campaign = (
    groups.test_o31_one_campaign_runs_a_read_only_and_a_writing_condition_interleaved_each_on_a_slot_of_its_rights
)
test_o31_both_groups_at_once = groups.test_o31_runs_of_two_conditions_hold_a_slot_of_each_group_at_once
test_o31_wait_only_for_own_group = groups.test_o31_a_run_waits_only_for_a_slot_of_its_own_group
test_o31_no_tool_server_takes_any_slot = groups.test_o31_a_run_without_a_tool_server_may_take_any_slot
test_o31_unheld_rights_refused = (
    groups.test_o31_a_condition_whose_rights_no_slot_declares_is_refused_when_the_campaign_is_built
)
test_o31_another_conditions_slot_mints_nothing = groups.test_o31_a_slot_leased_for_another_condition_mints_nothing
test_o31_one_rights_set_leases_every_slot = groups.test_o31_a_campaign_of_one_rights_set_leases_every_slot_as_before
test_c19_lease_groups_and_modes = provider.test_the_levels_of_the_data_path_factor_decide_the_lease_group_and_the_mode
test_c19_one_lease_key_serves_both_planes = provider.test_c19_one_lease_key_per_run_serves_both_planes
test_c19_clean_run_revokes_and_frees = (
    provider.test_c19_a_clean_run_revokes_its_slots_tokens_at_its_end_and_its_key_frees_after_the_settle
)
test_c19_key_not_freed_while_tokens_live = (
    provider.test_c19_a_key_is_not_freed_while_its_sandbox_remains_or_a_token_of_its_slot_lives
)
test_c19_tool_server_run_revokes_after_the_server_stopped = (
    provider.test_c19_a_run_with_a_tool_server_revokes_after_the_server_stopped
)
test_c19_a_mint_that_failed_still_revokes = (
    provider.test_c19_a_mint_that_failed_after_a_token_existed_still_revokes_the_slot
)
