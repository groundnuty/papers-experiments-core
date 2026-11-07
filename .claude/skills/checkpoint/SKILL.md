---
name: checkpoint
description: Write a committed, cross-machine handoff snapshot - active plan, decisions made, file pointers with line numbers, open questions, and the next 1-3 concrete actions - to `.claude/session-reports/`. Use when the user says "checkpoint", "save state", "snapshot before I stop", "where am I", "wrap up for handoff", or before a long break, a model switch, or handing the repo to a collaborator. Auto-memory is machine-local and `/rewind` is in-session undo; the checkpoint is the artifact that travels with the repo.
argument-hint: "[short-topic-slug]"
disable-model-invocation: true
allowed-tools: ["Read", "Write", "Bash"]
---
<!-- Adapted from pedrohcgs/claude-code-my-workflow (MIT), https://github.com/pedrohcgs/claude-code-my-workflow -->

# Checkpoint

## Purpose

Write a state snapshot the next session can resume from in under a minute — yours on another machine, a collaborator's, or a fresh context after compaction. The session log answers *what happened and why*; the checkpoint answers *where am I and what is next*: facts, file pointers, and actions.

Three mechanisms look interchangeable; only one of them travels. **Auto-memory** is machine-local and never syncs. **`/rewind`** undoes within the current session. A **checkpoint** is a file committed to the repo, so it reaches anyone who clones it. That is the entire reason to write one — and the reason to commit it once written.

## When to use

- Before handing the repo to a collaborator — or to yourself on another machine, where auto-memory does not follow.
- Before a long break, a model switch, or the end of a working day — and when the `PreCompact` hook warns that compaction is imminent and mid-plan context is about to be compressed.

## When not to use

- For the narrative of the session — that is the session log (the "Session Logging" section of `AGENTS.md`, template `.claude/templates/session-log.md`). The checkpoint links to the latest log by path; it does not retell it.
- As a substitute for committing. Uncommitted work is still lost work: commit the work, then checkpoint, then commit the checkpoint. See the "Autonomous-work rules" section of `AGENTS.md`.

## Phase 1 — Gather state

Read these in order. If a source is missing, record `(none on disk)` rather than inventing content.

1. **Git state** — `git branch --show-current`, `git status -s`, `git log --oneline -10`, `git diff --stat HEAD`. Capture the branch, uncommitted file count, recent subjects, and files touched this session.
2. **Latest session log** — `ls -t .claude/session-reports/session_logs/*.md 2>/dev/null | head -1`. Pull its open questions, blockers, and next steps.
3. **Active plan or spec** — whatever this repo keeps on disk (e.g. `docs/plans/`, per the "PR discipline" section of `AGENTS.md`). Pull its status, files-to-modify list, and open questions.
4. **In-flight background work** — anything this session started and did *not* wait for: a background Bash command, a background subagent, a long compile or render, a review still out with another model. For each, note what is running, where its output lands, the command that checks on it, and what ends it.

A checkpoint that omits a running job orphans it: the next session sees no trace of the work, its output lands where nobody is looking, and the job is either redone from scratch or quietly abandoned.

## Phase 2 — Write the checkpoint

Write to `.claude/session-reports/YYYY-MM-DD_checkpoint_<slug>.md`. Take the slug from `$ARGUMENTS`; with no argument, derive one from the branch name and say which slug you used. Skeleton — expand each placeholder, drop any section that would be padding:

```markdown
---
date: YYYY-MM-DD
branch: <current branch>
status: in_progress | paused | ready-to-merge
session-log: <path, or (none)>
plan: <path, or (none)>
---

# Checkpoint — <short topic>

## Goal
<One sentence: what this work is trying to accomplish.>

## Where I am
<One short paragraph or 3-5 bullets: last step completed, step in progress, what has not started.>

## File pointers
<3-8 concrete `path:line` references marking where the next session resumes, each with a note:>
- `path/to/file.ext:142` — half-written handler, error branch missing

## In flight
<Jobs still running that this session did not wait for, one row each. Write (none) if nothing is running — an empty section reads as an oversight.>
| What is running | Output lands in | Check with | Ends when |
|---|---|---|---|
| full test suite, backgrounded | `/tmp/suite.log` | `tail -3 /tmp/suite.log` | a `Passed:` line appears |

## Decisions made
<2-5 bullets on why the work took the shape it did — what the diff does not explain. Omit if there were none.>

## Open questions
<What you would ask if someone else picked this up. Number them Q1, Q2, ...>

## Next actions
1. <Imperative. Concrete. The next session starts here.>  2. <...>  3. <...>

## Resume prompt
> Resuming from `.claude/session-reports/<filename>`. Read it, then start at action 1.
```

Keep it short enough to read at a glance. If the state does not fit, the plan file is the right home for the detail — the checkpoint is a thin index pointing at it.

## Phase 3 — Report

Print to chat, then offer to commit the file — an uncommitted checkpoint is machine-local, which is the one thing it must not be:

```
Checkpoint saved: .claude/session-reports/<filename>
  Branch: <branch>   Status: <status>   Uncommitted: <N> files
  Session log: <path or none>   Open questions: <count>
  Resume: `claude --continue`, or paste the checkpoint's resume prompt into a fresh session.
```

## Notes

- **No slug, no branch to derive from.** Ask the user for one word rather than guessing a topic.
- **Nothing to point at.** If the file-pointers section would come out empty, nothing changed this session — a session log line is the better artifact and the checkpoint is premature.
