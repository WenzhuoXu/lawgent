import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  createTranslatorState,
  parseSSEBuffer,
  sseEventToChunks,
  type UIMessageChunk,
} from "../../../src/api/sse";

const here = new URL(".", import.meta.url).pathname;

function translateFixture(file: string): UIMessageChunk[] {
  const raw = readFileSync(join(here, file), "utf8");
  const state = createTranslatorState();
  const chunks: UIMessageChunk[] = [];
  // Feed the whole file through the frame parser, then translate each frame.
  parseSSEBuffer(raw.endsWith("\n\n") ? raw : raw + "\n\n", (name, data) => {
    chunks.push(...sseEventToChunks(state, name, data));
  });
  return chunks;
}

describe("sseEventToChunks — simple turn", () => {
  const chunks = translateFixture("simple.sse");

  it("opens with a start carrying the persisted assistant id", () => {
    expect(chunks[0]).toEqual({ type: "start", messageId: "b11d22181b6a4b3f" });
  });

  it("emits text-start → text-delta → text-end for the answer", () => {
    const types = chunks.map((c) => c.type);
    expect(types).toContain("text-start");
    expect(types).toContain("text-delta");
    expect(types.indexOf("text-start")).toBeLessThan(types.indexOf("text-delta"));
    expect(types.lastIndexOf("text-delta")).toBeLessThan(types.indexOf("text-end"));
    const delta = chunks.find((c) => c.type === "text-delta") as Extract<UIMessageChunk, { type: "text-delta" }>;
    expect(delta.delta).toContain("合同成立");
  });

  it("emits a live phase status early (before the answer) and clears it", () => {
    const types = chunks.map((c) => c.type);
    const firstPhase = chunks.findIndex((c) => c.type === "data-phase");
    const firstText = chunks.findIndex((c) => c.type === "text-delta");
    expect(firstPhase).toBeGreaterThanOrEqual(0);
    // status must appear before the answer text begins
    if (firstText >= 0) expect(firstPhase).toBeLessThan(firstText);
    // and it must be cleared (a data-phase with null data) by the end
    const cleared = chunks.some((c) => c.type === "data-phase" && (c as { data: unknown }).data === null);
    expect(cleared).toBe(true);
    expect(types).toContain("data-phase");
  });

  it("emits a data-plan part for the direct-answer route", () => {
    const plan = chunks.find((c) => c.type === "data-plan");
    expect(plan).toBeTruthy();
    expect((plan as any).data.executionMode).toBe("direct_answer");
    expect((plan as any).data.complexity).toBe("simple");
  });

  it("emits a skipped audit and finishes exactly once", () => {
    const audit = chunks.find((c) => c.type === "data-audit");
    expect((audit as any).data.skipped).toBe(true);
    const finishes = chunks.filter((c) => c.type === "finish");
    expect(finishes).toHaveLength(1);
    // `finish` must come after all text; trailing data parts (memory_updated is
    // persisted by the server AFTER `done`) may follow — that's expected.
    const finishIdx = chunks.findIndex((c) => c.type === "finish");
    expect(chunks.lastIndexOf(chunks.find((c) => c.type === "text-end")!)).toBeLessThan(finishIdx);
    const after = chunks.slice(finishIdx + 1).map((c) => c.type);
    expect(after.every((t) => t.startsWith("data-"))).toBe(true);
  });
});

describe("sseEventToChunks — complex turn (tools + hosted + plan)", () => {
  const chunks = translateFixture("complex.sse");

  it("produces tool-input-available chunks for local tools", () => {
    const inputs = chunks.filter((c) => c.type === "tool-input-available");
    expect(inputs.length).toBeGreaterThan(0);
    const readDoc = inputs.find((c) => (c as any).toolName === "read_document");
    expect(readDoc).toBeTruthy();
  });

  it("pairs each tool output with an open tool id (no orphan outputs)", () => {
    const openIds = new Set<string>();
    for (const c of chunks) {
      if (c.type === "tool-input-available") openIds.add(c.toolCallId);
      if (c.type === "tool-output-available" || c.type === "tool-output-error") {
        expect(openIds.has((c as any).toolCallId)).toBe(true);
      }
    }
  });

  it("dedupes hosted tool calls (one input+output pair per tool_id)", () => {
    const hostedInputs = chunks.filter(
      (c) => c.type === "tool-input-available" && c.toolCallId.startsWith("hosted:"),
    );
    const ids = hostedInputs.map((c) => (c as any).toolCallId);
    expect(new Set(ids).size).toBe(ids.length); // no duplicate hosted ids
  });

  it("emits data-packs for active_domain_packs", () => {
    const packs = chunks.find((c) => c.type === "data-packs");
    expect(packs).toBeTruthy();
  });
});

describe("sseEventToChunks — reasoning stream", () => {
  const chunks = translateFixture("reasoning.sse");

  it("folds reasoning_delta into one reasoning-start + many reasoning-delta", () => {
    const starts = chunks.filter((c) => c.type === "reasoning-start");
    const deltas = chunks.filter((c) => c.type === "reasoning-delta");
    expect(starts.length).toBe(1);
    expect(deltas.length).toBeGreaterThan(10);
  });
});
