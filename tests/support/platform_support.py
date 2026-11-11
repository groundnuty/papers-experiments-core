"""Test glue for the platform layer's O12 checks: the values of the default platform document that no message may hold.

The default document, its names, the fakes and the clock are the public testing module, `expcore_onedata.testing`;
what stays here is what only this repository's tests need.
"""

from pathlib import Path

from expcore_onedata.testing import platform_document


def configured_values(tmp_path: Path) -> list[str]:
    """The values of the default document that a message must never hold (O12), as they would be written in one.

    Hosts, URLs, names, ids, users and the paths of the credential directory and the manifests. Left out are the words
    the layer uses for its own roles (the zone's label, the key names, the privilege vocabulary) and the names of the
    conditions, which are ordinary words such as "read" and are looked for quoted, as `{name!r}` would write them.
    """
    document = platform_document(tmp_path)
    values: list[str] = []
    for name, provider in document["providers"].items():
        values += [name, provider["url"], provider["url"].split("//")[1], provider["id"]]
    zone, space = document["zone"], document["space"]
    values += [zone["url"], zone["url"].split("//")[1], space["name"], space["id"], *space["prefixes"]]
    for principal in document["principals"].values():
        values += [principal["username"], principal["user_id"]]
    credentials = document["credentials"]
    values += [credentials["admin"]["env"], str((tmp_path / credentials["users"]["directory"]).resolve())]
    values += [str(tmp_path / credentials["users"]["directory"])]
    server = document["toolserver"]
    values += [server["command"], *(arg for arg in server["args"] if not arg.startswith("-"))]  # a flag is no value
    for name, condition in server["conditions"].items():
        manifest = tmp_path / condition["manifest"]
        values += [str(manifest), str(manifest.resolve()), condition["manifest"], repr(name), f'"{name}"']
    return values


def leaked_values(message: str, tmp_path: Path) -> list[str]:
    """Return the configured values a message holds. The path of platform.toml itself is where the operator pointed
    the layer, not a value in it, so it is taken out first."""
    for config_file in {str(tmp_path / "platform.toml"), str((tmp_path / "platform.toml").resolve())}:
        message = message.replace(config_file, "platform.toml")
    return sorted({value for value in configured_values(tmp_path) if value.lower() in message.lower()})
