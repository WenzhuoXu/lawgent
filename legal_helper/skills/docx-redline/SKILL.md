---
name: docx-redline
description: Deliver contract/document edits as native Word tracked changes (修订模式 — w:ins/w:del revision marks plus margin comments) instead of silent clean rewrites, and compare two drafts into a blackline (比对稿). Use whenever the deliverable is a .docx a counterparty or 合伙人 will review in Word, or the user supplies a document carrying someone else's tracked changes.
argument-hint: "<source .docx> <edit list or review findings> [--author name]"
---

# /docx-redline — Tracked-Changes Redlining

**Not legal advice.** Every revision ships as an attributed, accept/reject-able
Word revision mark — never a silent replacement the reviewer cannot see.

## When to use

- The user asks for a 修订版 / 修订模式 / redline / markup of a contract or memo.
- A review produced findings that must land *in the document itself*
  (replacement wording, deletions, margin comments), PRC deliverables first
  (合同, 法律意见书, 请示/报告), US/EU documents equally supported.
- The user has two versions and wants the differences shown (比对稿).
- Never hand back a clean-edited `.docx` when the user expects revisions:
  `edit_docx_text` rewrites silently; `redline_docx` is the reviewable deliverable.

## How

1. Read first. `read_document(path)` shows existing tracked changes as
   `{++inserted++}` / `{--deleted--}` under a summary line — those are a
   counterparty's proposals, not agreed text. `inspect_docx(path)` lists every
   body paragraph with the `index` edits take; `read_document(path,
   section="第三条")` returns one clause.
2. Call `redline_docx(source_path, filename, changes, author=…)`. Each change:
   - `{"op": "replace", "find": …, "replace": …, "comment": optional}`
   - `{"op": "delete", "find": …}` — or no `find` to delete a whole
     `paragraph` / `clause`
   - `{"op": "insert_after" | "insert_before", "find": …, "text": …}` — or no
     `find` to insert new paragraph(s) after/before a `paragraph` / `clause`
   - `{"op": "comment", "find": …, "text": …}` (margin note only)
   - scope with `"clause": "第三条"` (label as the document shows it; 第3条
     works too) or `"paragraph": <inspect_docx index>`; `"occurrence": n`
     picks one match.
   - attribution: `category` ("substantive" / "drafting") with
     `category_authors`, so the reviewer can filter each kind in Word.
3. Check the result: every entry in `not_found` has a `reason` and a `hint`
   (e.g. `label_is_auto_numbering`: 第一条 is Word numbering, not text — scope
   by clause and find only the words). Re-anchor and run again on the output;
   never report a change as made while it sits in `not_found`. Read
   `integrity`: it lists anything else the edit lost.
4. Two drafts → `compare_docx(original_path, revised_path, filename)`; its
   `round_trip` confirms accept-all = revised and reject-all = original.

Read [references/redlining.md](references/redlining.md) for the OOXML
semantics (w:ins / w:del / w:delText, paragraph marks, comment ranges),
anchoring rules, and the round-trip contract.

## CJK typography

The redline never touches style parts, so a 宋体/仿宋 document stays
宋体/仿宋 and inserted text copies the formatting of the run it replaces.
For a new PRC deliverable, `write_docx` detects Chinese and applies CJK-native
styles; follow [resources/gbt9704_memo_template.md](resources/gbt9704_memo_template.md)
for the 法律意见书 / 请示 skeleton.

## Output Contract (binding)

As a specialist, emit the harness's bundle contract (Findings · Issues ·
Artefacts · Out of scope · Sources), supplied with your task, and report the
deliverable below in it.

## Deliverable (binding)

1. A `.docx` whose every change is a tracked revision
   (`w:ins`/`w:del`) attributed to the configured author(s), with margin
   comments for any change that needs a reason a reviewer must see. Track
   Changes is left on in the output.
2. Alongside the file, report: revisions applied (per-change counts),
   comments added, any `not_found` changes with the re-anchoring you
   attempted, and any `integrity` findings.
3. A protected document (`w:documentProtection` other than tracked-changes
   only) is refused — say so and ask for an editable copy; do not work around it.
4. Chinese documents keep CJK-native typography; never emit a 法律意见书
   styled with Latin-only fonts.
5. Legal-answer prose accompanying the file still ends with `## Sources` /
   `## 资料来源` per the citation rule in /playbook/general_playbook.md — the
   redline does not replace it.
