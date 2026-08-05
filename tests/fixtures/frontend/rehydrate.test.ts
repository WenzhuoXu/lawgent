import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  createTranslatorState,
  parseSSEBuffer,
  sseEventToChunks,
  type UIMessageChunk,
} from "../../../src/api/sse";
import { foldChunksToParts, type MessagePart } from "../../../src/lib/foldChunks";
import { eventsToUIMessages } from "../../../src/lib/eventsToUIMessages";

const here = new URL(".", import.meta.url).pathname;

function liveParts(sseFile: string): MessagePart[] {
  const raw = readFileSync(join(here, sseFile), "utf8");
  const state = createTranslatorState();
  const chunks: UIMessageChunk[] = [];
  parseSSEBuffer(raw.endsWith("\n\n") ? raw : raw + "\n\n", (name, data) => {
    chunks.push(...sseEventToChunks(state, name, data));
  });
  return foldChunksToParts(chunks);
}

function rehydratedAssistantParts(chatFile: string): MessagePart[] {
  const chat = JSON.parse(readFileSync(join(here, chatFile), "utf8"));
  const msgs = eventsToUIMessages(chat.messages, chat.events, chat.artifacts ?? []);
  const assistant = msgs.filter((m) => m.role === "assistant").at(-1);
  return assistant?.parts ?? [];
}

// Chat-level parts that are side-channels (not persisted as per-message
// WorkflowEvent rows), so they legitimately appear live but not on rehydrate.
// data-phase is a live-only status line (cleared before done); never persisted.
const CHAT_LEVEL = new Set(["data-memory", "data-packs", "data-phase"]);

/** Compare the *kinds and identities* of parts, ignoring streamed-text volatility. */
function partSignature(parts: MessagePart[]) {
  return parts
    .filter((p) => (p.type.startsWith("tool-") || p.type.startsWith("data-")) && !CHAT_LEVEL.has(p.type))
    .map((p) => {
      if (p.type.startsWith("tool-")) {
        const t = p as Extract<MessagePart, { type: `tool-${string}` }>;
        return `${p.type}#${t.state}`;
      }
      const d = p as Extract<MessagePart, { type: `data-${string}` }>;
      return `${p.type}#${d.id ?? ""}`;
    })
    .sort();
}

describe("eventsToUIMessages — simple turn rehydration", () => {
  it("produces one user + one assistant message", () => {
    const chat = JSON.parse(readFileSync(join(here, "simple.chat.json"), "utf8"));
    const msgs = eventsToUIMessages(chat.messages, chat.events, chat.artifacts ?? []);
    expect(msgs.filter((m) => m.role === "user")).toHaveLength(1);
    expect(msgs.filter((m) => m.role === "assistant")).toHaveLength(1);
  });

  it("assistant carries the final answer text", () => {
    const parts = rehydratedAssistantParts("simple.chat.json");
    const text = parts.find((p) => p.type === "text") as Extract<MessagePart, { type: "text" }>;
    expect(text.text).toContain("合同成立");
  });

  it("live ≡ rehydrated: same structural part signature", () => {
    expect(partSignature(rehydratedAssistantParts("simple.chat.json")))
      .toEqual(partSignature(liveParts("simple.sse")));
  });
});

describe("eventsToUIMessages — complex turn rehydration", () => {
  it("reconstructs tool parts with output states", () => {
    const parts = rehydratedAssistantParts("complex.chat.json");
    const tools = parts.filter((p) => p.type.startsWith("tool-"));
    expect(tools.length).toBeGreaterThan(0);
    // every reconstructed tool should have resolved to an output (persisted finished events)
    const resolved = tools.filter(
      (p) => (p as any).state === "output-available" || (p as any).state === "output-error",
    );
    expect(resolved.length).toBeGreaterThan(0);
  });

  it("reconstructs the plan + agent-task checklist", () => {
    const parts = rehydratedAssistantParts("complex.chat.json");
    expect(parts.some((p) => p.type === "data-plan")).toBe(true);
  });
});
