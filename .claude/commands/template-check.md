---
description: Check if this repo is running the latest agentic-repo-template version and show the CHANGELOG delta.
---

# /template-check

Compare the template version applied to this repo against the latest public release at [groundnuty/agentic-repo-template](https://github.com/groundnuty/agentic-repo-template).

## What to do

1. **Read the local stamp.** Read `.claude/.template-version`. If it does not exist, print the "no stamp" message below and stop.

   ```bash
   cat .claude/.template-version
   ```

   The file contains `version=`, `profile=`, and `applied_at=` lines. Parse them.

2. **Fetch the latest release tag.** Prefer `gh` if available, fall back to `curl`:

   ```bash
   gh api repos/groundnuty/agentic-repo-template/releases/latest --jq '.tag_name' 2>/dev/null \
     || curl -fsS https://api.github.com/repos/groundnuty/agentic-repo-template/releases/latest | jq -r '.tag_name'
   ```

   If the call fails (offline, rate-limited, both tools missing), print the "no network" message below and stop.

3. **Compare.** If the stamp's `version` matches the latest tag, print the "up to date" message. Otherwise, print the "behind" message with the CHANGELOG delta (step 4).

4. **Fetch CHANGELOG delta** (only when behind):

   ```bash
   gh api repos/groundnuty/agentic-repo-template/contents/CHANGELOG.md --jq '.content' | base64 -d
   ```

   Extract entries strictly between the stamp's `version` (exclusive) and the latest tag (inclusive). Show one bullet per version with its headline description. Do not paste the full CHANGELOG.

5. **Do not modify any files.** This command is informational. No `init.sh` invocation, no `.template-version` rewrite.

## Output formats

**Up to date:**

```
Template: v0.1.9 (paper profile, applied 2026-04-20)
Latest:   v0.1.9
Up to date.
```

**Behind:**

```
Template: v0.1.7 (paper profile, applied 2026-04-18)
Latest:   v0.1.9

Changes since v0.1.7:
  v0.1.8 — Claude Code v2.1.113 adoption (sandbox-bypass fix)
  v0.1.9 — /template-check command + version stamping

To update, run `/template-upgrade`.
```

**No stamp:**

```
No .claude/.template-version file found.
Either this repo predates agentic-repo-template v0.1.9 or was not initialized via init.sh.
To start tracking, create the stamp manually:
  cat > .claude/.template-version <<EOF
  version=unknown
  profile=<info|research|paper|paper-latex|code>
  applied_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  EOF
```

**No network:**

```
Template: v0.1.9 (paper profile, applied 2026-04-20)
Could not reach api.github.com — latest version unknown.
```

## Notes

- `api.github.com` is already on the default sandbox network allowlist — no extra permissions needed.
- Private forks: this command always hits the public `groundnuty/agentic-repo-template` repo. If you maintain a private fork, edit this file to point at your fork's API.
- Upgrade discipline is documented in the template README's "Upgrading" section. This command tells you *that* you're behind; the README tells you *how* to catch up.
