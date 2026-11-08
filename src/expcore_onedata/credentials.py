# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:160-193 (a credential file per user, mode 0600)
"""Where credentials come from, and the principals they belong to.

Nothing here holds a credential: `SecretSource` and `UserCredentials` name an environment variable, a file or a
directory, and read the value only when asked. A value is registered with `expcore.records.redact.register_secret` the
moment it is read, so no record written afterwards can hold it, and a message names the source and never the value.

- `SecretSource` is one secret, for the admin access token the client reads and examines with.
- `UserCredentials` is a leased platform user's own password: a file per user in one directory, named by a pattern with
  one `{user}`. The directory is a path in platform.toml or the name of an environment variable that holds it. A file
  that others can read is refused, as the earlier code refused it.
- `Principal` is a platform user as the layer knows it: the login `username` that its credential belongs to, and the
  `user_id` Onezone gives it, which names it in privilege reads and as the subject of its tokens. They differ.
"""

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

    def describe(self) -> str:
        """Name the source for a message."""
        return f"environment variable {self.ref}" if self.kind == "env" else f"file {self.ref}"

    def read(self, environ: Mapping[str, str] | None = None) -> str:
        """Return the secret, registered as one; raise ConfigError naming the source when it is missing or empty."""
        if self.kind == "env":
            value = (os.environ if environ is None else environ).get(self.ref, "")
        else:
            try:
                value = Path(self.ref).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ConfigError(f"{self.describe()}: cannot be read ({type(error).__name__})") from None
        return _registered(value, self.describe())


@dataclass(frozen=True)
class UserCredentials:
    """Where each platform user's own password is: a file per user in a directory, named by a pattern."""

    kind: str  # "env": the directory is in the environment variable `ref`; "dir": `ref` is the directory
    ref: str
    pattern: str = DEFAULT_PATTERN

    def describe(self) -> str:
        """Name the directory's source for a message."""
        return f"user credentials ({'environment variable' if self.kind == 'env' else 'directory'} {self.ref})"

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
        """Require the user's file to exist, to be a regular file and to be closed to others; return its path.

        Only the file's presence and mode are looked at; its contents are not read.
        """
        path = self.path_for(username, environ)
        try:
            mode = path.stat().st_mode
        except OSError:
            raise ConfigError(f"{self.describe()}: a user has no credential file") from None
        if not stat.S_ISREG(mode):
            raise ConfigError(f"{self.describe()}: a user's credential is not a regular file")
        if mode & 0o077:
            raise ConfigError(f"{self.describe()}: a user's credential file is readable by others")
        return path

    def read(self, username: str, environ: Mapping[str, str] | None = None) -> str:
        """Return a user's password, registered as a secret; the user's own message never holds it."""
        path = self.check(username, environ)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError(
                f"{self.describe()}: a user's credential cannot be read ({type(error).__name__})"
            ) from None
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
