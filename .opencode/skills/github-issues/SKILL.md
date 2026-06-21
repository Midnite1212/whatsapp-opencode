---
name: github-issues
description: >
  Work with GitHub issues — list, read, comment, label, assign, close, and open new
  issues. Use for issue triage/housekeeping or filing a new issue. Targets the hmcc-global
  org (default repo hmcchk-web).
license: MIT
compatibility: opencode
metadata:
  audience: internal
  workflow: github
---

## What I do

Triage and create GitHub issues via the **GitHub MCP**. (To *fix* an issue with code and a
PR, use the **`github-fix-issue`** skill instead.)

## Target

Repos under the **`hmcc-global`** org; default **`hmcc-global/hmcchk-web`** if none is
named (per `AGENTS.md`).

## Triage existing issues

- **List / read** — list issues (filter by state / label / assignee as asked) or get one
  by number.
- **Comment** — add a clear, helpful comment.
- **Label / assign** — update as instructed.
- **Close** — only when asked; leave a short closing reason. Never close without being
  asked.

## Create a new issue

Write a clear **title** and a body with:
- **Context** — what and where (1–2 sentences).
- **Steps to reproduce** — numbered, for bugs.
- **Expected vs actual.**
- Apply obvious labels (`bug`, `enhancement`, …) if appropriate.

## Reply

Return the **issue number + URL** and what you did. Report failures plainly; never echo
tokens or env values.
