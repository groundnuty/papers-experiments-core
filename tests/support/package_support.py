"""Helpers for tests that build replication packages: stand-in wheels, and fake installed distributions to pack.

Packing the real environment means packing the numerical libraries again on every build, and the core is installed
editable here, so the tests that build whole packages vendor small stand-ins instead, one per distribution of the
environment. The vendoring itself is tested on fake installed distributions (`installed_distribution`) and, once,
by the fresh-repository check, which packs a real environment and installs it offline.
"""

import base64
import hashlib
import io
import zipfile
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path

from expcore.package.wheels import Wheel
from expcore.provenance.environment import CORE_DISTRIBUTION


def stand_in(name: str, version: str) -> tuple[str, bytes]:
    """A tiny wheel that names the distribution and version, and holds nothing else."""
    escaped = name.replace("-", "_")
    info = f"{escaped}-{version}.dist-info"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for entry, text in (
            (f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"),
            (f"{info}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
        ):
            archive.writestr(zipfile.ZipInfo(entry, date_time=(1980, 1, 1, 0, 0, 0)), text)
    return f"{escaped}-{version}-py3-none-any.whl", buffer.getvalue()


def stand_in_vendor(distributions: Mapping[str, str], supplied: Path | None = None) -> list[Wheel]:
    """Vendor a stand-in per distribution: the core's is read by the scans, as a direct-URL install's is."""
    return [
        Wheel(name, version, *stand_in(name, version), scanned=name == CORE_DISTRIBUTION)
        for name, version in sorted(distributions.items())
    ]


def installed_distribution(
    site: Path, name: str, version: str, files: Mapping[str, bytes], *, direct_url: str | None = None
) -> metadata.Distribution:
    """Install a fake pure distribution into `site` the way an installer does: its files, a dist-info with RECORD."""
    escaped = name.replace("-", "_")
    info = f"{escaped}-{version}.dist-info"
    written = dict(files)
    written[f"{info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    written[f"{info}/WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    written[f"{info}/INSTALLER"] = b"uv\n"
    written[f"{info}/uv_cache.json"] = b'{"timestamp": 1}'
    if direct_url is not None:
        written[f"{info}/direct_url.json"] = direct_url.encode()
    lines = []
    for rel, data in written.items():
        target = site / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        lines.append(f"{rel},sha256={digest},{len(data)}")
    lines += [f"{info}/RECORD,,", f"../../../bin/{escaped}-tool,,"]
    (site / info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metadata.Distribution.at(site / info)
