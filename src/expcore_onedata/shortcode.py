# Provenance: new
"""A short code a caller writes into a published record, holding no name the configuration gives (O12).

A reason a caller gives the core (why a tool holds the federation, `tool_admission.shared_admission(reason=...)`; why a
reset reads in full) goes into a record that is published with the study: the record of the tools' holds, a reset's
record. A free text could carry a host, an address, a path or a space's name. So such a reason is a short code: a
lower-case letter, then up to 63 lower-case letters, digits or underscores (`CODE_FORM`), which leaves no room for a
path, a host, an address or a sentence; and it is refused when it holds a name the platform's configuration gives
(`configured_names`: the zone's name and host, each provider's name, id and host, each space's label, name, id and
prefixes), compared lower-cased with every separator dropped, so `alpha_test` holds `alpha.test`. A name only the study
knows (a dataset's, a condition's) is the study's to keep out, as in any field it writes.
"""

import re

from expcore_onedata.config import PlatformConfig

CODE_FORM = re.compile(r"[a-z][a-z0-9_]{0,63}")  # a lower-case letter, then up to 63 letters, digits or underscores


def checked_code(value: object, config: PlatformConfig, *, what: str, says: str) -> str:
    """`value` as it goes into a published record: a short code (`CODE_FORM`) that holds none of the names `config`
    gives (`configured_names`), compared with its underscores dropped. Raise ValueError naming `what` (the argument)
    and what the code `says` otherwise; never echo the value, which may itself hold a name."""
    if not isinstance(value, str) or not CODE_FORM.fullmatch(value):
        raise ValueError(
            f"{what} is a short code that says {says}: a lower-case letter, then up to 63 lower-case letters, digits "
            "or underscores; never empty, a path, a host or a sentence (the record is published, O12)"
        )
    flat = value.replace("_", "")
    if any(name in flat for name in configured_names(config)):
        raise ValueError(f"{what} holds a name the platform's configuration gives; the record is published (O12)")
    return value


def configured_names(config: PlatformConfig) -> frozenset[str]:
    """Every name the configuration gives, lower-cased with all but its letters and digits dropped: the zone's name
    and host, each provider's name, id and host, and each space's label, name, id and prefixes."""
    names = [config.zone.name, _host(config.zone.url)]
    for provider in config.providers.values():
        names += [provider.name, provider.id or "", _host(provider.url)]
    for entry in config.spaces:
        names += [entry.label, entry.space.name, entry.space.id, *entry.space.prefixes]
    return frozenset(flat for flat in (re.sub(r"[^a-z0-9]", "", name.lower()) for name in names) if flat)


def _host(url: str) -> str:
    """The host of an address, without its scheme, port or path."""
    return url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].split(":", 1)[0]


__all__ = ["CODE_FORM", "checked_code", "configured_names"]
