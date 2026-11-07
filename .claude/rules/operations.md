---
paths:
  - ".claude/settings.json"
  - ".claude/settings.local.json"
  - ".mcp.json"
  - ".mcp.json.example"
---

# Config mechanics

Loaded when you touch a settings or MCP file. Harness details that are easy to get wrong and silent when wrong.

## MCP allow rules need a literal server name

Claude Code rejects `mcp__*` wildcards in `permissions.allow` — otherwise an unknown server could auto-execute tools. Allow rules must name a server: `mcp__<server>__<tool>` or `mcp__<server>__*`.

The base template pre-allows the claude.ai-hosted connector servers (`mcp__claude_ai_Gmail__*`, `mcp__claude_ai_PubMed__*`, …), the context7 plugin MCP, and the two built-in introspection tools (`ListMcpResourcesTool`, `ReadMcpResourceTool`). For a custom or third-party plugin MCP, add its literal entry to the gitignored `.claude/settings.local.json`:

```json
{ "permissions": { "allow": ["mcp__my_custom_server__*"] } }
```

Until then its first tool call prompts, and the answer sticks for the rest of the session.

## MCP servers run outside the Bash sandbox

`sandbox.network`, `sandbox.filesystem.denyRead`, and `sandbox.credentials` constrain shell commands. They do **not** constrain MCP servers — an MCP server reads and reaches whatever its own process can. Enable only servers you trust.

Keep the server keys in `.mcp.json` matching the prefixes the shipped allow/deny rules use. Renaming the `papers` key silently disarms its Sci-Hub denies, and `enabledMcpjsonServers` auto-enables only the curated key names — a renamed server also loses auto-enable and prompts.

## File-permission rules use `Edit(path)`, not `Write(path)`

Only `Edit(path)` and `Read(path)` are matched as file-permission checks, and `Edit(path)` covers every file-editing tool — `Edit`, `Write`, `NotebookEdit`. Standalone `Write(path)` / `NotebookEdit(path)` / `Glob(path)` rules are not honored and trigger a `/doctor` startup warning, so adding a paired `Write(...)` entry only re-introduces the warning.

The base deny list guards each sensitive path (ssh, aws, gnupg, kube, gcloud, gitconfig, npmrc, pypirc, docker, netrc, gh, shell init files, `~/.claude/settings.json`, `~/.claude.json`) with one `Edit(<path>)` rule. Extend it the same way. Neither `Edit(...)` nor `Bash(...)` stops a sandboxed shell from *reading* a file — for that use `sandbox.filesystem.denyRead` or `sandbox.credentials`.

## Bash deny patterns match through exec wrappers

The `Bash(...)` denies (sudo, `git push --force*`, `docker push *`, `rm -rf /`, `git commit --no-verify`) also match when the command is wrapped in `env`, `sudo`, `watch`, `ionice`, `setsid`, and similar. `env sudo rm -rf /` is caught — no need to enumerate wrapped variants.
