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
- **Whole paragraphs** — the paragraph *mark* is tracked too:
  `w:pPr/w:rPr/w:ins` on an inserted paragraph, `w:pPr/w:rPr/w:del` on a
  deleted one. Rejecting an inserted mark joins that paragraph to the next
  (so an inserted paragraph vanishes cleanly); accepting a deleted mark does
  the same for a deleted one.
- `w:id` must be unique per revision across `document.xml`; `w:date` is an
  ISO-8601 UTC timestamp (`2026-07-21T09:00:00Z`).

## Comments

A margin comment is three markers plus a part:

- `<w:commentRangeStart w:id="N"/>` before the first covered element,
  `<w:commentRangeEnd w:id="N"/>` after the last, then a
  `<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
  <w:commentReference w:id="N"/></w:r>` run;
- the comment body lives in `word/comments.xml` (python-docx ≥ 1.2 manages
  the part); `redline_docx` also defines the `CommentReference` style when
  the document lacks it, so the reference does not point at nothing.

Range markers sit as siblings of the `w:ins`/`w:del` they annotate, never
inside them — what Word itself emits when commenting on revised text.

## Anchoring

- **Text.** `find` is matched against the paragraph's text outside existing
  revisions, so someone else's pending insertion or deletion is never
  re-matched and revisions never nest. If the exact string misses only
  because the document has a tab or line break where `find` has a space, it
  still matches, and the result says `matched_with_whitespace_normalized`.
- **Numbering labels are not text.** 第一条, 1.1 and （一） rendered from
  `numbering.xml` do not exist in the paragraph's XML. Scope with
  `clause="第一条"` (第1条, "1.1", "Article 5" and heading words also
  resolve) and keep the label out of `find`.
- **Paragraph index.** `paragraph` is the 0-based body index `inspect_docx`
  lists, counted on the document *before* the call.
- Every match in scope is revised unless `occurrence` picks one.
- A `find` cannot cross a paragraph boundary: split it into one change per
  paragraph, or delete/insert whole paragraphs.
- Text boxes, headers and footers are not redlined; such a `find` comes back
  `not_found` with that reason.

## What a split run keeps

When a match starts or ends inside a run, the run is split element by
element: `w:tab` and `w:br` move whole (so "Term:\tthree years" redlined to
five reads "Term:\tfive years" once accepted — the earlier engine dropped the
tab), and zero-width children — footnote and comment references, field
characters, drawings — are kept outside the deletion rather than deleted with
the text around them. Inserted text copies the `rPr` of the first replaced
run so it matches the surrounding formatting.

## Verification (the round-trip contract)

1. `not_found` is empty and per-change counts match the intended number of
   occurrences; `integrity.status` is `ok`.
2. Accepting every revision gives exactly the intended text; rejecting every
   revision gives exactly the original. The test suite checks both on the
   XML (`preview_docx_revisions`) and with LibreOffice's own accept-all /
   reject-all.
3. `read_document(output)` shows the marks as `{++…++}` / `{--…--}` — spot-check
   author and wording there; render (`render_docx_pages`) for high-stakes work.

## Compare (比对稿)

`compare_docx` drives LibreOffice's document compare headless: differences
become native revisions attributed to the given author, and Han text is
compared character by character (签字 → 盖章 is a two-character change, not a
rewritten sentence). LibreOffice's DOCX export drops the tracked mark of a
wholly inserted paragraph, so a reject-all can leave one empty paragraph; the
`round_trip` check ignores empty paragraphs and reports any real mismatch.

## PRC-first notes

- 中文修订版: the redline never touches style parts, so a 宋体/仿宋 document
  stays 宋体/仿宋.
- Author strings may be Chinese (e.g. `author="法律AI助手"`); Word renders
  reviewer names verbatim. Separate substantive from drafting changes with
  `category` + `category_authors` so a 合伙人 can review one kind at a time.
- Statutory quotes inside inserted text follow the global citation rule —
  pinpoint (条/款/项) in the accompanying answer's `## 资料来源`.
