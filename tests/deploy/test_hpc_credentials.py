"""The HPC bundle's per-launch Couchbase administrator (deploy/hpc-federation/bundle/lib.sh and node-init.sh).

The image's couchbase-cli 4.5.1 accepts a password of at most 24 characters. Over that, cluster-init prints an error
and exits 0 without creating the administrator (node.py:26 and 434-437 in the image), which is how the third live run
ran a Couchbase with none. So the bundle generates exactly 24 random alphanumerics, and refuses any administrator
outside couchbase-cli's limits before onepanel ever sees it.
"""

import re
import subprocess
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle"
COUCHBASE_CLI_MAX_PASSWORD = 24  # MAX_LEN_PASSWORD in the pinned images' couchbase-cli (node.py:26)
USER = "cb0123456789ab"  # node-init.sh's form: "cb" and 12 hex digits


def lib(command: str, *args: str) -> subprocess.CompletedProcess:
    """Run a bundle function in a fresh bash that sources lib.sh."""
    script = f'source "{BUNDLE}/lib.sh"; {command} "$@"'
    return subprocess.run(["bash", "-c", script, "lib", *args], capture_output=True, text=True, timeout=30, check=False)


def test_o53_a_generated_password_is_24_random_alphanumerics():
    """O53: the most couchbase-cli accepts, and nothing that needs quoting on onepanel's command line."""
    passwords = set()
    for _ in range(3):
        result = lib("gen_alnum", str(COUCHBASE_CLI_MAX_PASSWORD))
        assert result.returncode == 0, result.stderr
        assert re.fullmatch(r"[A-Za-z0-9]{24}", result.stdout), result.stdout
        passwords.add(result.stdout)
    assert len(passwords) == 3


def test_o53_the_bundle_password_length_is_couchbase_clis_limit():
    """O53: CB_PASSWORD_LEN is 24, couchbase-cli's MAX_LEN_PASSWORD; the third live run's 48 hex characters were not."""
    result = lib("printf '%s' \"$CB_PASSWORD_LEN\"")
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(COUCHBASE_CLI_MAX_PASSWORD)


def test_o53_node_init_generates_the_administrator_within_the_limit_and_checks_it():
    """O53: node-init.sh draws the password with gen_alnum at CB_PASSWORD_LEN and refuses the launch unless
    cb_cred_ok accepts the pair, before any secret is written."""
    text = (BUNDLE / "node-init.sh").read_text(encoding="utf-8")
    assert 'cb_pass=$(gen_alnum "$CB_PASSWORD_LEN")' in text
    check = text.index('cb_cred_ok "$cb_user" "$cb_pass" || die')
    assert check < text.index('put_secret "$S/couchbase-password"')


@pytest.mark.parametrize(
    ("password", "accepted"),
    [
        ("A" * 24, True),
        ("aZ09" * 6, True),
        ("A" * 25, False),  # one over couchbase-cli's limit: cluster-init would skip the administrator
        ("0123456789abcdef" * 3, False),  # the third live run's 48 hex characters
        ("A" * 23, False),
        ("A" * 23 + "-", False),  # outside [A-Za-z0-9]
    ],
)
def test_o53_cb_cred_ok_takes_only_a_password_couchbase_cli_accepts(password, accepted):
    """O53: cb_cred_ok, which node-init.sh, the rotation and the overlay all apply, accepts exactly 24
    alphanumerics."""
    result = lib("cb_cred_ok", USER, password)
    assert (result.returncode == 0) is accepted, result.stderr


@pytest.mark.parametrize(
    ("user", "accepted"),
    [(USER, True), ("a" * 32, True), ("a" * 33, False), ("", False), ("cb user", False), ("cb:user", False)],
)
def test_o53_cb_cred_ok_takes_only_a_user_couchbase_accepts(user, accepted):
    """O53: a user of 1 to 32 characters of [A-Za-z0-9._-], so user:password fits one line of couchbase-cli's
    base64 Basic header (57 bytes)."""
    result = lib("cb_cred_ok", user, "A" * 24)
    assert (result.returncode == 0) is accepted, result.stderr
