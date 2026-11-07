---
paths:
  - "devbox.json"
  - "**/devbox.json"
---

# Devbox usage

Applied when the repo has a `devbox.json`.

## Basics

- **All tool invocations go through `devbox run --`.** This pins the toolchain to what `devbox.json` declares.
- **`devbox shell`** gives you an interactive environment with all tools available. Preferred when iterating.
- **Add a package** with `devbox add <pkg>`. Commit both `devbox.json` and `devbox.lock`.

## In this repo

```
devbox run -- make check
devbox run -- make test
devbox shell
```

## CI

The CI workflow (when present) should use `devbox run --` rather than `apt-get install`. Ensures local/CI parity.

## Gotchas

- **`devbox init`** creates a fresh `devbox.json`. Don't run in an existing devbox repo.
- **`devbox update`** bumps lock entries. Review the diff; don't commit blindly.
- **Nix substituters:** if the sandbox disallows network access to an unexpected host, check `devbox.json`'s substituters or see `.claude/settings.json` sandbox.network.
