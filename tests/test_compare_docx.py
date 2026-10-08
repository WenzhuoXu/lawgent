"""compare_docx: two drafts into one document of native tracked changes (P2).

Driven through LibreOffice's own compare over UNO; skipped where LibreOffice
or a uno-capable Python is missing. The gate is the round-trip contract:
accept-all gives the revised text, reject-all the original.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docx_fixtures import build_docx, p
from legal_helper.documents.compare import uno_python
from legal_helper.documents.redline import extract_docx_revisions
from legal_helper.documents.soffice import soffice_binary

pytestmark = pytest.mark.skipif(
    not soffice_binary() or not uno_python(), reason="LibreOffice with a uno-capable Python is not available"
)


def _drafts(tmp_path: Path) -> tuple[Path, Path]:
    old = (
        p("第一条 本合同自双方签字之日起生效。")
        + p("Term:", runs='<w:r><w:tab/><w:t>three years from the Effective Date.</w:t></w:r>')
        + p("第二条 租金为每月人民币壹万元。")
    )
    new = (
        p("第一条 本合同自双方盖章之日起生效。")
        + p("Term:", runs='<w:r><w:tab/><w:t>five years from the Effective Date.</w:t></w:r>')
        + p("第二条 租金为每月人民币贰万元。")
        + p("第三条 新增条款。")
    )
    return build_docx(tmp_path / "v1.docx", old), build_docx(tmp_path / "v2.docx", new)


def test_compare_writes_tracked_changes_that_round_trip(tmp_path):
    from legal_helper.tools import compare_docx

    original, revised = _drafts(tmp_path)
    payload = json.loads(
        compare_docx.call(
            {
                "original_path": str(original),
                "revised_path": str(revised),
                "filename": "blackline.docx",
                "author": "Counsel (compare)",
            }
        )
    )
    assert payload["round_trip"] == {"accept_all_equals_revised": True, "reject_all_equals_original": True}
    assert payload["insertions"] >= 4 and payload["deletions"] >= 3
    assert payload["authors"] == ["Counsel (compare)"]
    revisions = extract_docx_revisions(Path(payload["output_path"]))
    # Han text is compared by character: the change is 签字 → 盖章, not the sentence.
    assert {"type": "deletion", "text": "签字"}.items() <= next(r for r in revisions if r["text"] == "签字").items()
    assert any(r["type"] == "insertion" and r["text"] == "盖章" for r in revisions)


def test_identical_drafts_report_no_differences(tmp_path):
    from legal_helper.documents.compare import compare_docx_documents

    original, _ = _drafts(tmp_path)
    payload = compare_docx_documents(original, original, tmp_path / "same.docx", author="x")
    assert payload["insertions"] == 0 and payload["deletions"] == 0
    assert any("identical" in note for note in payload["notes"])
