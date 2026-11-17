"""Slots and a tool server in a platform template (O83): which principals are slot users, the study's artifacts the
template names, and the rendered file's slots checked against the layout's principals. The same on Compose and on HPC.

A platform template declares `[slots.<name>]` (a `data_path` and a `toolserver` user, each a `username` and a `user_id`
filled as `@user_id.<username>@`, and the tool-server user's `rights`) and `[toolserver]`, with its paths under
`@study_artifacts@`. The users are ordinary principals of the layout, so the bootstrap that provisions principals
provisions them (`fedboot.Bootstrap`), on both platforms alike; this module is what ties the template to them:

- `slot_users` reads, from the template as it stands (its other placeholders replaced by stand-ins), which
  principals are slot users, since provisioning must treat an existing one as read-only before any file is rendered;
- `check_artifacts` is the refusal before anything is brought up: the interpreter and the arguments that name the
  study's files exist, the clone is at the pinned commit, and each frozen manifest is the condition's;
- `check_rendered` loads the written file as a campaign does (its slots as the lease pool reads them) and refuses a slot
  that disagrees with the layout it was provisioned from: a user that is no principal, a data-path user with more than
  the baseline, a tool-server user whose `rights` are not exactly its principal's privileges, an id that is not the
  facts', a user of other spaces than its plane's. So the rights the file holds are the privileges the bootstrap gave.

A message names a condition or a slot by its position, never a path.
"""

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fedlayout import Layout
from fedspec import Spec

ENVIRONMENT = "EXPCORE_FED_STUDY_ARTIFACTS"  # where a Compose federation finds the study's artifacts (HPC: site.env)
STUDY_ARTIFACTS = "@study_artifacts@"
ANY_PLACEHOLDER = re.compile(r"@[A-Za-z0-9_.-]+@")
SLOTS_WORD = re.compile(r"^[^#\n]*slots", re.M)  # `slots` anywhere in the code of a line, whatever its quoting


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


def stand_in(template: str, study_artifacts: str | None) -> dict[str, Any] | None:
    """The template as a document, for what it declares in plain text: `@study_artifacts@` as the directory (itself
    when none is given) and every other placeholder as `0`, which a TOML value of these templates takes as it is (an
    integer where the placeholder stands bare, a character in a string where it stands in one). None when the result is
    no TOML (a placeholder in a key, say)."""
    kept = study_artifacts if study_artifacts else STUDY_ARTIFACTS
    text = ANY_PLACEHOLDER.sub(lambda found: kept if found.group(0) == STUDY_ARTIFACTS else "0", template)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def require_study_artifacts(template: str, study_artifacts: str | None) -> None:
    """Refuse a template that names `@study_artifacts@` when no directory was given for it, naming both platforms'
    settings: the renderer would refuse a placeholder nothing filled, without saying where this one comes from."""
    if STUDY_ARTIFACTS in template and not study_artifacts:
        raise RuntimeError(
            f"the platform template names {STUDY_ARTIFACTS} and no directory was given for it "
            f"({ENVIRONMENT} for Compose, HPC_STUDY_ARTIFACTS in site.env for HPC)"
        )


def _template(spec: Spec) -> str:
    return spec.platform_template.read_text(encoding="utf-8") if spec.platform_template is not None else ""


def slot_users(spec: Spec) -> dict[str, str]:
    """The slot users the layout's platform template declares, by principal name, each with its plane (`data_path` or
    `toolserver`); none for a template without `[slots]`. A template that declares `[slots]` and cannot be read as it
    stands is refused: provisioning would otherwise not know which users it must never change."""
    text = _template(spec)
    document = stand_in(text, None)
    if document is None:  # no TOML as it stands: one that may declare slots (in any spelling TOML has) is refused
        if SLOTS_WORD.search(text):
            raise RuntimeError(
                "the platform template may declare [slots] and is no TOML before it is rendered: a placeholder may "
                "stand in a value, never in a key or a table's name"
            )
        return {}
    found = {}
    for slot in (document.get("slots") or {}).values():
        for plane in ("data_path", "toolserver"):
            user = (slot.get(plane) or {}).get("username") if isinstance(slot, dict) else None
            if isinstance(user, str):
                found[user] = plane
    return found


def check_artifacts(
    spec: Spec,
    study_artifacts: str | None,
    root: Path | None = None,
    *,
    verify: Callable[[Path, str], str] | None = None,
) -> None:
    """Refuse a federation whose tool server cannot run from the study's artifacts, before anything is brought up: the
    command, when a path, is an executable file; each argument that names a file of the study exists; the clone is a
    directory at the pinned commit with no change to a tracked file (the check the launcher makes at every start,
    `expcore_onedata.pin`); each condition's frozen manifest is one, unedited, showing exactly its allow-list (the
    server shows exactly those, and the client fails the start on any other). The platform layer reads a relative
    clone or manifest from the platform file's directory, which is the federation's `root`, so they are read from
    there here. Nothing for a template without `[toolserver]`."""
    from expcore.tools.manifest import FrozenManifest, ManifestError
    from expcore_onedata.pin import PinError, verify_pin

    text = _template(spec)
    require_study_artifacts(text, study_artifacts)
    tool = (stand_in(text, study_artifacts) or {}).get("toolserver")
    if not isinstance(tool, dict):
        return
    command = tool.get("command")
    if (
        isinstance(command, str)
        and os.path.isabs(command)
        and not (os.path.isfile(command) and os.access(command, os.X_OK))
    ):
        raise RuntimeError("the tool server's command is not an executable file")
    here = root if root is not None else Path()  # where the rendered platform.toml lies
    base = (study_artifacts or "").rstrip("/") + "/"
    for argument in tool.get("args") or []:
        if study_artifacts and isinstance(argument, str) and argument.startswith(base) and not Path(argument).exists():
            raise RuntimeError("an argument of the tool server names a file of the study's artifacts that is missing")
    clone, commit = tool.get("clone"), tool.get("commit")
    if isinstance(clone, str) and isinstance(commit, str):
        if not (here / clone).is_dir():
            raise RuntimeError("the tool server's clone is not a directory")
        try:
            (verify or verify_pin)(here / clone, commit)
        except PinError as error:
            raise RuntimeError(str(error)) from None
    for index, condition in enumerate((tool.get("conditions") or {}).values()):
        where = f"conditions[{index}]"
        try:
            frozen = FrozenManifest.load(here / condition["manifest"])
        except (ManifestError, KeyError, TypeError):
            raise RuntimeError(
                f"{where}: its frozen manifest is missing, is not one, or its hash is not its tools'"
            ) from None
        shown, allowed = {t["name"] for t in frozen.tools}, set(condition.get("allow") or [])
        if shown != allowed:
            raise RuntimeError(
                f"{where}: its frozen manifest shows {len(shown - allowed)} tool(s) its allow-list lacks and lacks "
                f"{len(allowed - shown)} that the allow-list names: freeze it again from the pinned server with the "
                "condition's allow-list"
            )


def check_launchable(spec: Spec, study_artifacts: str | None) -> None:
    """Refuse, before anything is brought up, a `[toolserver]` the resolve launcher cannot start (O73, `launch_argv`):
    on a platform whose names resolve through it (HPC) the command must be a Python interpreter and the arguments start
    with `-m <module>` or name a script; the platform layer refuses the same at load, naming the key. Nothing for a
    template without `[toolserver]`."""
    from expcore_onedata.addresses import AddressMapError, launch_argv

    tool = (stand_in(_template(spec), study_artifacts) or {}).get("toolserver")
    if not isinstance(tool, dict) or not isinstance(tool.get("command"), str):
        return
    try:
        launch_argv(tool["command"], list(tool.get("args") or []), Path("resolve_launch.py"), Path("address-map.json"))
    except AddressMapError as error:
        raise RuntimeError(str(error)) from None


def check_rendered(layout: Layout, facts: Mapping[str, Any], *, environ: Mapping[str, str] | None = None) -> None:
    """Load the written platform file as a campaign would, its slots as the lease pool reads them, and refuse what a
    campaign would refuse or what disagrees with the layout, now: see the module's docstring. Nothing for a file with no
    `[slots]`. `facts` are the ones the file was rendered from (the ids of the users)."""
    if "slots" not in tomllib.loads(layout.platform.read_text(encoding="utf-8")):
        return
    from expcore.config.loading import ConfigError
    from expcore_onedata.config import load_platform
    from expcore_onedata.rights import BASELINE_RIGHTS
    from expcore_onedata.users import load_slots

    try:
        config = load_platform(layout.platform, environ=environ)
        slots = load_slots(layout.platform, config, environ=environ)
    except ConfigError as error:
        raise RuntimeError(f"the rendered platform file is refused: {error}") from None
    principals = {p.name: p for p in layout.spec.principals}
    for index, slot in enumerate(slots.slots):
        where = f"slots[{index}]"
        for plane, user in (("data_path", slot.data_path), ("toolserver", slot.tool_server)):
            principal = principals.get(user.username)
            if principal is None:
                raise RuntimeError(
                    f"{where}: its {plane} user is no principal of the layout, so nothing provisioned it"
                )
            if user.user_id != facts["principals"][principal.name]["user_id"]:
                raise RuntimeError(
                    f"{where}: the id of its {plane} user is not the one the federation gave that principal"
                )
            if not principal.agent:
                raise RuntimeError(f"{where}: its {plane} user is declared agent = false in the layout")
        if set(principals[slot.data_path.username].privileges) != set(BASELINE_RIGHTS):
            raise RuntimeError(f"{where}: its data-path user holds more or less than the baseline rights in the layout")
        if set(slot.rights) != set(principals[slot.tool_server.username].privileges):
            raise RuntimeError(
                f"{where}: its tool-server rights are not exactly the privileges the layout gave that user, which is "
                "what the bootstrap provisioned"
            )
    for condition in config.toolserver.conditions.values() if config.toolserver else ():
        try:
            slots.group_for(condition.rights)
        except ValueError:
            raise RuntimeError(
                f"{config.condition_label(condition.name)}: no slot's tool-server user holds its rights"
            ) from None
    for tokens, plane in (("data_path", "data_path"), ("tool_server", "toolserver")):
        reached = {entry.space.name for entry in config.spaces if tokens in entry.tokens}
        for index, slot in enumerate(slots.slots):
            user = slot.data_path if plane == "data_path" else slot.tool_server
            if set(principals[user.username].spaces) != reached:
                raise RuntimeError(
                    f"slots[{index}]: its {plane} user is not a member of exactly the spaces the platform file's "
                    f"{tokens} plane reaches, so its token would be refused"
                )
