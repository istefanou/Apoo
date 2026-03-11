#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/johnara/Projects/Apoo"
BRANCH="main"

cd "$REPO_DIR"

# Ensure repo exists
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 1
fi

# Ensure branch exists
current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  git checkout "$BRANCH" >/dev/null 2>&1 || true
fi

# Stage and commit only when there are changes
git add -A
if ! git diff --cached --quiet; then
  msg="$(date '+%Y-%m-%d') backup"
  git commit -m "$msg"

  # Push if origin is configured
  if git remote get-url origin >/dev/null 2>&1; then
    git push origin "$BRANCH" || true
  fi
fi
