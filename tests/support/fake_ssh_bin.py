"""A fake `ssh` executable on PATH for the ssh transport's tests: it parses the argv the way ssh does (`-o K=V`
options, `--`, the destination, then the one command word), checks the options, logs each call, and runs the command
the way sshd does, by handing the joined string to `sh -c`, here on this host. A rule can make a call fail as ssh does
(exit 255, naming the host on stderr, as the real one does), hang (a sleep, with a child holding the pipes), or print
a reply that is not whole."""

import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT = r"""
import json, os, subprocess, sys, time
base = os.path.dirname(os.path.abspath(__file__))
argv, options, host, command = sys.argv[1:], [], None, None
while argv:
    word = argv.pop(0)
    if word == "-o":
        options.append(argv.pop(0))
    elif word == "--":
        host, command = argv.pop(0), argv
        break
    else:
        print("ssh: unknown option " + word, file=sys.stderr)
        sys.exit(255)
with open(os.path.join(base, "calls.jsonl"), "a") as log:
    log.write(json.dumps({"options": options, "host": host, "command": command}) + "\n")
if host is None or len(command) != 1:
    print("ssh: usage", file=sys.stderr)
    sys.exit(255)
rules = json.load(open(os.path.join(base, "rules.json")))
for rule in rules:
    if rule["match"] in command[0]:
        action = rule["action"]
        if action == "fail":
            print("ssh: Could not resolve hostname " + host + ": Name or service not known", file=sys.stderr)
            sys.exit(255)
        if action in ("hang", "hang_child"):
            if action == "hang_child":  # a child that holds the pipes, as a proxy command does
                child = subprocess.Popen(["sleep", "120"])
                with open(os.path.join(base, "child.pid"), "w") as handle:
                    handle.write(str(child.pid))
            time.sleep(120)
            sys.exit(0)
        if action == "garbage":
            print("this is not a reply")
            sys.exit(0)
os.execvp("sh", ["sh", "-c", command[0]])
"""


@dataclass
class FakeSshBin:
    """The directory holding the fake `ssh` (put on PATH by `install_fake_ssh`), its call log and its rules."""

    base: Path
    host: str
    rules: list[dict[str, str]] = field(default_factory=list)

    def rule(self, match: str, action: str) -> None:
        """Calls whose command holds `match` now `fail`, `hang`, `hang_child` or print `garbage`."""
        self.rules.append({"match": match, "action": action})
        (self.base / "rules.json").write_text(json.dumps(self.rules), encoding="utf-8")

    def clear(self) -> None:
        self.rules.clear()
        (self.base / "rules.json").write_text("[]", encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        path = self.base / "calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def child_gone(self, wait_s: float = 5.0) -> bool:
        """Whether the child a `hang_child` call started has ended (its group was killed)."""
        pid = int((self.base / "child.pid").read_text(encoding="utf-8"))
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            if _is_zombie(pid):
                return True
            time.sleep(0.05)
        return False


def _is_zombie(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def install_fake_ssh(directory: Path, monkeypatch: Any, host: str = "zz-login-7f3a.invalid") -> FakeSshBin:
    """A fake `ssh` first on PATH for the test, for the destination `host`."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "ssh_main.py").write_text(SCRIPT, encoding="utf-8")
    (directory / "rules.json").write_text("[]", encoding="utf-8")
    launcher = directory / "ssh"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{directory / "ssh_main.py"}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
    return FakeSshBin(directory, host)
