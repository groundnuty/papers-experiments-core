# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:160-193 (a credential file per user, mode 0600)
"""Where credentials come from, and the principals they belong to.

Nothing here holds a credential: `SecretSource` and `UserCredentials` name an environment variable, a file or a
directory, and read the value only when asked. A value is registered with `expcore.records.redact.register_secret` the
moment it is read, so no record written afterwards can hold it. A message names the config key of the source and never
the value, nor the variable, file or directory the key points at.

- `SecretSource` is one secret, for the admin access token the client reads and examines with.
- `UserCredentials` is a leased platform user's own password: a file per user in one directory, named by a pattern with
  one `{user}`. The directory is a path in platform.toml or the name of an environment variable that holds it.
- Every credential file, the admin's included, must be a regular file (a link is not followed) owned by the user running
  the process, with no group or other permission bit (mode 0600). The check is made on the open file that is then read,
  so the file cannot be swapped between the check and the read.
- `Principal` is a platform user as the layer knows it: the login `username` that its credential belongs to, and the
  `user_id` Onezone gives it, which names it in privilege reads and as the subject of its tokens. They differ.
"""

import errno
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from expcore.config.loading import ConfigError
from expcore.records.redact import MIN_SECRET_LEN, register_secret

PLAIN_ID = re.compile(r"[A-Za-z0-9_.@-]{1,128}")
DEFAULT_PATTERN = "{user}.pass"


def _check_file(info: os.stat_result, where: str) -> None:
    """Require a regular file owned by the current user that no group or other can access; `where` names the key."""
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ConfigError(f"{where}: is not a regular file owned by the current user")
    if info.st_mode & 0o077:
        raise ConfigError(f"{where}: is open to group or others; a credential file must be mode 0600")


def read_credential_file(path: Path, where: str) -> str:
    """Read a credential file after checking the open file itself; raise ConfigError naming `where`, never the path.

    The file is opened without following a link and without blocking (so a pipe is refused, not waited on), and what
    is checked is the open file, not the name, so it cannot be replaced between the check and the read.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:  # a link, which is not followed
            raise ConfigError(f"{where}: is not a regular file owned by the current user") from None
        raise ConfigError(f"{where}: cannot be read ({type(error).__name__})") from None
    try:
        _check_file(os.fstat(descriptor), where)
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        try:
            return handle.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError(f"{where}: cannot be read ({type(error).__name__})") from None


def _registered(value: str, where: str) -> str:
    """Strip a credential, refuse an empty or too short one, register it as a secret and return it."""
    value = value.strip()
    if not value:
        raise ConfigError(f"{where}: is not set or is empty")
    if len(value) < MIN_SECRET_LEN:
        raise ConfigError(f"{where}: holds fewer than {MIN_SECRET_LEN} characters, too short to redact from a record")
    register_secret(value)
    return value


@dataclass(frozen=True)
class SecretSource:
    """Where one secret comes from: the name of an environment variable or the path of a file, never the value."""

    kind: str  # "env" or "file"
    ref: str
    key: str = "credentials.admin"  # the config key a message names it by

    def describe(self) -> str:
        """Name the source for a message by its config key and kind; the variable or path is not repeated."""
        return f"[{self.key}] {'environment variable' if self.kind == 'env' else 'file'}"

    def read(self, environ: Mapping[str, str] | None = None) -> str:
        """Return the secret, registered as one; raise ConfigError naming the source when it is missing or empty."""
        if self.kind == "env":
            value = (os.environ if environ is None else environ).get(self.ref, "")
        else:
            value = read_credential_file(Path(self.ref), self.describe())
        return _registered(value, self.describe())


@dataclass(frozen=True)
class UserCredentials:
    """Where each platform user's own password is: a file per user in a directory, named by a pattern."""

    kind: str  # "env": the directory is in the environment variable `ref`; "dir": `ref` is the directory
    ref: str
    pattern: str = DEFAULT_PATTERN

    def describe(self) -> str:
        """Name the directory's source for a message by its config key; the variable or path is not repeated."""
        return f"[credentials.users] {'directory_env' if self.kind == 'env' else 'directory'}"

    def directory(self, environ: Mapping[str, str] | None = None) -> Path:
        """Return the directory of the credential files; raise ConfigError naming the source if it is not one."""
        if self.kind == "env":
            named = (os.environ if environ is None else environ).get(self.ref, "").strip()
            if not named:
                raise ConfigError(f"{self.describe()}: is not set")
            path = Path(named)
        else:
            path = Path(self.ref)
        if not path.is_dir():
            raise ConfigError(f"{self.describe()}: is not a directory")
        return path

    def path_for(self, username: str, environ: Mapping[str, str] | None = None) -> Path:
        """Return the credential file of a user; the name must be plain, so it cannot leave the directory."""
        if not PLAIN_ID.fullmatch(username) or username.startswith("."):
            raise ConfigError(f"{self.describe()}: the user name is not a plain name")
        return self.directory(environ) / self.pattern.replace("{user}", username)

    def check(self, username: str, environ: Mapping[str, str] | None = None) -> Path:
        """Require the user's file to exist, to be a regular file the user running this owns and to be mode 0600.

        Only the file's presence, type, owner and mode are looked at; its contents are not read.
        """
        path = self.path_for(username, environ)
        try:
            info = path.lstat()  # a link is not followed: it is not a regular file
        except OSError:
            raise ConfigError(f"{self.describe()}: a user has no credential file") from None
        _check_file(info, f"{self.describe()}: a user's credential file")
        return path

    def read(self, username: str, environ: Mapping[str, str] | None = None) -> str:
        """Return a user's password, registered as a secret; the user's own message never holds it."""
        path = self.check(username, environ)
        text = read_credential_file(path, f"{self.describe()}: a user's credential file")
        return _registered(text, f"{self.describe()}: a user's credential")


@dataclass(frozen=True)
class Principal:
    """A platform user: the login name its credential belongs to, and the id Onezone knows it by."""

    username: str
    user_id: str

    def __post_init__(self) -> None:
        """Refuse names that are not plain, because both go into a request."""
        for what, value in (("username", self.username), ("user_id", self.user_id)):
            if not isinstance(value, str) or not PLAIN_ID.fullmatch(value):
                raise ValueError(f"a principal's {what} must be a plain name of letters, digits and _.@-")
