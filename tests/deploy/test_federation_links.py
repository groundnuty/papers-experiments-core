"""#221: the link check at the end of up. A layout's probe space is one no agent may reach and that every provider of a
space the agent may reach supports, refused at load otherwise; up runs the platform layer's check last, through the
platform.toml it rendered, and is not ready until every pair transfers."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from federation_support import MINIMAL

from expcore_onedata.linkcheck import LinkReport, PairResult
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import with_link_check

PROBED = (
    MINIMAL
    + """
[spaces.link-probes]
owner = "oak"
mirrors = ["elm"]
agent_visible = false
baseline = false
"""
).replace('study_space = "alpha-space"', 'study_space = "alpha-space"\nprobe_space = "link-probes"')


def write(tmp_path: Path, text: str, *, template: bool = True) -> Path:
    if template:
        (tmp_path / "platform.toml.template").write_text("# a template\n", encoding="utf-8")
        text = text.replace(
            'study_space = "alpha-space"', 'study_space = "alpha-space"\nplatform_template = "platform.toml.template"'
        )
    path = tmp_path / "federation.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_layouts_probe_space_is_loaded(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, PROBED))
    assert spec.probe_space == "link-probes"


@pytest.mark.parametrize(
    ("change", "template", "words"),
    [
        (("agent_visible = false\n", ""), True, "spaces.link-probes.agent_visible: must be false for the probe space"),
        (('mirrors = ["elm"]\nagent_visible', "agent_visible"), True, "is not supported by elm, which supports alpha"),
        (("", ""), False, "probe_space: needs a platform_template"),
        (('probe_space = "link-probes"', 'probe_space = "no-such-space"'), True, "probe_space: names a space"),
    ],
    ids=["agent visible", "a provider left out", "no template", "unknown space"],
)
def test_a_probe_space_an_agent_could_see_or_that_cannot_test_a_pair_is_refused_at_load(
    fed, tmp_path, change, template, words
):
    text = PROBED.replace(change[0], change[1], 1) if change[0] else PROBED
    with pytest.raises(fed.spec.LayoutError) as refused:
        fed.spec.load_spec(write(tmp_path, text, template=template))
    assert words in str(refused.value)


def up_stubbed(fed, monkeypatch, tmp_path, check):
    """Up over a stand-in for everything it touches, with `check` as the link check; return the layout and the calls."""
    layout = fed.layout.Layout(tmp_path / "fed", spec=fed.spec.load_spec())
    calls: list[str] = []
    monkeypatch.setattr(fed.cli, "require_images", lambda: None)
    monkeypatch.setattr(fed.cli, "project_containers", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_networks", lambda project: [])
    for name in ("require_own", "require_ports_free", "require_memory_fits", "prepare", "bring_up"):
        monkeypatch.setattr(fed.cli, name, lambda *arguments, name=name: calls.append(name))
    monkeypatch.setattr(fed.cli, "Rest", lambda ca: SimpleNamespace(close=lambda: calls.append("closed")))
    boot = SimpleNamespace(ids=SimpleNamespace(spaces={"link-probes": "sid-probes"}))
    monkeypatch.setattr(fed.cli, "Bootstrap", lambda rest, layout: boot)
    monkeypatch.setattr(
        fed.cli, "Content", lambda boot: SimpleNamespace(reset=lambda timeout: calls.append("baseline"))
    )
    monkeypatch.setattr(fed.cli, "write_facts", lambda layout, boot: calls.append("facts") or "facts written")

    def links(layout, space_ids):
        calls.append("links")
        assert space_ids == {"link-probes": "sid-probes"}
        return check()

    monkeypatch.setattr(fed.cli, "check_after_up", links)
    return layout, calls


def test_up_runs_the_link_check_last_and_reports_it(fed, monkeypatch, tmp_path):
    layout, calls = up_stubbed(fed, monkeypatch, tmp_path, lambda: "link check: every pair transferred")
    out = fed.cli.up(layout)
    assert calls[-2:] == ["links", "closed"]
    assert calls.index("facts") < calls.index("links")
    assert out.endswith("facts written\nlink check: every pair transferred")


def test_up_fails_when_a_pair_does_not_transfer(fed, monkeypatch, tmp_path, capsys):
    def stuck():
        raise RuntimeError("the link check failed: no transfer from providers[1] to providers[0] within 30 s")

    layout, calls = up_stubbed(fed, monkeypatch, tmp_path, stuck)
    with pytest.raises(RuntimeError, match=r"providers\[1\] to providers\[0\]"):
        fed.cli.up(layout)
    assert calls[-1] == "closed"  # the REST client is closed all the same


# -- fedlinks.check_after_up ----------------------------------------------------------------------------------------


@pytest.fixture
def fedlinks(fed):
    import fedlinks

    return fedlinks


def layout_with(fed, tmp_path, monkeypatch, edit, *, probe=True):
    """A layout whose rendered platform.toml is the platform layer's test document, edited."""
    monkeypatch.setenv(ADMIN_ENV, "a-test-admin-credential")
    write_platform(tmp_path, edit)
    spec = fed.spec.load_spec(write(tmp_path, PROBED if probe else MINIMAL))
    layout = fed.layout.Layout(tmp_path, spec=spec)
    assert layout.platform == tmp_path / "platform.toml"
    return layout


def two_providers(data, *, link_check=True):
    data["support"] = {"providers": ["alpha", "beta"], "local": "alpha"}
    if link_check:
        with_link_check(data)


def test_without_a_template_up_checks_nothing_and_says_why(fed, fedlinks, tmp_path):
    layout = fed.layout.Layout(tmp_path, spec=fed.spec.load_spec(write(tmp_path, MINIMAL, template=False)))
    assert "no platform_template" in fedlinks.check_after_up(layout, {})


def test_pairs_and_no_link_check_refuse_up(fed, fedlinks, tmp_path, monkeypatch):
    layout = layout_with(fed, tmp_path, monkeypatch, lambda d: two_providers(d, link_check=False), probe=False)
    with pytest.raises(RuntimeError, match=r"1 provider pair\(s\) and no \[link_check\]"):
        fedlinks.check_after_up(layout, {})


def test_up_runs_the_layers_check_and_writes_its_report_beside_the_facts(fed, fedlinks, tmp_path, monkeypatch):
    layout = layout_with(fed, tmp_path, monkeypatch, two_providers)
    passed = PairResult("providers[0]", "providers[1]", True, 0.25, 65536)
    monkeypatch.setattr(fedlinks, "check_links", lambda config, client: LinkReport(True, "ok", (passed,), 1.0, 2.0))
    out = fedlinks.check_after_up(layout, {"link-probes": "sp-probe-0009"})
    assert "providers[1] -> providers[0] 0.25 s 65536 B" in out
    record = json.loads((tmp_path / fedlinks.REPORT).read_text(encoding="utf-8"))
    assert record["pairs"][0]["seconds"] == 0.25


def test_a_stuck_pair_fails_up_naming_it(fed, fedlinks, tmp_path, monkeypatch):
    layout = layout_with(fed, tmp_path, monkeypatch, two_providers)
    stuck = PairResult("providers[0]", "providers[1]", False, 30.0, reason="no transfer from providers[1]")
    report = LinkReport(False, "a provider link is stuck: no transfer from providers[1]", (stuck,), 1.0, 2.0)
    monkeypatch.setattr(fedlinks, "check_links", lambda config, client: report)
    with pytest.raises(RuntimeError, match=r"not ready: a provider link is stuck"):
        fedlinks.check_after_up(layout, {"link-probes": "sp-probe-0009"})
    assert json.loads((tmp_path / fedlinks.REPORT).read_text(encoding="utf-8"))["ok"] is False


def test_a_link_check_of_another_space_than_the_probe_space_refuses_up(fed, fedlinks, tmp_path, monkeypatch):
    layout = layout_with(fed, tmp_path, monkeypatch, two_providers)
    with pytest.raises(RuntimeError, match="does not name the layout's probe_space"):
        fedlinks.check_after_up(layout, {"link-probes": "sid-another"})
