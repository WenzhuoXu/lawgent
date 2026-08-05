# Tracked-changes redlining — deep reference

## OOXML revision semantics

A Word tracked change is not metadata *about* an edit — it *is* the edit,
kept in the document body so Word can accept or reject it:

- **Insertion** — the new runs are wrapped in
  `<w:ins w:id="…" w:author="…" w:date="…"> <w:r><w:t>new text</w:t></w:r> </w:ins>`.
  Accept keeps the runs (unwrapping the `w:ins`); reject deletes them.
- **Deletion** — the removed runs are wrapped in
  `<w:del …>` and every `w:t` inside **must** become `w:delText`
  (Word refuses to open a file with `w:t` inside `w:del`). Accept removes
  the runs; reject unwraps them back into normal text.
- **Replacement** — a `w:del` (old text) immediately followed by a
  `w:ins` (new text). There is no dedicated "replace" element.
- `w:id` must be unique per revision across `document.xml`; `w:date` is an
  ISO-8601 UTC timestamp (`2026-07-21T09:00:00Z`). Both `w:ins` and `w:del`
  are siblings of `w:r` inside `w:p`.

## Comments

A margin comment is three markers plus a part:

- `<w:commentRangeStart w:id="N"/>` before the first covered element,
  `<w:commentRangeEnd w:id="N"/>` after the last, then a
  `<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
  <w:commentReference w:id="N"/></w:r>` run;
- the comment body lives in `word/comments.xml` (python-docx ≥ 1.2 manages
  the part, content types, and rels).

`redline_docx_document` places range markers at `w:p`-child level — as
siblings of the `w:ins`/`w:del` they annotate, never inside them — which is
what Word itself emits when a reviewer comments on revised text.

## Anchoring and ordering rules

- Matching scans only direct `w:r` children of each paragraph, so text
  inside an earlier edit's `w:ins`/`w:del` is never re-matched and revisions
  never nest. Consequence: order edits so later `find` strings do not depend
  on earlier *inserted* text.
- Occurrences are applied right-to-left within a paragraph so character
  offsets stay valid; all occurrences of a `find` are revised — use a longer,
  unique anchor when only one occurrence is intended.
- A `find` cannot cross a paragraph boundary. For paragraph-level
  restructuring, do a clean structural edit (`reshape_docx`) on an interim
  copy *before* redlining, or split the edit into per-paragraph anchors.
- Boundary runs split by a match are collapsed to `rPr` + one `w:t`; the
  inserted run copies the `rPr` of the first replaced run so the insertion
  matches surrounding formatting.

## Verification loop (binding practice)

1. `redline_docx_document(...)` → check `not_found` is empty, counts match
   the intended number of occurrences.
2. `preview_docx_revisions(out, "accept")` — must equal the intended final
   text; `preview_docx_revisions(out, "reject")` — must equal the original.
3. `extract_docx_revisions(out)` — spot-check author, ISO-UTC date, and the
   exact inserted/deleted strings.
4. For high-stakes deliverables, render (`soffice --convert-to pdf`) and
   eyeball the revision marks.

## PRC-first notes

- 中文修订版: keep the source document's CJK fonts; the redline machinery
  never touches style parts, so a 宋体/仿宋 document stays 宋体/仿宋.
- Author strings may be Chinese (e.g. `作者="法律AI助手"`); Word renders
  reviewer names verbatim.
- Statutory quotes inside inserted text follow the global citation rule —
  pinpoint (条/款/项) in the accompanying answer's `## 资料来源`.
