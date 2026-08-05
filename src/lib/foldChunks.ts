/**
 * Folds a flat list of UIMessageChunks into an assistant message's `parts`
 * array — the same shape AI SDK builds internally. Used by the rehydration
 * builder so a reloaded turn produces the SAME parts the live stream does
 * (the live ≡ rehydrated invariant enforced in tests).
 */

import type { UIMessageChunk } from "@/api/sse";

export type MessagePart =
  | { type: "text"; text: string }
  | { type: "reasoning"; text: string }
  | {
      type: `tool-${string}`;
      toolCallId: string;
      state: "input-available" | "output-available" | "output-error";
      input?: unknown;
      output?: unknown;
      errorText?: string;
    }
  | { type: `data-${string}`; id?: string; data: unknown };

export function foldChunksToParts(chunks: UIMessageChunk[]): MessagePart[] {
  // Track parts by OBJECT REFERENCE, not index — references survive the
  // splice() used to drop cleared transient parts (a `data-*` with null data),
  // so a later text/tool delta never dereferences a stale index.
  const parts: MessagePart[] = [];
  const textById = new Map<string, Extract<MessagePart, { type: "text" }>>();
  const reasoningById = new Map<string, Extract<MessagePart, { type: "reasoning" }>>();
  const toolById = new Map<string, Extract<MessagePart, { type: `tool-${string}` }>>();
  const dataByKey = new Map<string, Extract<MessagePart, { type: `data-${string}` }>>();

  for (const c of chunks) {
    switch (c.type) {
      case "text-start": {
        const part: MessagePart = { type: "text", text: "" };
        parts.push(part);
        textById.set(c.id, part as Extract<MessagePart, { type: "text" }>);
        break;
      }
      case "text-delta": {
        const part = textById.get(c.id);
        if (part) part.text += c.delta;
        break;
      }
      case "reasoning-start": {
        const part: MessagePart = { type: "reasoning", text: "" };
        parts.push(part);
        reasoningById.set(c.id, part as Extract<MessagePart, { type: "reasoning" }>);
        break;
      }
      case "reasoning-delta": {
        const part = reasoningById.get(c.id);
        if (part) part.text += c.delta;
        break;
      }
      case "tool-input-available": {
        const part = {
          type: `tool-${c.toolName}` as `tool-${string}`,
          toolCallId: c.toolCallId,
          state: "input-available" as const,
          input: c.input,
        };
        parts.push(part);
        toolById.set(c.toolCallId, part);
        break;
      }
      case "tool-output-available": {
        const part = toolById.get(c.toolCallId);
        if (part) {
          part.state = "output-available";
          part.output = c.output;
        }
        break;
      }
      case "tool-output-error": {
        const part = toolById.get(c.toolCallId);
        if (part) {
          part.state = "output-error";
          part.errorText = c.errorText;
        }
        break;
      }
      default: {
        if (c.type.startsWith("data-")) {
          const dc = c as Extract<UIMessageChunk, { type: `data-${string}` }>;
          const dcType = dc.type as `data-${string}`;
          const key = `${dcType}:${dc.id ?? ""}`;
          const existing = dataByKey.get(key);
          if (dc.data === null) {
            // null = drop the transient part (e.g. citeCheck cleared on audit,
            // or the phase status cleared once the answer begins).
            if (existing) {
              const at = parts.indexOf(existing);
              if (at >= 0) parts.splice(at, 1);
              dataByKey.delete(key);
            }
            break;
          }
          if (existing) {
            existing.data = mergeData(existing.data, dc.data);
          } else {
            const part = { type: dcType, id: dc.id, data: dc.data };
            parts.push(part);
            dataByKey.set(key, part);
          }
        }
        // start / finish / error carry no part
      }
    }
  }
  return parts;
}

function mergeData(prev: unknown, next: unknown): unknown {
  if (prev && next && typeof prev === "object" && typeof next === "object") {
    // Skip `undefined` values in `next` so a partial incremental update
    // (e.g. a throttled agent_delta preview carrying only tail/total_chars)
    // never clobbers an already-accumulated field (toolCount, sources).
    // Parts signal "drop me" with `null` data, not per-key undefined.
    const merged: Record<string, unknown> = { ...(prev as Record<string, unknown>) };
    for (const [k, v] of Object.entries(next as Record<string, unknown>)) {
      if (v !== undefined) merged[k] = v;
    }
    return merged;
  }
  return next;
}
