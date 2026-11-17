# Provenance: new
"""The command line of a cell and the files the cell sees, built from what the sandbox decided: pure (O56).

A cell is one `apptainer exec`:

    apptainer -q exec --containall --cleanenv --no-home --no-mount hostfs,bind-paths,cwd --pwd /work
        -B <run>/work:/work -B <run>/tmp:/tmp -B <run>/shm:/dev/shm -B ...:ro ... --env NAME=value ... <image.sif>
        timeout -s KILL <limit> <python> -I -B -c <completion bootstrap> <nonce> <size> <sha256> <python>

`--containall` gives the cell its own PID, IPC and mount namespaces, a minimal `/dev` and an environment of the image's
and the `--env` values alone; `--no-home` and `--no-mount hostfs,bind-paths,cwd` keep the driver's home, the host's
file systems and the site's configured bind paths out, so the cell sees the image and the binds below and nothing of
the node's. Its network is the node's: there is no network namespace for an unprivileged user (`facts.py`).

The binds: `/work`, `/tmp` and `/dev/shm` from the run's directory (a cell's own `/tmp` would be gone at its end, where
Docker's container keeps it for the run), `/work` a directory of the run's ext3 image instead with `work_disk = "image"`
(`-B <image>:/work:image-src=/work`, `workdisk.py`, O71); read-only, `/etc/hosts` (the federation's names on loopback,
where the gate listens), `/etc/resolv.conf` naming no name server and `/etc/nsswitch.conf` looking hosts up in files
alone (so no name is looked up by DNS), `/etc/expcore/gate.json` (the gate's address, port and names, for a connect hook
to read where a cleared environment cannot drop it), the run's CA and trust bundles, the egress collector's socket
directory, the egress shim, the libraries of hook (b), `/etc/ld.so.preload` naming them all, the read-only inputs and
the files of hook (b).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

NO_MOUNT = "hostfs,bind-paths,cwd"
RUN_TAG = "EXPCORE_SANDBOX"  # in every cell's environment: the run's sandbox id, how the watch finds its processes
GATE_PORT_ENV = "EXPCORE_GATE_PORT"  # a REST cell's: the port of the gate on 127.0.0.1
GATE_INFO = "/etc/expcore/gate.json"  # a REST cell's, read-only: {"address", "port", "names"} (hook b)
EGRESS_DIR = "/hpcop-egress"  # the collector's socket directory, as egresslog.c has it compiled in
SHIM_TARGET = "/hpcop-shim/egresslog.so"
PRELOAD_DIR = "/expcore-preload"  # hook (b)'s libraries, each `<n>.so`
LOOPBACK = "127.0.0.1"
FATAL = "FATAL:"  # how apptainer itself says it could not run a container, on its stderr
APPTAINER_FAILED_EXIT = 255
RESOLV_CONF, NSSWITCH_CONF = "/etc/resolv.conf", "/etc/nsswitch.conf"
# No name server (facts.py, dns_none). A resolv.conf without one alone would not do: the C library then asks the name
# server on 127.0.0.1, the node's loopback that every user shares; so the hosts database is the files alone.
RESOLV_TEXT = "# no name server: a cell resolves the names of /etc/hosts alone\noptions attempts:1 timeout:1\n"
NSSWITCH_TEXT = "".join(
    f"{db}: files\n" for db in ("passwd", "group", "shadow", "hosts", "networks", "protocols", "services", "ethers")
)


@dataclass(frozen=True)
class Bind:
    """A path of the node bound into the cell at `target`, read-only unless `writable`; with `image_src`, the source is
    an ext3 image and that directory of it is bound, read/write (`workdisk.py`, O71)."""

    source: Path
    target: str
    writable: bool = False
    image_src: str | None = None

    def arg(self) -> str:
        if self.image_src is not None:
            return f"{self.source}:{self.target}:image-src={self.image_src}"
        return f"{self.source}:{self.target}" + ("" if self.writable else ":ro")


def exec_argv(
    words: Sequence[str],
    image: Path,
    binds: Iterable[Bind],
    env: Mapping[str, str],
    command: Sequence[str],
    *,
    workdir: str = "/work",
) -> list[str]:
    """`apptainer exec` of `command` in `image` with these binds and this environment (the module's docstring)."""
    argv = [*words, "-q", "exec", "--containall", "--cleanenv", "--no-home", "--no-mount", NO_MOUNT]
    argv += ["--pwd", workdir]
    for bind in binds:
        argv += ["-B", bind.arg()]
    for name, value in env.items():
        argv += ["--env", f"{name}={value}"]
    return [*argv, str(image), *command]


def cell_command(limit_s: float, bootstrap: Sequence[str]) -> list[str]:
    """What runs in the cell: `timeout` as the backstop of the harness's own timer, then the completion bootstrap."""
    return ["timeout", "-s", "KILL", f"{limit_s:.3f}", *bootstrap]


def hosts_text(names: Sequence[str]) -> str:
    """The cell's `/etc/hosts`: localhost, and each of the federation's names on the loopback where the gate listens."""
    lines = [f"{LOOPBACK} localhost", "::1 localhost"]
    if names:
        lines.append(f"{LOOPBACK} {' '.join(names)}")
    return "\n".join(lines) + "\n"


def preload_text(targets: Sequence[str]) -> str:
    """The cell's `/etc/ld.so.preload`: one library per line, in the order the loader is to take them."""
    return "".join(f"{target}\n" for target in targets)


def gate_info(port: int, names: Sequence[str]) -> str:
    """`/etc/expcore/gate.json`: where the gate is and which names it serves (hook b's connect hook reads it)."""
    return json.dumps({"address": LOOPBACK, "port": port, "names": list(names)}, sort_keys=True) + "\n"


def apptainer_failed(returncode: int, stderr: str) -> bool:
    """`apptainer` could not run the container, as it says itself: its exit 255 with a `FATAL:` line. An exit code
    alone never is: a cell may exit 255 by itself."""
    return returncode == APPTAINER_FAILED_EXIT and any(line.startswith(FATAL) for line in stderr.splitlines())


def bindable(path: Path) -> bool:
    """Whether a node path can be named in a `-B` value: Apptainer splits it at ':' and ','."""
    text = str(path)
    return text.startswith("/") and not any(c in text for c in ":,") and not any(c.isspace() for c in text)


__all__ = [
    "GATE_INFO",
    "GATE_PORT_ENV",
    "NSSWITCH_CONF",
    "NSSWITCH_TEXT",
    "RESOLV_CONF",
    "RESOLV_TEXT",
    "RUN_TAG",
    "Bind",
    "apptainer_failed",
    "bindable",
    "cell_command",
    "exec_argv",
    "gate_info",
    "hosts_text",
    "preload_text",
]
