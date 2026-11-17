"""What a platform file gets for a layout's slots and tool server (O83), the same on Compose and on HPC.

`additions` is the one place both platforms' renderers (`fedfacts.write_facts`, `hpcboot.write_facts`) go through: it
installs each condition's frozen manifest into the root, and returns the `[slots]` and `[toolserver]` tables of the
platform file, written from the layout's declarations and the ids Onezone gave the users, so the rights in the file are
the privileges the bootstrap gave the users, from the one declaration. A layout that declares neither gets nothing, and
its platform file is what it always was.

The study's own files (the interpreter, the clone, the frozen manifests) are named in the layout under
`@study_artifacts@`; each platform says where that is. A manifest is copied into the root (`toolserver/manifests/`),
beside platform.toml, and the file names the copy: a platform file copied beside a study reads the root's own.

`check_artifacts` is the refusal before anything is brought up, and `check_rendered` the one after the file is written:
the platform layer loads it as a campaign would, its slots as the lease pool would, every condition must have slots to
lease from, and a slot's users must be members of exactly the spaces its planes reach. A message names a condition or a
slot by its position and never a path.
"""

import json
import os
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fedlayout import Layout
from fedpki import private_directory
from fedslots import STUDY_ARTIFACTS, Condition, ToolServer
from fedspec import Spec

ENVIRONMENT = "EXPCORE_FED_STUDY_ARTIFACTS"  # where a Compose federation finds the study's artifacts (HPC: site.env)


def study_artifacts_from(environ: Mapping[str, str]) -> str | None:
    """The study's artifacts directory a Compose deployment is given, or None: an absolute path of plain characters with
    no `..`. The refusal names the variable and never says what it holds."""
    value = environ.get(ENVIRONMENT)
    if value is None or value == "":
        return None
    if (
        not os.path.isabs(value)
        or ".." in Path(value).parts
        or not value.isprintable()
        or '"' in value
        or "\\" in value
    ):
        raise ValueError(f"{ENVIRONMENT} must be an absolute path of plain characters with no '..' step")
    return value


def expand(declared: str, study_artifacts: str | None) -> str:
    """A declared path with `@study_artifacts@` replaced by the directory; refused when none was given."""
    if not study_artifacts:
        raise RuntimeError(f"the layout's tool server names {STUDY_ARTIFACTS} and no directory was given for it")
    return study_artifacts.rstrip("/") + declared[len(STUDY_ARTIFACTS) :]


def _position(tool: ToolServer, condition: Condition) -> str:
    """A condition named by its place in the layout, never by its name."""
    return f"conditions[{tool.conditions.index(condition)}]"


def read_manifests(spec: Spec, study_artifacts: str | None) -> dict[str, bytes]:
    """Each condition's frozen manifest, read from the study's artifacts: its bytes, by condition name. Refused, naming
    the condition by its position, when the file cannot be read, is not a manifest or was edited, or shows other tools
    than the condition's allow-list (the server shows exactly those, and the client fails the start on any other)."""
    from expcore.tools.manifest import FrozenManifest, ManifestError

    tool, found = spec.toolserver, {}
    if tool is None:
        return found
    for condition in tool.conditions:
        where = _position(tool, condition)
        path = Path(expand(condition.manifest, study_artifacts))
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"{where}: its frozen manifest cannot be read ({type(error).__name__})") from None
        try:
            frozen = FrozenManifest.load(path)
        except ManifestError:
            raise RuntimeError(f"{where}: its frozen manifest is not one, or its hash is not its tools'") from None
        shown = {t["name"] for t in frozen.tools}
        if shown != set(condition.allow):
            extra, missing = len(shown - set(condition.allow)), len(set(condition.allow) - shown)
            raise RuntimeError(
                f"{where}: its frozen manifest shows {extra} tool(s) its allow-list lacks and lacks {missing} that "
                "the allow-list names: freeze it again from the pinned server with the condition's allow-list"
            )
        found[condition.name] = raw
    return found


def check_artifacts(
    spec: Spec, study_artifacts: str | None, *, verify: Callable[[Path, str], str] | None = None
) -> None:
    """Refuse a federation whose tool server cannot run from the study's artifacts, before anything is brought up: the
    interpreter is a file one can run, the clone is a directory at the pinned commit with no change to a tracked file
    (the check the launcher makes at every start, `expcore_onedata.pin`), and each frozen manifest is as above."""
    tool = spec.toolserver
    if tool is None:
        return
    interpreter = Path(expand(tool.command, study_artifacts))
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise RuntimeError("the tool server's interpreter is not an executable file in the study's artifacts")
    clone = Path(expand(tool.clone, study_artifacts))
    if not clone.is_dir():
        raise RuntimeError("the tool server's clone is not a directory in the study's artifacts")
    if verify is None:
        from expcore_onedata.pin import verify_pin as verify
    from expcore_onedata.pin import PinError

    try:
        verify(clone, tool.commit)
    except PinError as error:
        raise RuntimeError(str(error)) from None
    read_manifests(spec, study_artifacts)


def install_manifests(layout: Layout, study_artifacts: str | None) -> dict[str, Path]:
    """Copy each condition's frozen manifest, byte for byte, into the root's `toolserver/manifests/<condition>.json`
    (0600, in 0700 directories, made new beside a copy an earlier up left); return each copy's path by condition."""
    manifests = read_manifests(layout.spec, study_artifacts)
    if not manifests:
        return {}
    directory = private_directory(private_directory(layout.toolserver) / "manifests")
    placed = {}
    for name, raw in manifests.items():
        target = directory / f"{name}.json"
        scratch = directory / f".{name}.{os.getpid()}.part"
        descriptor = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            os.replace(scratch, target)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
        placed[name] = target
    return placed


def _string(value: str) -> str:
    return json.dumps(value)  # a JSON string is a TOML basic string


def tables(spec: Spec, facts: Mapping[str, Any], study_artifacts: str | None, manifests: Mapping[str, str]) -> str:
    """The `[slots]` and `[toolserver]` tables of the layout, as platform.toml holds them: every user's id from the
    facts, each slot's tool-server rights its principal's privileges, the tool server's paths under the study's
    artifacts and each condition's manifest at `manifests[name]` (the path the platform file is to name)."""
    lines = ["# the layout's slots and tool server (fedtools.py): the same text on every platform"]
    ids = {name: principal["user_id"] for name, principal in facts["principals"].items()}
    for slot in spec.slots:
        lines += [f"[slots.{slot.name}.data_path]", f"username = {_string(slot.data_path)}"]
        lines += [f"user_id = {_string(ids[slot.data_path])}", f"[slots.{slot.name}.toolserver]"]
        lines += [f"username = {_string(slot.toolserver)}", f"user_id = {_string(ids[slot.toolserver])}"]
        rights = sorted(spec.principal(slot.toolserver).privileges)
        lines += [f"rights = {json.dumps(rights)}"]
    tool = spec.toolserver
    if tool is not None:
        lines += ["", "[toolserver]", f"command = {_string(expand(tool.command, study_artifacts))}"]
        lines += [f"args = {json.dumps(list(tool.args))}", f"start_timeout_s = {tool.start_timeout_s:g}"]
        lines += [f"clone = {_string(expand(tool.clone, study_artifacts))}", f"commit = {_string(tool.commit)}"]
        for condition in tool.conditions:
            lines += ["", f"[toolserver.conditions.{condition.name}]", f"allow = {json.dumps(list(condition.allow))}"]
            lines += [f"rights = {json.dumps(list(condition.rights))}"]
            lines += [f"manifest = {_string(manifests[condition.name])}"]
    return "\n".join(lines) + "\n"


def additions(
    layout: Layout, facts: Mapping[str, Any], study_artifacts: str | None, *, absolute_root: str | None = None
) -> str:
    """The text a platform file gets for the layout's slots and tool server, with the frozen manifests installed in the
    root: nothing for a layout that declares neither. The platform file names each manifest relative to itself, or, with
    `absolute_root`, below that absolute root (the HPC file, which is copied beside a study)."""
    spec = layout.spec
    if not spec.slots:
        return ""
    placed = install_manifests(layout, study_artifacts)
    base = absolute_root.rstrip("/") + "/" if absolute_root else ""
    named = {name: f"{base}toolserver/manifests/{name}.json" for name in placed}
    return tables(spec, facts, study_artifacts, named)


def attach(text: str, extra: str) -> str:
    """The platform file's text with the layout's tables after it; refused when the template declares `[slots]` or
    `[toolserver]` itself, since each would then be declared twice."""
    if not extra:
        return text
    declared = {"slots", "toolserver"} & set(tomllib.loads(text))
    if declared:
        raise RuntimeError(
            "the platform template declares " + " and ".join(f"[{name}]" for name in sorted(declared)) + ", and the "
            "layout does: declare them in the layout alone, so they are the same on every platform"
        )
    return text.rstrip("\n") + "\n\n" + extra


def check_rendered(layout: Layout, *, environ: Mapping[str, str] | None = None) -> None:
    """Load the written platform file as a campaign would, and refuse what a campaign would refuse, now: the platform
    layer's own load (a tool server the launcher cannot start, a manifest that is no file), its slots as the lease pool
    reads them (each user's credential a private file), a slot group for every condition's rights, and every slot user
    a member of exactly the spaces its plane reaches. Nothing without slots."""
    spec = layout.spec
    if not spec.slots:
        return
    from expcore.config.loading import ConfigError
    from expcore_onedata.config import load_platform
    from expcore_onedata.users import load_slots

    try:
        config = load_platform(layout.platform, environ=environ)
        slots = load_slots(layout.platform, config, environ=environ)
    except ConfigError as error:
        raise RuntimeError(f"the rendered platform file is refused: {error}") from None
    if spec.toolserver is not None:
        for condition in spec.toolserver.conditions:
            try:
                slots.group_for(condition.rights)
            except ValueError:
                raise RuntimeError(
                    f"{_position(spec.toolserver, condition)}: no slot's tool-server user holds its rights"
                ) from None
    for plane, tokens in (("data_path", "data_path"), ("toolserver", "tool_server")):
        reached = {entry.space.name for entry in config.spaces if tokens in entry.tokens}
        for index, slot in enumerate(spec.slots):
            if set(spec.principal(getattr(slot, plane)).spaces) != reached:
                raise RuntimeError(
                    f"slots[{index}]: its {plane} user is not a member of exactly the spaces the platform file's "
                    f"{tokens} plane reaches, so its token would be refused"
                )
