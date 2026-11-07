---
paths:
  - "CHANGELOG.md"
  - "README.md"
  - ".claude/skills/*/SKILL.md"
  - ".claude/rules/*.md"
  - ".claude/agents/*.md"
alwaysApply: false
---

<!-- Adapted from pedrohcgs/claude-code-my-workflow (MIT), https://github.com/pedrohcgs/claude-code-my-workflow -->


# Summary–Body Parity (anti–whack-a-mole)

**Do not surgical-patch a summary paragraph that has drifted from its body.** Summaries drift when the body changes and the summary is not re-verified. Fixing only the flagged phrase almost always leaves — or creates — another wrong claim in the same paragraph.

## What counts as a summary paragraph

A CHANGELOG version lede (the text before the first `###`); a README tagline or section lede; a PR title and its `## Summary` block; a skill / rule / agent frontmatter `description:`. Also anything of the shape "This release does X. It does not do Y. Counts are Z." — the triple-claim form is a drift magnet.

## The protocol

1. **Read the full body** the summary is summarizing. Not just the diff.
2. **Enumerate every substantive claim** in the summary: noun lists, counts, superlatives ("no new"), inclusions and exclusions.
3. **Check each claim against the body.**
4. **Rewrite the whole paragraph.** Every claim that no longer holds gets corrected, not just the one that was flagged.
5. **Re-scan for orphans** — a claim dropped from the summary must not linger unreferenced in the body, and vice versa.

## Two strikes, then restructure

If the same paragraph is flagged twice in a row — even on different words — stop patching and rewrite it structurally. Two hits means the paragraph's shape is wrong, not its wording.

**Bias toward abstraction.** A summary that makes no enumerative claim cannot drift. "On-disk inventory unchanged — see README for counts" survives edits that "27 skills / 13 agents / 22 rules" does not. The specific form is more informative when fresh and rots faster.

Repeated review-bot findings on one paragraph are a structural signal, not a list of bugs to patch one at a time.
