---
name: github-pr
description: >
  Open a GitHub pull request — create a branch, push the change, and write a clear title
  and description. Use when asked to raise / open / create / submit a PR. Targets the
  hmcc-global org (default repo hmcchk-web).
license: MIT
compatibility: opencode
metadata:
  audience: internal
  workflow: github
---

## What I do

Open a pull request via the **GitHub MCP**, **API-only** (no local clone):
`create_branch` → `push_files` → `create_pull_request`, with a well-written description.

## Target

GitHub actions are for repos under the **`hmcc-global`** org. If the request names no
repo, default to **`hmcc-global/hmcchk-web`** (per `AGENTS.md`). Confirm the repo only if
the request is ambiguous.

## The repo is REMOTE — read it through the MCP

The target repo is **not checked out anywhere locally** — this is API-only. To see
existing code (e.g. to find the file/lines to change), **read and search it through the
GitHub MCP** (e.g. `get_file_contents`, repo search). **Do NOT** search the local
filesystem or spawn an Explore/file-search agent — the code is not on disk, so that just
hangs. No GitHub MCP = no repo access = you cannot do this; stop and say so.

## Flow

1. Resolve the repo (`hmcc-global/<repo>`, default `hmcchk-web`) and its default branch
   (usually `main`). Read the file(s) you need to change via the GitHub MCP first.
2. `create_branch` — a short, descriptive branch off the default branch, e.g.
   `fix/sermon-date-parse`, `feat/add-help-command`.
3. `push_files` — create/update only the files this change touches. Keep it small and
   focused; the human reviews before merge.
4. `create_pull_request` — from the branch into the default branch, with the title/body
   below.

## PR title & description

**Title:** concise and imperative — e.g. `Fix sermon date parsing`.

**Body:**
- **Summary** — what changed and why (1–3 sentences).
- **Changes** — bullet list of the notable edits.
- **Test plan** — how it was or should be verified (or `n/a` for docs-only).
- `Requested by: <name>` — from the `[Requester: <name>]` message context (`AGENTS.md`).
- `Closes #<n>` — when the PR resolves an issue, so merging auto-closes it.

## Reply

Return the **PR number + URL** and a one-line summary. On failure (auth, missing
repo/permission, protected branch), say so plainly with the error — never claim success
you didn't verify, and never echo tokens or env values.
