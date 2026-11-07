<!-- Adapted from pedrohcgs/claude-code-my-workflow (MIT), https://github.com/pedrohcgs/claude-code-my-workflow -->

# Quality report — [branch or change set]

**Date:** YYYY-MM-DD
**Scope:** [what was reviewed — branch, PR, directory, release]
**Verdict:** PASS | PASS WITH FINDINGS | FAIL

## Summary

[2–3 sentences. What was checked, what state it's in, whether it's safe to merge or ship.]

## Checks run

| Check | Result | Notes |
|---|---|---|
| Tests | PASS / FAIL | [count, coverage, failures] |
| Lint / format | PASS / FAIL | |
| Build | PASS / FAIL | |
| [Domain check] | PASS / FAIL | |

## Findings

### Blocking

- **[Finding]** — [evidence: file:line, log excerpt, or reproduction]. **Fix:** [what to do].

### Non-blocking

- **[Finding]** — [evidence]. **Suggested fix:** [what to do].

## Not checked

[What this report does NOT cover, so the reader doesn't over-trust it.]
