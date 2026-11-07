---
description: Flush what this session learned into the files that outlive it - AGENTS.md, path-scoped rules, auto-memory, checkpoint - each routed to the surface that actually holds it.
---

# /compact-prepare

Run this **before context is lost** — when the `PreCompact` hook warns that
compaction is imminent, at the end of a working session, or whenever you would
otherwise type *"update AGENTS.md, memory and rules with whatever you'll
need to keep working well."*

Compaction compresses the conversation; it does not write anything to disk. What
is not in a file when it fires is gone. This command is the step that makes the
next context window as good as this one.

The work is **routing**. The same fact belongs in a different file depending on
who needs it and when, and putting it in the wrong one either wastes context in
every future session or loses it at the next upgrade.

## 1. Inventory

List what this session produced that outlives it. Write `(none)` for an empty
category rather than padding it.

- **Corrections the user made** to how you work — especially ones you would repeat.
- **Facts about this project** that were not obvious from the code: why something
  is built the way it is, what a command really does, what breaks.
- **Conventions** agreed during the session.
- **State**: what is in flight, blocked, or next.
- **Dead ends** — what was tried and did not work. These save the most time later
  and are the most often lost.

## 2. Route

| The item is… | Goes to | Why there |
|---|---|---|
| Guidance every agent needs, every session — including stack, commands, layout and do-not-touch zones | `AGENTS.md`, **below** `<!-- END template-managed -->`, in its matching section | The only instruction file: Claude Code, Codex and OpenCode all read it natively, and upgrades never touch that part |
| Only relevant to certain files | a path-scoped rule in `.claude/rules/` with `paths:` frontmatter | Loads only when those files are touched; costs nothing otherwise |
| A machine-local preference or learning | auto-memory (`/memory`) | Never syncs, never commits — right for "on this machine, X" |
| Where-am-I / what-is-next | ask the user to run `/checkpoint` (only they can invoke it) | A committed artifact that travels to other machines and collaborators |
| The narrative of what happened | the session log in `.claude/session-reports/session_logs/` (skeleton: `.claude/templates/session-log.md`) | Story, not instruction |

Three rules that matter more than the table:

- **Never write inside the template-managed fence in `AGENTS.md`.** The next
  upgrade regenerates that region and your text vanishes. Everything you add goes
  *below* `<!-- END template-managed -->`.
- **Never create a `CLAUDE.md`.** This repo has none on purpose: Claude Code reads
  `AGENTS.md` natively only while no `CLAUDE.md`-family file exists, so a new one
  silently hides every instruction in `AGENTS.md`.
- **Prefer the narrowest surface that works.** A fact that only matters when
  editing `*.tex` belongs in a path-scoped rule, not in the always-on corpus that
  every session in every harness pays for.

## 3. Write, then verify

1. Make the edits. One line per fact where possible — these files load every
   session, and length costs adherence.
2. **Grep before you write.** If a rule already says it, sharpen that line
   instead of adding a second one.
3. Verify: `grep -c "template-managed" AGENTS.md` still returns `2`, and
   `git diff --stat` shows only the files you intended.
4. Report what went where, one line each, so the user can correct the routing
   before it hardens into a file every future session reads.

## Do not

- Record something you will use in the next five minutes — that is the conversation.
- Record speculation. If you are unsure a fact is durable, say so and let the user
  decide rather than committing a guess that every future session will read.
