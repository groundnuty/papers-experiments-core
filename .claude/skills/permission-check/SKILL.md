---
name: permission-check
description: Diagnose why Claude Code is or isn't asking for permission — which permission mode the session started in, where it came from, and which allow/deny rules apply. Use when the user asks why they are being prompted, why bypass or auto mode isn't taking effect, or to check their permissions. Read-only; reads files outside the repo only after the user agrees.
argument-hint: "(no arguments)"
allowed-tools: ["Read", "Bash", "Glob"]
disallowed-tools: ["Edit", "Write", "MultiEdit"]
---

<!-- Adapted from pedrohcgs/claude-code-my-workflow (MIT), https://github.com/pedrohcgs/claude-code-my-workflow -->


# Permission Check

## Purpose

Show why Claude Code is (or isn't) prompting: which permission mode the session started in, where that came from, and which allow/deny rules apply. The usual causes are a mode set somewhere Claude Code ignores, a session that started before a settings change, or a deny rule.

## How the starting mode is chosen (Claude Code 2.1.283+)

**Terminal sessions** take the first of these that applies:

1. The `--permission-mode` flag, or `--dangerously-skip-permissions`.
2. `permissions.defaultMode` in a settings file. Files rank managed > `--settings` > `.claude/settings.local.json` > `.claude/settings.json` > `~/.claude/settings.json`. **`auto` and `bypassPermissions` in the two project files don't take effect**: `auto` there falls through to the built-in default, `bypassPermissions` there starts the session in Manual. Set them in user or managed settings, or with the flag.
3. The built-in default: `auto` for interactive terminal sessions; `default` (Manual) for `claude -p`; `default` whenever any settings file sets `disableAutoMode`.

If `auto` is selected but auto mode is unavailable (a setting turns it off, the model doesn't support it), the session starts in Manual.

**VS Code extension conversations** take the first of: `claudeCode.initialPermissionMode` in VS Code *user* settings; the mode last picked from the mode indicator; `permissions.defaultMode` from managed or `~/.claude/settings.json`; the built-in default. The extension never reads a project's `.claude/settings*.json` for the starting mode.

**Mid-session**, Shift+Tab (or the mode indicator) changes the mode; `/status` shows the live one. A settings change does not affect a running session's mode.

**Rules** layer on top: deny rules block in every mode, including `bypassPermissions`; allow rules have no effect in `bypassPermissions`; protected paths are never auto-approved outside it.

## Privacy contract

Host-global settings files (`~/.claude/settings.json`, VSCode user settings) may contain:
- Paths to unrelated projects and secrets
- API keys, tokens, or provider credentials added outside this repo
- Permission policies set by the user's org or employer

This skill is designed for defense-in-depth: **Phase A runs automatically and reads only repo-local files.** Phase B reads host-global files **only after the user explicitly confirms** — never silently. When reporting host-global layers, redact any key that is not directly relevant to `permissions.*` or `claudeCode.*`.

## Protocol

### Phase A: Repo-local layers (auto-runs)

Read these immediately — they are checked into (or gitignored inside) the repo and do not cross the trust boundary:

```bash
VSCODE_WS="${CLAUDE_PROJECT_DIR}/.vscode/settings.json"
CLI_PROJECT="${CLAUDE_PROJECT_DIR}/.claude/settings.json"
CLI_LOCAL="${CLAUDE_PROJECT_DIR}/.claude/settings.local.json"
```

For each file that exists, extract:
- **VSCode workspace:** any `claudeCode.*` key — note that the starting mode comes from VS Code *user* settings, so a workspace `claudeCode.initialPermissionMode` is a likely cause of confusion
- **CLI project / project-local:** `permissions.defaultMode`, `permissions.allow`, `permissions.deny`

Missing files are fine — report "not present" rather than erroring.

Report each `defaultMode` found and whether it takes effect where it is set (`auto` and `bypassPermissions` in these two files do not). If that already explains the behavior, stop here and surface the diagnosis.

### Phase B: Host-global layers (requires explicit user confirmation)

If Phase A is inconclusive — e.g., all repo-local layers agree on bypass but the user is still being prompted — ask the user:

> "To complete the diagnosis, I need to read two files outside this repo:
> - `~/.claude/settings.json` (CLI user-level)
> - your VSCode user settings (`~/Library/Application Support/Code/User/settings.json` on macOS; Linux/Windows vary)
>
> These may contain unrelated paths or secrets. I will redact any key that isn't in `permissions.*` or `claudeCode.*`. Proceed?"

Only after the user confirms, read:

```bash
# VSCode user (platform-dependent path; try all three)
case "$(uname -s)" in
    Darwin)  VSCODE_USER="${HOME}/Library/Application Support/Code/User/settings.json" ;;
    Linux)   VSCODE_USER="${HOME}/.config/Code/User/settings.json" ;;
    MINGW*|MSYS*|CYGWIN*) VSCODE_USER="${APPDATA}/Code/User/settings.json" ;;
    *)       VSCODE_USER="" ;;
esac

CLI_USER="${HOME}/.claude/settings.json"
```

When reporting their contents, **extract only the relevant keys**:
- CLI user: `permissions.defaultMode`, `permissions.allow`, `permissions.deny`
- VSCode user: any key starting with `claudeCode.`

Never print the full file. Redact everything else to `(other keys redacted)`.

### Step 2: Resolve the starting mode

Apply the order above. Report which source chose the mode, and name any value that was set but ignored (and why). Then report the merged `allow` and `deny` lists across the CLI files.

### Step 3: Compare with the live mode

Ask the user to run `/status` (or read the footer: "auto mode on", "plan mode on", …). If it differs from the resolved starting mode, it was changed mid-session (Shift+Tab) or the session started before the settings changed — a new session picks up the settings.

### Step 4: Flag common failure modes

1. **Ignored value:** `auto` or `bypassPermissions` in `.claude/settings.json` or `.claude/settings.local.json` — not applied; move it to `~/.claude/settings.json` or use `--permission-mode`.
2. **VS Code reads different files:** a project `defaultMode` never reaches the extension; check `claudeCode.initialPermissionMode` and the last mode picked there.
3. **Auto mode unavailable:** `disableAutoMode` in any settings file, or a model without auto mode → Manual.
4. **Stale session:** settings are right but the session predates them → start a new session.
5. **A deny rule:** any matching deny blocks the tool in every mode.

## Output format

```
=== STARTING MODE ===
--permission-mode flag:            (not used)
managed settings:                  (none)
.claude/settings.local.json:       defaultMode: bypassPermissions   ← IGNORED here (project file) → Manual
.claude/settings.json:             (not set)
~/.claude/settings.json:           (not read — needs your OK)
Resolved: Manual, because bypassPermissions is not honored in project files

=== RULES ===
Merged allow: Bash, Edit, ...      Merged deny: Bash(git push --force*), ...

=== NEXT STEP ===
Move bypassPermissions to ~/.claude/settings.json, or start with --permission-mode bypassPermissions.
```

## Notes

- This skill is read-only. It never modifies settings.
- If `$CLAUDE_PROJECT_DIR` is unset, fall back to `git rev-parse --show-toplevel`.
- Platform-aware: detect macOS vs Linux vs Windows for the VSCode user path.
