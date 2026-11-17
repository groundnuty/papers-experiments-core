# Provenance: new
"""The working directory a tool server starts in (O84), made once for the launch and the manifest tool alike.

A server that inherits the driver's working directory reads whatever the driver's directory holds: FastMCP reads a
`.env` there for its own settings, and a server's relative paths land in the driver's files. Both the launch of a
condition's server (`expcore_onedata.toolserver`) and the commands that freeze and check a manifest from one
(`expcore.tools.manifest`, `expcore_onedata.toolmanifest`) start it in the directory this module makes, so what a
manifest is frozen from is what a run's server sees.
"""

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def private_workdir() -> Iterator[Path]:
    """The server's working directory (O84): a new, empty directory of this start's own, mode 0700 whatever the
    driver's umask (`mkdtemp`, owner only, then set again), made for each start beside the run's other private files
    and removed, with what the server left in it, when the server has stopped. The server inherits nothing from the
    driver's working directory, a `.env` in it included, and never finds one in the directory it starts in."""
    with tempfile.TemporaryDirectory(prefix="expcore-toolserver-") as directory:
        path = Path(directory)
        path.chmod(0o700)
        yield path
