---
paths:
  - "explorations/**"
---

<!-- Adapted from pedrohcgs/claude-code-my-workflow (MIT), https://github.com/pedrohcgs/claude-code-my-workflow -->

# Explorations

**Experimental work goes into `explorations/` first, never straight into a production directory.** The point of the folder is a lower bar: exploration code is held to a lower bar than production. No plan needed; full docs and full test coverage not needed. What must hold is that the code runs, the results are correct, and the goal is written down.

## Layout

```
explorations/
├── ACTIVE_PROJECTS.md
├── [project]/
│   ├── README.md          # goal, status, findings
│   ├── SESSION_LOG.md     # 2–3 lines appended as you work
│   ├── <code-dirs>/       # whatever your stack needs (src/, R/, notebooks/)
│   └── output/            # results
└── ARCHIVE/
    ├── completed_[project]/
    └── abandoned_[project]/
```

Create the folder plus a README from `.claude/templates/exploration-readme.md`, then code immediately.

## Lifecycle

Before building, ask whether it improves the project. If not, don't build it. Then work entirely inside the exploration folder until one of three exits:

- **Graduate** — tests pass, results replicate within tolerance, code readable without deep context, README explains approach and findings. Copy into the production tree, then move the exploration to `ARCHIVE/completed_[project]/`.
- **Keep exploring** — record next steps in the README.
- **Archive** — move to `ARCHIVE/abandoned_[project]/` with an explanation written from `.claude/templates/archive-readme.md`.

Archiving is available at any point and is a normal outcome, not a failure: exploration is uncertain by design.
