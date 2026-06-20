---
name: sermon-notes-parser
description: >
  Convert a sermon-prep document (a Google Doc exported as .docx, or its text) into the
  organization's sermon-notes JSON — tiptap `originalContent` plus metadata — ready to
  submit via the org API. Use when asked to parse, convert, or prepare sermon notes for
  upload.
license: MIT
compatibility: opencode
metadata:
  audience: internal
  workflow: documents
---

## What I do

Turn a **sermon-prep document** into one **sermon-notes JSON object**: top-level metadata
plus an `originalContent` field holding the congregant-facing notes as a **tiptap
(ProseMirror) document** — `{"type":"doc","content":[…]}`.

The result is a **first-draft skeleton** — a person finalizes it in the editor afterward
(fixing typos, cutting tangents, restructuring prose). Aim for a faithful, well-structured
draft, **not** byte-exact output. Keep it lean (see the keep rule).

> Output is **JSON**, not XML. (Earlier drafts of this skill and `AGENTS.md` say "tiptap
> XML" — that is wrong; tiptap's native format is ProseMirror JSON.)

## When to use me

When the request is to parse / convert / prepare **sermon notes** (text, or an attached /
downloaded `.docx`) for the org system — typically just before the `org-api` agent
submits the result.

## Reading the document (the keep rule)

The source is the full leader prep doc; only a lean subset goes to congregants.

To read highlight + structure reliably, parse the **.docx** (a zip of XML), not the
plain-text export (which discards formatting). A run is highlighted when its `w:rPr`
contains `<w:highlight w:val="yellow"/>`. Capture each run's text, highlight, bold/italic,
heading level, and list nesting before converting:

```bash
mkdir -p _x && cd _x && unzip -o "$DOC.docx" >/dev/null   # then parse word/document.xml
```

**KEEP:**
- The `Read <passage>` line → the opening `bibleVerse`.
- Every **heading** (thesis + main points + sub-points) — renumbered, see below.
- Every **highlighted** run/line — highlight is the pastor's emphasis; it marks where the
  fill-in-blank power words and the emphasized phrases live.
- **Lists** (bulleted / numbered).
- **Quotations** — scripture quotes (`Proverbs 29:25, "…"`) and quotes attributed to a
  person or book (`Oswald Chambers once said, "…"`). These are usually copied verbatim
  **even when not highlighted**.

**DROP:**
- Header lines (`Date:`, `Location:`) — only `Date` and `Focus Passage` feed metadata.
- Every `[BRACKETED]` section and stage direction: `[NOTES TO REMEMBER]`, `[RESEARCH]`,
  `[PRE-INTRO]`, `[INTRODUCTION]`, `[THE GOSPEL]`, `[CUE …]`, `[SHOW … AD]`, `[PICTURE …]`.
  **This wins even if the line is highlighted** (e.g. a highlighted `[PICTURE …]` is still
  dropped).
- Plain exposition prose that is none of the above — leave it for the human to add back.

## Metadata

| Field | How to set it |
|---|---|
| `title` | The Google Doc's title, lightly cleaned (e.g. `Re:Imagine - Part 1`, `Faithfulness: Part 7`). |
| `sermonSeries` | The series portion of the title — strip the trailing part indicator (` - Part N`, `: Part N`) → `Re:Imagine`, `Faithfulness`. |
| `subtitle` | Always `""`. |
| `date` | Header `Date:` line → `YYYY-MM-DD` (e.g. `26 April 2026` → `2026-04-26`). |
| `passage` | Header `Focus Passage:` line, verbatim (e.g. `Malachi 3:16-18`). Also becomes the opening `bibleVerse`. |
| `speaker` | User-specified; **default `Pastor Peter Young`**. |
| `serviceType` | User-specified; **default `Sunday Celebration`**. |
| `imageLink` | User-specified; **default `""`**. |
| `sermonId` | `<prefix>-<DDMMYYYY>-1` from the date — `2026-04-26` → `sn-26042026-1`. The trailing `-1` is the service sequence for the day (default `1`). |
| `sermonLink` | `<prefix>-<mon><DD>` from the date — `2026-04-26` → `sn-apr26` (`mon` = lowercase 3-letter month, `DD` = zero-padded day). |
| `isDeleted` | Always `false`. |
| `isPublished` | Always `false`. |

`<prefix>` is **derived from `serviceType`**:

| serviceType | prefix |
|---|---|
| `Sunday Celebration` | `sn` |
| `Vision Sunday` | `vs` |
| `Special Events` | `se` |
| `Encounter` | `en` |

**Never emit `_id`, `createdAt`, or `updatedAt`** — MongoDB generates those on insert.

## Body — `originalContent` (tiptap `doc`)

### Headings (renumber cleanly)

The source outline numbers are inconsistent (mains `1./2.`, sub-points a running `1…7`).
**Renumber** in the output heading text:

- Thesis line (`The One Thing: …`) → `heading` level **1**, text as-is.
- Main points → `heading` level **2**, prefixed `I. `, `II. `, `III. ` (sequential Roman).
- Sub-points under a main point → `heading` level **3**, prefixed `A. `, `B. `, `C. `…
  (letters, **reset** under each main point).
- A repeated `The One Thing` near the end and a `Next Steps:` label are kept as a heading /
  bold paragraph respectively (level varies in the source — follow the doc).

### Structure

- **`paragraph`** — body lines; wrap runs in `text` nodes.
- **`bulletList` / `orderedList`** (`orderedList` has `attrs.start`, usually `1`) →
  `listItem` → `paragraph` → `text`. Lists nest as in the source.
- **`blockquote`** — a notable standalone quotation (person or book):
  ```json
  {"type":"blockquote","content":[{"type":"paragraph","content":[{"type":"text","text":"Oswald Chambers once said, “…”."}]}]}
  ```
  (Short inline scripture quotes can stay as a normal `paragraph` with a bold reference.)
- **`hardBreak`** — a soft line break **within one paragraph** (e.g. an `a) … b) … c) …`
  block the author kept as a single paragraph): `{"type":"hardBreak"}` between the lines.

### Custom nodes

- **`fillInBlank`** — the **"power word(s)"** of a kept line: the key term a congregant
  fills in (highlighted lines and headings are where these live). Pick it by meaning.
  A line splits into `text` (lead-in) + `fillInBlank` + `text` (trailing) as needed; a
  line can hold several.
  ```json
  {"type":"fillInBlank","attrs":{"editorText":"judgment","userText":"","currentId":"<new-uuid>"}}
  ```
  `editorText` = the answer word (keep its casing / trailing space), `userText` always
  `""`, `currentId` = a fresh UUID v4 (unique per blank).
- **`bibleVerse`** — a standalone scripture **reference / pointer**: the focus passage (the
  `Read <passage>` line) and cross-refs on their own line. `actionText` is `"READ"` or
  `null` (varies; default `"READ"`).
  ```json
  {"type":"bibleVerse","attrs":{"bibleVerse":"Malachi 3:16-18","actionText":null}}
  ```
  **Quoted scripture** (the verse text in quotes, e.g. `Proverbs 15:29, "…"`) stays as
  `text` — the reference often `bold`, the emphasized phrase carrying a `highlight` mark.
- **`userNotes`** — an empty note-taking placeholder inserted at section breaks (after a
  heading's content, before the next section; also right after the opening passage).
  ```json
  {"type":"userNotes","attrs":{"userNotes":null,"id":"<new-uuid-or-null>"}}
  ```
  Standalone placeholders get a fresh UUID `id`; placeholders nested inside a `listItem`
  use `id: null`.

### Marks

- `bold`, `italic` as in the source.
- `highlight` — any inline phrase highlighted **within kept text** → `{"type":"highlight"}`
  (with or without `bold`). A whole highlighted heading/line is the keep/emphasis signal,
  not itself a `highlight` mark.
- `textStyle` with `attrs.fontSize: "11pt"` on body/list text runs, as in the samples.

## Output

Emit **only** the JSON object — no Markdown fences, no commentary. Shape:

```json
{
  "sermonId": "...", "title": "...", "subtitle": "", "speaker": "...",
  "sermonSeries": "...", "date": "YYYY-MM-DD", "imageLink": "...",
  "originalContent": { "type": "doc", "content": [ /* tiptap nodes */ ] },
  "sermonLink": "...", "serviceType": "...", "passage": "...",
  "isDeleted": false, "isPublished": false
}
```

See `reference.json` in this folder for a full worked example (input doc → this output).
