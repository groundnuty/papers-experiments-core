---
paths:
  - "src/**"
  - "tests/**"
---

# Importing code from earlier studies

- Code from an earlier study enters only through `uv run python -m expcore.provenance.sanitise import --map <map> --source-repo <name> --commit <full sha> --path <source path> <blob file> <dest>`. The import commit holds only the sanitised files; every adaptation is a later commit. Never copy source text by hand.
- Get the blob with `git -C <source repo> show <commit>:<path> > <file>` and confirm `git hash-object <file>` equals `git -C <source repo> rev-parse <commit>:<path>`. Never read from the source repo's working tree, which may be on another branch.
- If an import is refused (a mapped name survives), stop and report. Do not edit around the refusal.
- Commit messages must name no file, path or term from the earlier studies: write the message to `$TMPDIR/msg.md` and run `uv run python -m expcore.provenance.study_tokens check $TMPDIR/msg.md` before committing.
- Test fixtures use invented names only. Short real names (two letters) collide with the token list.
- Every regression test's docstring starts with its ID from `docs/regressions.md`, for example `"""B01: ..."""`.
