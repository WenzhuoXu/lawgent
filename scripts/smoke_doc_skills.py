"""End-to-end smoke test for the new structural-edit tools.

Drives the Anthropic SDK with the same `@beta_tool`-decorated doc tools the
server exposes (orchestrator path) and asks the model to fix the level-shift
mistake in `附件4_手册地图模板_旅客服务_A类调整版.xlsx`.

What we expect to see in the trace:

1. The model calls `read_format_recipe("xlsx")` — disclosure works.
2. The model calls `inspect_xlsx_range` to ground its plan in real
   coordinates.
3. The model calls `reshape_xlsx` (not a stream of `edit_xlsx_cells`) —
   the structural-vs-scalar rule lands.
4. The model calls `diff_xlsx` and/or `render_xlsx_pages` to verify.

Run: ``conda run -n llm python scripts/smoke_doc_skills.py``
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Side-effect: loads `legal_helper/api_key` into os.environ.
from legal_helper.config import current_settings

current_settings()
assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY missing"

from anthropic import Anthropic  # noqa: E402

from legal_helper.tools import (  # noqa: E402
    copy_xlsx_sheet,
    diff_xlsx,
    edit_xlsx_cells_checked,
    inspect_xlsx,
    inspect_xlsx_range,
    list_format_recipes,
    read_document,
    read_format_recipe,
    render_xlsx_pages,
    reshape_xlsx,
)


SRC = Path(
    "outputs/chat_uploads/dfde076929c445fa/24a7c9e15d54/"
    "附件4_手册地图模板_旅客服务_-在线完成版.xlsx"
).resolve()
BAD = Path(
    "outputs/chat_artifacts/dfde076929c445fa/"
    "附件4_手册地图模板_旅客服务_A类调整版.xlsx"
).resolve()


SYSTEM_PROMPT = (
    "You are a document-tool agent. The user uploaded an Excel template that "
    "encodes a process hierarchy (一级流程 / 二级流程 / 三级流程 in cols A/B/C of "
    "sheet '" "A类" "'). A previous run produced a bad output: it tried to "
    "level-shift items from col A to col B using a stream of scalar cell "
    "edits, leaving empty gap rows and duplicate labels. Read the relevant "
    "format recipe before any edit. Pick the structurally-correct tool. "
    "Finish by diffing against the source template and reporting what "
    "changed."
)


USER_PROMPT = (
    "Source template: {src}\n"
    "Bad prior output (for reference only — do NOT edit this one): {bad}\n\n"
    "Task: produce a corrected version of {src} where ANY row whose level-1 "
    "(column A) cell holds a duplicate of an existing level-2 entry from "
    "rows 6-11 (服务战略管理 / 服务文化管理 / 服务品牌管理 / 服务标准管理 / 服务创新管理 / "
    "服务绩效管理) gets removed entirely. Those are dangling duplicates in the "
    "uploaded template. The corrected hierarchy should look like rows 5-13: "
    "旅客服务 at A5, the six L2 processes at B6-B11, with L3 children inline "
    "in col C. Preserve every style, merge, row height, and column width. "
    "Save the output as `附件4_smoke_test_corrected.xlsx`. After saving, "
    "diff it against the source over A1:K30 to prove the dangling rows are "
    "gone. Reply with a one-paragraph summary of what you did, the output "
    "path, and the diff_count."
).format(src=SRC, bad=BAD)


def _tool_payloads(tools):
    """Convert beta_tool wrappers into Anthropic Messages tool dicts."""
    out = []
    for t in tools:
        out.append(
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
        )
    return out


def _dispatch(tool_call_name, tool_input, tool_map):
    fn = tool_map[tool_call_name]
    return fn(**tool_input)


def run():
    tools = [
        list_format_recipes,
        read_format_recipe,
        read_document,
        inspect_xlsx,
        inspect_xlsx_range,
        edit_xlsx_cells_checked,
        reshape_xlsx,
        copy_xlsx_sheet,
        diff_xlsx,
        render_xlsx_pages,
    ]
    tool_map = {t.name: t for t in tools}
    payloads = _tool_payloads(tools)

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    messages = [{"role": "user", "content": USER_PROMPT}]
    trace_tool_calls: list[str] = []
    final_text = ""

    for turn in range(1, 16):
        print(f"\n=== Turn {turn} ===", flush=True)
        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=payloads,
            messages=messages,
        )
        print(f"  stop_reason={resp.stop_reason}", flush=True)
        assistant_blocks = []
        tool_calls_this_turn = []
        for block in resp.content:
            if block.type == "text":
                assistant_blocks.append({"type": "text", "text": block.text})
                if block.text.strip():
                    print(f"  TEXT: {block.text.strip()[:400]}", flush=True)
                    final_text = block.text
            elif block.type == "tool_use":
                assistant_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
                trace_tool_calls.append(block.name)
                snippet = json.dumps(block.input, ensure_ascii=False)[:300]
                print(f"  CALL: {block.name}({snippet})", flush=True)
                tool_calls_this_turn.append(block)

        messages.append({"role": "assistant", "content": assistant_blocks})

        if resp.stop_reason != "tool_use":
            break

        tool_results = []
        for tc in tool_calls_this_turn:
            try:
                result = _dispatch(tc.name, tc.input, tool_map)
            except Exception as exc:
                result = f"ERROR: {exc!r}\n{traceback.format_exc()[:1500]}"
            text_result = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            print(f"  RESULT[{tc.name}]: {text_result[:400]}", flush=True)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": text_result[:50_000],
                }
            )
        messages.append({"role": "user", "content": tool_results})

    print("\n=== Trace summary ===")
    print(f"  total_turns: {turn}")
    print(f"  tool_calls: {trace_tool_calls}")
    print(f"  recipe_consulted: {'read_format_recipe' in trace_tool_calls}")
    print(f"  used_reshape_xlsx: {'reshape_xlsx' in trace_tool_calls}")
    print(f"  used_inspect_xlsx_range: {'inspect_xlsx_range' in trace_tool_calls}")
    print(f"  used_diff_xlsx: {'diff_xlsx' in trace_tool_calls}")
    print(f"  final_text: {final_text[:600]}")
    return trace_tool_calls


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
