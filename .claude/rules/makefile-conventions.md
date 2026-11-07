---
paths:
  - "Makefile"
  - "**/Makefile"
  - "makefile"
---

# Makefile conventions

Applied when the project ships a `Makefile` (or `dev.mk`).

## Standard targets

| Target | Purpose |
|---|---|
| `check` | Fast: lint + type-check + unit tests. Target runtime under 60s. |
| `test`  | Full: unit + integration tests. |
| `build` | Produce whatever artifact this repo produces. |
| `clean` | Remove build artifacts. Must not touch tracked files. |
| `fmt`   | Format all source files. |
| `lint`  | Just the linter. `check` depends on this. |

## Invocation discipline

- Claude invokes `make <target>`. It does **not** invoke the underlying tools directly (`go test`, `pytest`, `eslint`) unless the user explicitly asks.
- Rationale: the Makefile encodes the project's contract for "how to run checks." Bypassing it risks running against the wrong config, ignoring setup steps, or diverging from CI.

## Devbox wrapper

If this repo uses devbox:

```
devbox run -- make check
```

## Never

- `make push` — blocked implicitly via the git-push deny list.
- Silently patch a Makefile to make a failing test "pass" by not running it.
- `make -j` without reason — amplifies non-deterministic failures.
