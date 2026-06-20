---
name: sermon-notes-parser
description: >
  Convert sermon notes (text or an attached document) into the organization's tiptap
  XML format, ready to submit via the org API. Use when asked to parse, convert, or
  prepare sermon notes for upload.
license: MIT
compatibility: opencode
metadata:
  audience: internal
  workflow: documents
---

## What I do

- Take sermon notes — plain text or an attached/downloaded document — and convert them
  into the organization's **tiptap XML** representation.
- Preserve structure: title, section headings, scripture references, lists, and body
  text, each mapped to the correct tiptap node/mark.
- Emit **only** raw, well-formed XML — no Markdown, no code fences, no commentary.

## When to use me

Use me when the request is to parse / convert / prepare **sermon notes** (or a notes
document) into tiptap format — typically just before the `org-api` agent submits the
result to the org system.

## Conversion rules

<!-- TODO (provided later): the exact tiptap mapping. Define how each input element
     maps to tiptap nodes/marks. Placeholder example — REPLACE with the real schema:

       Title              -> <heading level="1">…</heading>
       Section heading    -> <heading level="2">…</heading>
       Scripture ref      -> (custom node/mark — TBD)
       Body paragraph     -> <paragraph>…</paragraph>
       Bulleted list      -> <bulletList><listItem><paragraph>…</paragraph></listItem></bulletList>

     Also specify: the root/doc node, attribute conventions, and any required wrappers. -->

_Mapping to be finalized — ask for the rules if they are not yet defined here._

## Output requirements

- A single, well-formed XML document using valid tiptap structure.
- Escape special characters: `&amp;`, `&lt;`, `&gt;`, `&quot;`.
- Output the XML and nothing else (no Markdown fences, no surrounding prose).
