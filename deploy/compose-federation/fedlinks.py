"""The link check at the end of up (#221): every (local, source) pair transfers before the federation is reported ready.

A provider's rtransfer link to a peer can come up stuck, with nothing logged, and then every read through it hangs.
Up runs the platform layer's own check (`expcore_onedata.linkcheck.check_links`) through the platform.toml it has just
rendered, so the pairs, the probe space and the bounds are the ones a campaign's health gate will use, and it fails,
naming the pair and the remedy, unless every pair passes. The report is written beside facts.json as
`link-check.json`: each pair by position, its seconds and bytes, and the transfers the check made.

A layout with no platform template renders no platform.toml, so no local provider is declared and nothing is checked;
up says so. One whose platform.toml declares provider pairs and no `[link_check]` is refused: up does not report a
federation ready with its links unchecked, and a campaign's health gate would refuse it anyway.
"""

import json

from fedlayout import Layout

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.linkcheck import check_links
from expcore_onedata.linksettings import link_pairs

REPORT = "link-check.json"


def check_after_up(layout: Layout, space_ids: dict[str, str]) -> str:
    """Run the link check through the rendered platform.toml; return what up prints, or raise RuntimeError naming the
    pair that does not transfer. `space_ids` are the spaces' ids by name, as up made them."""
    spec = layout.spec
    if spec.platform_template is None:
        return "link check: none, since the layout names no platform_template and so no local provider"
    try:
        config = load_platform(layout.platform)
    except ConfigError as error:
        raise RuntimeError(f"the rendered platform.toml cannot be loaded for the link check: {error}") from None
    pairs = link_pairs(config.spaces, list(config.providers))
    if not pairs:
        return "link check: no provider pair, every space is supported by the local provider alone"
    if config.link_check is None:
        raise RuntimeError(
            f"platform.toml declares {len(pairs)} provider pair(s) and no [link_check]: declare probe_space in the "
            "layout and [link_check] in its platform template; up does not report ready with links unchecked"
        )
    if spec.probe_space is None or config.link_check.space.id != space_ids.get(spec.probe_space):
        raise RuntimeError("the [link_check] of platform.toml does not name the layout's probe_space")
    client = OnezoneClient(config)
    try:
        report = check_links(config, client)
    finally:
        client.close()
    (layout.root / REPORT).write_text(json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not report.ok:
        raise RuntimeError(f"the link check failed, so the federation is not ready: {report.detail}")
    rows = (f"{p.source} -> {p.local} {p.seconds:.2f} s {p.bytes} B" for p in report.pairs)
    return f"link check: every pair transferred ({'; '.join(rows)}); {REPORT} written"
