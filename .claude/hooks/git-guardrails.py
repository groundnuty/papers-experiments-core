#!/usr/bin/env python3
"""
Git guardrails hook (PreToolUse) — opt-in.

Blocks destructive git operations before they run, as a backstop to
`permissions.deny`, whose literal prefixes miss `git -C x reset --hard`,
`git reset HEAD~1 --hard`, quoted flags, and commands handed to a shell.

Blocks:
  - git reset --hard               (discards uncommitted work irrecoverably)
  - git clean -f / -xdf / --force  (deletes UNTRACKED files, including data)
  - git push --force / -f / +ref   (clobbers remote history; --force-with-lease allowed)
  - git add -A / --all / . / :/    (blanket staging can sweep in secrets)
  - git checkout . / git restore . (mass discard; `git restore --staged .` is allowed)
  - git commit --no-verify / -n    (skips the pre-commit hooks)

The command is read as the shell reads it: quotes ('…' "…" $'…' $"…"),
backslash-newlines and comments are undone, and it splits into commands at
; & && || | ( ) and newlines. git is found anywhere in a command (`timeout 9 git
…`), case-folded (`GIT` runs git on macOS), past any global options, and its
flags are tested as a set wherever they sit (`git reset HEAD~1 --hard`). What a
shell will run is read too: `sh -c`, `eval`, `env -S`, `ssh HOST CMD`, $(…),
`…`, output piped into a shell, and heredoc input. A heredoc body counts as
data only when its output provably lands in a file (`cat > f <<EOF`), a git
command, a $(…) argument (`git commit -m "$(cat <<'EOF' … EOF)"`), or a
non-shell interpreter (`python3 - <<'PY'`) — though $(…) in an unquoted body
still runs in the shell first, and interpreter output piped into a shell is
checked with the substring rules.

Not covered: git or a flag spelled via a variable (`$G reset --hard`), flags on
stdin (`xargs git reset`), aliases, other languages (`python3 -c`), a script
written now and run later. An UNQUOTED mention (`echo run git reset --hard`)
is denied, as it reads like `timeout 9 git reset --hard`; quote it instead.

Enable by referencing it from `.claude/settings.local.json`:

    {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 .claude/hooks/git-guardrails.py"}]}]}}

To make the guard fail CLOSED when it cannot run at all (python3 missing, a
timeout), add `"onFailure": "block"` beside "command" (Claude Code 2.1.295+;
older versions ignore the key — verified on 2.1.252). The cost: with no python3
on PATH, every Bash command is then blocked until you fix it.

Fails OPEN inside: any error exits 0 with no decision, so a bug in the parser
never wedges the session. A command it cannot parse (unbalanced quotes) gets the previous
regex rules instead. Python 3.8+, stdlib only.

Adapted from pedrohcgs/claude-code-my-workflow (MIT),
https://github.com/pedrohcgs/claude-code-my-workflow — which in turn adapted the
git-guardrails pattern from mattpocock/skills. The upstream hardcoded-path check
(warning on /Users/<u> paths in .R/.do/.qmd files) is not carried here: it is
specific to R/Stata replication packages, and this template is language-agnostic.
"""

from __future__ import annotations

import json
import os
import re
import sys

REASONS = {
    "reset": ("git reset --hard discards uncommitted work irrecoverably.",
              "Use `git stash` (recoverable), or reset specific paths."),
    "clean": ("git clean -f/--force deletes UNTRACKED files — including data not yet committed.",
              "Inspect with `git clean -n` first, then delete specific paths by hand."),
    "push": ("git push --force (or a +refspec) clobbers remote history.",
             "Use `git push --force-with-lease` if a branch genuinely must be rewritten."),
    "add": ("Blanket staging (git add -A / . / :/) can stage secrets, data, or settings.local.json.",
            "Stage specific files: `git add path/to/file ...`."),
    "discard": ("Mass discard of working-tree changes is irreversible.",
                "Discard specific files, or `git stash` to keep them recoverable."),
    "commit": ("git commit --no-verify/-n skips the pre-commit hooks.",
               "Fix what the hook reports, then commit normally."),
}

# ── git's flags, tested as a set ────────────────────────────────────────────

_ROOT = {".", "./", ":/", ":/."}  # pathspecs meaning "the whole tree"
_VALUE_GLOBALS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                  "--config-env", "--attr-source"}


def _flags(args):
    """Arguments before a `--` end-of-options marker."""
    return args[:args.index("--")] if "--" in args else args


def _long(args, name):
    """`name`, or an abbreviation of it: git runs `--ha` as `--hard`."""
    return any(len(a) > 2 and a.startswith("--") and name.startswith(a) for a in _flags(args))


def _short(args, letter, stop=""):
    """A short-option cluster sets `letter` (-xdf sets f). A letter in `stop`
    takes the rest of its cluster as a value (`-mn` is the message "n")."""
    for a in _flags(args):
        if len(a) > 1 and a[0] == "-" and a[1] != "-":
            for ch in a[1:]:
                if ch == letter:
                    return True
                if ch in stop:
                    break
    return False


def _discard(args):
    """checkout/restore of the whole tree that touches the working tree.
    `--staged` alone only unstages, so it is allowed."""
    if not _ROOT.intersection(args):
        return False
    staged = _long(args, "--staged") or _short(args, "S", "s")
    return _long(args, "--worktree") or _short(args, "W", "s") or not staged


RULES = {  # subcommand -> (predicate over the words after it, REASONS key)
    "reset": (lambda a: _long(a, "--hard"), "reset"),
    "clean": (lambda a: _long(a, "--force") or _short(a, "f"), "clean"),
    "push": (lambda a: _long(a, "--force") or _short(a, "f")
             or any(x.startswith("+") for x in a), "push"),
    "add": (lambda a: _long(a, "--all") or _short(a, "A") or bool(_ROOT.intersection(a)), "add"),
    "checkout": (_discard, "discard"),
    "restore": (_discard, "discard"),
    "commit": (lambda a: _long(a, "--no-verify") or _short(a, "n", "mFcCtuS"), "commit"),
}


def _git_calls(words):
    """(subcommand, args) for each git invocation in `words`."""
    for k, w in enumerate(words):
        if os.path.basename(w).lower() == "git":
            k += 1
            while k < len(words) and words[k].startswith("-"):
                k += 2 if words[k] in _VALUE_GLOBALS else 1
            if k < len(words):
                yield words[k], words[k + 1:]


def _git_rule(words):
    return next((RULES[s][1] for s, a in _git_calls(words) if s in RULES and RULES[s][0](a)), None)


# ── a small shell reader ────────────────────────────────────────────────────

_OPS = ("<<<", "<<-", "&>>", ";;&", "&&", "||", ";;", ";&", "|&", "<<", "&>",
        ">>", ">|", ">&", "<&", "<>", ";", "&", "|", ">", "<")
_REDIRS = {"<<<", "<<-", "<<", "&>>", "&>", ">>", ">|", ">&", "<&", "<>", ">", "<"}
_PLAIN = re.compile(r"[^ \t\n\\'\"$`#;&|<>()]+")
_ESC = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|[0-7]{1,3}|.)", re.S)
_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
           "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(body):
    """Decode the escapes bash expands inside $'…' ($'\\x2dA' is -A)."""
    def one(m):
        e = m.group(1)
        if e[0] in "xuU" and len(e) > 1:
            return chr(min(int(e[1:], 16), 0x10FFFF))
        if e[0] in "01234567":
            return chr(int(e, 8) & 0xFF)
        return _SIMPLE.get(e, "\\" + e)
    return _ESC.sub(one, body)


class _Cmd:
    """One simple command: its words, and what feeds or receives it."""

    def __init__(self):
        self.words = []       # argv, unquoted; redirection targets removed
        self.subs = []        # (word index, commands, quoted) for $(…), `…`, <(…)
        self.stdin = []       # (text, live, unterminated) heredocs/herestrings
        self.to_file = False  # stdout is redirected to a file
        self.piped = False    # stdout flows into | or |&


def _dq(s, i, end, cmd, widx, depth):
    """Read double-quoted text (or, with end="", a heredoc body) from s[i].
    $(…) and `…` inside are parsed and recorded on `cmd`. -> (value, next i)"""
    out, n = [], len(s)
    while i < n:
        c = s[i]
        if end and c == end:
            return "".join(out), i + 1
        if c == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
            out.append("" if s[i + 1] == "\n" else s[i + 1])
            i += 2
        elif s.startswith("$(", i) or c == "`":
            sub, i = _lex(s, i + (2 if c == "$" else 1), ")" if c == "$" else "`", depth + 1)
            cmd.subs.append((widx, sub, True))
            out.append("$(…)")
        else:
            out.append(c)
            i += 1
    if end:
        raise ValueError("unbalanced double quote")
    return "".join(out), n


def _heredoc(s, i, delim, strip_tabs):
    """Body from s[i] up to the delimiter line. -> (body, next i, found)"""
    m = re.compile("^%s%s$" % ("\t*" if strip_tabs else "", re.escape(delim)), re.M).search(s, i)
    return (s[i:m.start()], m.end() + 1, True) if m else (s[i:], len(s), False)


def _lex(s, i=0, closer="", depth=0):
    """Parse shell text into a flat list of _Cmd, stopping after `closer`
    (")" or "`") when reading a substitution. -> (commands, next i).
    Raises ValueError on an unbalanced quote."""
    if depth > 30:
        raise ValueError("nesting too deep")
    n, cmds, pending, parens = len(s), [], [], 0
    cur, word, started, quoted, redir, fd = _Cmd(), [], False, False, None, None

    def take(text, q=True):
        nonlocal started, quoted
        word.append(text)
        started, quoted = True, quoted or q

    def flush_word():
        nonlocal word, started, quoted, redir, fd
        if started:
            w = "".join(word)
            if redir is None:
                cur.words.append(w)
            elif redir in ("<<", "<<-"):
                pending.append((cur, w, redir == "<<-", quoted))
            elif redir == "<<<":
                cur.stdin.append((w, False, False))
            elif redir in ("&>", "&>>") or (fd in (None, 1) and (
                    redir in (">", ">>", ">|") or (redir == ">&" and not (w.isdigit() or w == "-")))):
                cur.to_file = True
            redir = fd = None
        word, started, quoted = [], False, False

    def flush_cmd(piped=False):
        nonlocal cur, redir
        flush_word()
        redir, cur.piped = None, piped
        cmds.append(cur)
        cur = _Cmd()

    while i < n:
        c = s[i]
        m = _PLAIN.match(s, i)
        if m:
            take(m.group(), False)
            i = m.end()
        elif c == "\\":
            if s[i + 1:i + 2] != "\n":               # backslash-newline is removed
                take(s[i + 1:i + 2])
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ValueError("unbalanced single quote")
            take(s[i + 1:j])
            i = j + 1
        elif s.startswith("$'", i):
            j = i + 2
            while j < n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise ValueError("unbalanced $' quote")
            take(_ansi_c(s[i + 2:j]))
            i = j + 1
        elif c == '"' or s.startswith('$"', i):
            val, i = _dq(s, i + (1 if c == '"' else 2), '"', cur, len(cur.words), depth)
            take(val)
        elif closer and c == closer and (closer == "`" or parens == 0):
            flush_cmd()
            return cmds, i + 1
        elif s.startswith(("$(", "<(", ">("), i) or c == "`":
            if c in "<>":                             # process substitution
                flush_word()
            sub, i = _lex(s, i + (1 if c == "`" else 2), "`" if c == "`" else ")", depth + 1)
            cur.subs.append((len(cur.words), sub, False))
            take("$(…)", False)
        elif c == "#" and not started:               # comment to end of line
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t":
            flush_word()
            i += 1
        elif c == "\n":
            flush_cmd()
            i += 1
            for cmd, delim, strip_tabs, q in pending:  # heredoc bodies start here
                body, i, found = _heredoc(s, i, delim, strip_tabs)
                cmd.stdin.append((body, not q, not found))
            del pending[:]
        elif c in "()":
            parens += 1 if c == "(" else (-1 if parens else 0)
            flush_cmd()
            i += 1
        else:
            op = next((o for o in _OPS if s.startswith(o, i)), None)
            if op is None:                            # a lone `$`, or `#` inside a word
                take(c, False)
                i += 1
                continue
            if op in _REDIRS:
                w = "".join(word)
                if started and not quoted and w.isdigit():  # `2>`: an fd, not a word
                    word, started, fd = [], False, int(w)
                else:
                    flush_word()
                redir = op
            else:
                flush_cmd(piped=op in ("|", "|&"))
            i += len(op)
    flush_cmd()
    return cmds, n


# ── what a command line will run ────────────────────────────────────────────

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "fish"}
# Interpreters whose input is their own language, not shell: a heredoc they read
# is data, as `python3 -c '…'` already is (see "Not covered" above).
_INTERP = re.compile(r"^(python[\d.]*|node|nodejs|deno|bun|ruby|perl|php|jq|yq|awk|gawk|sed)$")
_DASH_C = _SHELLS | {"su", "runuser", "flock", "script"}   # run the string after -c
_ARG_RUNNERS = {"eval", "ssh", "watch", "parallel", "tmux"}  # run their argument text
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_MAX_DEPTH = 8

# The rules from before the parser, kept for commands it cannot read.
_GO = (r"\bgit\s+(?:-C\s+\S+\s+|-c\s+\S+\s+|--git-dir(?:=\S+\s+|\s+\S+\s+)|"
       r"--work-tree(?:=\S+\s+|\s+\S+\s+)|--no-pager\s+|--paginate\s+|-p\s+)*")
_LEGACY = [(re.compile(_GO + p), key) for p, key in [
    (r"reset\s+--hard\b", "reset"),
    (r"clean\b.*(--force\b|(?<![\w-])-[a-z]*f)", "clean"),
    (r"push\b.*(--force(?![\w-])|(?<!-)\s-f\b)", "push"),
    (r"add\s+(?:--\s+)?(-A\b|--all\b|\.(?:\s|$)|:/)", "add"),
    (r"(checkout|restore)\s+(--\s+)?\.(?:\s|$)", "discard"),
]]


def _legacy(text):
    text = text.replace("\\\n", "")
    return next((key for pat, key in _LEGACY if pat.search(text)), None)


def _payload(words):
    """(index, command string) for the first program in `words` that runs a
    string: `sh -c STRING` (and su/flock/script -c), `eval`/`ssh`/`watch`/
    `parallel`/`tmux` ARGS, or `env -S STRING`."""
    for k, w in enumerate(words):
        name = os.path.basename(w).lower()
        if name in _ARG_RUNNERS:
            return k, " ".join(words[k + 1:])
        if name in _DASH_C:                           # -c, -lc, -ec … then STRING
            rest = words[k + 1:]
            for j, a in enumerate(rest):
                if len(a) > 1 and a[0] in "-+" and a[1] != "-" and "c" in a:
                    return k, next((x for x in rest[j + 1:] if x[:1] not in "-+"), None)
            return k, None
        if name == "env":
            rest = words[k + 1:]
            for j, a in enumerate(rest):
                if a in ("-S", "--split-string"):
                    return k, " ".join(rest[j + 1:j + 2])
                if a.startswith(("-S", "--split-string=")):
                    return k, a.split("=", 1)[1] if a.startswith("--") else a[2:]
    return None, None


def _feeds_shell(cmds, k):
    """Does cmds[k]'s output flow down a pipe into a shell (or ssh)?"""
    while cmds[k].piped and k + 1 < len(cmds):
        k += 1
        if any(os.path.basename(w).lower() in _SHELLS | {"ssh"} for w in cmds[k].words):
            return True
    return False


def _tee_to_file(words):
    k = next((k for k, w in enumerate(words) if os.path.basename(w) == "tee"), None)
    return k is not None and any(not a.startswith("-") for a in words[k + 1:])


def _scan(text, depth=0):
    """REASONS key for the first destructive git call `text` runs, else None."""
    if depth > _MAX_DEPTH:
        return _legacy(text)
    try:
        return _check(_lex(text)[0], depth)
    except (ValueError, RecursionError):
        return _legacy(text)


def _check(cmds, depth, out=None):
    """First REASONS key in `cmds`. `out` is where their output goes when that
    is known: "data" (an argument of a non-shell command) or "run" (a shell
    executes it, as in `bash -c "$(…)"`)."""
    for k, c in enumerate(cmds):
        why = _git_rule(c.words)
        shell, payload = _payload(c.words)
        if not why and payload:                      # bash -c '…', eval …, env -S …
            why = _scan(payload, depth + 1)
        runs = out == "run" or _feeds_shell(cmds, k)
        if not why and runs and len(c.words) > 1:   # echo 'git reset --hard' | sh
            why = _scan(_ansi_c(" ".join(c.words[1:])), depth + 1)
        prog = next((j for j, w in enumerate(c.words) if not _ASSIGN.match(w)), -1)
        for j, sub, quoted in c.subs:
            # A quoted "$(…)" argument (or x=$(…)) is data; an unquoted one is
            # word-split and may become the command: `timeout 5 $(echo …)`.
            data = shell is None and j != prog and (
                quoted or _ASSIGN.match(c.words[j] if j < len(c.words) else "x="))
            why = why or _check(sub, depth + 1, "data" if data else "run")
        names = [os.path.basename(w).lower() for w in c.words]
        interp = shell is None and any(_INTERP.match(n) for n in names) and not any(n in _SHELLS for n in names)
        for body, live, unterminated in c.stdin:
            if interp and (runs or c.piped):
                # `python3 - <<'PY' | sh`: what it prints is run, and we cannot
                # know what it prints — fall back to the substring rules.
                why = why or _legacy(body)
                continue
            inert = (shell is None and not runs and not c.piped and not unterminated
                     and (out == "data" or c.to_file or _tee_to_file(c.words) or interp
                          or any(True for _ in _git_calls(c.words))))
            why = why or ((live and _live(body, depth)) if inert else _scan(body, depth + 1))
        if why:
            return why
    return None


def _live(body, depth):
    """$(…) and `…` in an unquoted-delimiter heredoc body run even when the
    body itself only goes to a file."""
    holder = _Cmd()
    try:
        _dq(body, 0, "", holder, 0, depth)
        return next((w for w in (_check(sub, depth + 1, "data") for _, sub, _ in holder.subs) if w), None)
    except (ValueError, RecursionError):
        return _legacy(body)


def deny(reason: str) -> None:
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        return 0

    if data.get("tool_name", "") != "Bash":
        return 0

    cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
    key = _scan(cmd)
    if key:
        reason, alt = REASONS[key]
        deny(f"Blocked by git-guardrails: {reason} {alt} "
             f"(To override, run it yourself in a terminal outside Claude Code.)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open
