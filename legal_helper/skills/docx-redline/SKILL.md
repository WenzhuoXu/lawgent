---
name: docx-redline
description: Deliver contract/document edits as native Word tracked changes (w:ins/w:del revision marks plus margin comments) instead of silent clean rewrites, and render PRC deliverables with CJK-native typography (宋体/仿宋-class body, 黑体 headings, GB/T 9704-style layout). Use whenever the deliverable is a .docx a counterparty or 合伙人 will review in Word.
argument-hint: "<source .docx> <edit list or review findings> [--author name] [--gbt9704]"
---

# /docx-redline — Tracked-Changes Redlining

**Not legal advice.** Every revision ships as an attributed, accept/reject-able
Word revision mark — never a silent replacement the reviewer cannot see.

## When to use

- The user asks for a 修订版 / redline / markup of a contract or memo.
- A review produced findings that must land *in the document itself*
  (replacement wording, deletions, margin comments), PRC deliverables first
  (合同, 法律意见书, 请示/报告), US/EU documents equally supported.
- Never hand back a clean-edited `.docx` when the user expects revisions:
  `edit_docx_text` rewrites silently; redlining is the reviewable deliverable.

## How

Call `legal_helper.documents.redline_docx_document(source, output, edits,
author=..., timestamp=...)`. Each edit:

- `{"op": "replace", "find": ..., "replace": ..., "comment": optional}`
- `{"op": "delete", "find": ..., "comment": optional}`
- `{"op": "insert_after", "find": ..., "text": ..., "comment": optional}`
- `{"op": "comment", "find": ..., "text": ...}` (margin note only)

`find` must sit inside one paragraph; check `not_found` in the result and
re-anchor any misses — never report success while `not_found` is non-empty.
Verify with `extract_docx_revisions` (each mark's author/date/text) and
`preview_docx_revisions(path, "accept"|"reject")`, which must round-trip to
the intended final and original texts respectively.

Read [references/redlining.md](references/redlining.md) for the OOXML
semantics (w:ins / w:del / w:delText, comment ranges), anchoring strategy,
and multi-edit ordering rules.

## CJK typography

`write_docx` output auto-detects Chinese and switches to CJK-native styles
(宋体-class body + 黑体-class headings via `w:eastAsia`, black headings,
首行缩进 2 字符, kinsoku punctuation rules). For 公文-style deliverables pass
`layout="gbt9704"` (仿宋 三号 body, GB margins, fixed line pitch) and follow
[resources/gbt9704_memo_template.md](resources/gbt9704_memo_template.md)
for the document skeleton. PDF and PPTX writers auto-detect too; do not
force Latin fonts onto Chinese deliverables.

## Output Contract (binding)

1. Deliverable: a `.docx` whose every change is a tracked revision
   (`w:ins`/`w:del`) attributed to the configured author, with margin
   comments for any change that needs a reason a reviewer must see.
2. Alongside the file, report: revisions applied (per-find counts), comments
   added, and any `not_found` anchors with the re-anchoring you attempted.
3. Chinese documents keep CJK-native typography; never emit a 法律意见书
   styled with Latin-only fonts.
4. Legal-answer prose accompanying the file still ends with `## Sources` /
   `## 资料来源` per the citation rule in /playbook/general_playbook.md — the
   redline does not replace it.
