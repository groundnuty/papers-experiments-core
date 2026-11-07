#!/usr/bin/env bash
#
# .claude/refresh-skills.sh — re-fetch upstream-sourced vendored skills.
#
# Reads .claude/skills.manifest.json; for each skill with source="git" and
# an "url" field, clones --depth 1 into a temp dir, strips .git, and
# atomically replaces .claude/skills/<name>/.
#
# Usage: ./.claude/refresh-skills.sh [<skill-name>]

set -eu

JQ="${JQ:-jq}"
GIT="${GIT:-git}"
MANIFEST=".claude/skills.manifest.json"

if [ ! -f "$MANIFEST" ]; then
  echo "no manifest at $MANIFEST — nothing to refresh" >&2
  exit 0
fi

if ! command -v "$JQ" >/dev/null 2>&1; then
  echo "error: jq not found on PATH (JQ=$JQ)" >&2
  exit 4
fi
if ! command -v "$GIT" >/dev/null 2>&1; then
  echo "error: git not found on PATH (GIT=$GIT)" >&2
  exit 4
fi

target_name="${1:-}"

refresh_one() {
  local name="$1" url="$2" ref="${3:-main}"
  local dest=".claude/skills/$name"
  local tmpdir
  tmpdir=$(mktemp -d)

  echo "[refresh-skills] $name: cloning $url ($ref)"
  if ! "$GIT" clone --depth 1 --branch "$ref" "$url" "$tmpdir/$name" >/dev/null 2>&1; then
    echo "error: git clone failed for $name ($url, ref=$ref)" >&2
    rm -rf "$tmpdir"
    return 2
  fi
  rm -rf "$tmpdir/$name/.git"

  # Atomic replace.
  local dest_tmp="${dest}.tmp"
  rm -rf "$dest_tmp"
  mv "$tmpdir/$name" "$dest_tmp"
  rm -rf "$dest"
  mv "$dest_tmp" "$dest"
  rm -rf "$tmpdir"
  echo "[refresh-skills] $name: updated"
}

entries=$("$JQ" -c '.skills[] | select(.source == "git")' "$MANIFEST")
[ -z "$entries" ] && { echo "manifest has no git-sourced skills — nothing to refresh" >&2; exit 0; }

while IFS= read -r entry; do
  name=$("$JQ" -r '.name'          <<<"$entry")
  url=$( "$JQ" -r '.url'           <<<"$entry")
  ref=$( "$JQ" -r '.ref // "main"' <<<"$entry")
  if [ -n "$target_name" ] && [ "$target_name" != "$name" ]; then
    continue
  fi
  refresh_one "$name" "$url" "$ref"
done <<<"$entries"

echo "[refresh-skills] done"
