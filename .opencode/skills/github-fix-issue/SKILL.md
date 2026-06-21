---
name: github-fix-issue
description: >
  Read a referenced GitHub issue, implement the fix, and open a PR that closes it.
  Combines reading the issue with the PR flow. Use when asked to fix / resolve / address a
  GitHub issue. Targets the hmcc-global org (default repo hmcchk-web).
license: MIT
compatibility: opencode
metadata:
  audience: internal
  workflow: github
---

## What I do

Take a GitHub issue, implement the smallest correct fix, and open a PR that closes it —
via the **GitHub MCP**, API-only.

## Target

Repos under the **`hmcc-global`** org; default **`hmcc-global/hmcchk-web`** if none is
named (per `AGENTS.md`).

## The repo is REMOTE — read it through the MCP

The target repo is **not checked out locally** (API-only). Read the issue *and* the code
you need to change **through the GitHub MCP** (`get_issue`, `get_file_contents`, repo
search). **Do NOT** search the local filesystem or spawn an Explore/file-search agent —
the code isn't on disk, so that just hangs. No GitHub MCP = no access; stop and say so.

## Flow

1. **Read the issue** — `get_issue` for the repo + number given. Understand the problem
   and what "done" means. If it's unclear or risky to implement blind, **ask** rather than
   guess.
2. **Implement** the smallest correct change. Read the relevant files **via the GitHub
   MCP** first; follow the target repo's own `CLAUDE.md` / `AGENTS.md` and existing
   patterns — read before editing.
3. **Open the PR** following the **`github-pr`** skill (`create_branch` → `push_files` →
   `create_pull_request`).
4. In the PR body include **`Closes #<n>`** (merging then auto-closes the issue) and
   `Requested by: <name>` (from `[Requester: <name>]`).

## Reply

Return the **PR number + URL** and the issue it closes. If you couldn't safely implement
the fix, say what's blocking and ask — don't fabricate a fix or claim unverified success.
Never echo tokens or env values.
