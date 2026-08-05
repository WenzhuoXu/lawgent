/**
 * Pure rehydration: GET /api/chats/{id} → { messages, events, artifacts } into
 * AI SDK UIMessage[] with parts. Reuses the SAME translator + folder as the
 * live stream (api/sse.ts + foldChunks) so a reloaded turn is byte-identical to
 * what streaming produced — the live ≡ rehydrated invariant (enforced in tests).
 *
 * Attribution uses timestamp bucketing (Finding B): persisted WorkflowEvent
 * run_id is unreliable, but runs are strictly sequential per chat, so every
 * event belongs to the currently-open assistant message (the one following the
 * most recent user message). created_at is ASC from the server.
 */

import {
  createTranslatorState,
  sseEventToChunks,
  type UIMessageChunk,
} from "@/api/sse";
import { foldChunksToParts, type MessagePart } from "@/lib/foldChunks";
import type { LegalMessageMetadata } from "@/lib/dataParts";
import { attachmentPartsFromMetadata } from "@/lib/attachments";

export type ChatMessageRow = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
  metadata?: Record<string, unknown>;
};

export type WorkflowEventRow = {
  id: string;
  event_type: string;
  agent_name?: string;
  run_id?: string;
  created_at: string;
  data?: Record<string, unknown>;
};

export type ArtifactRow = {
  id: string;
  filename: string;
  url?: string;
  origin?: string;
  message_id?: string;
};

export type LegalUIMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  parts: MessagePart[];
  metadata?: LegalMessageMetadata;
};

/**
 * Replay a persisted event row through the live translator. A persisted row is
 * shaped like the SSE frame's data for a `workflow_event` (has event_type +
 * nested data) OR like a direct event. We pass (event_type, row.data) and let
 * normalizeEvent() unwrap — but for direct-persisted events (tool_call,
 * workflow_plan, citation_audit) the payload is stored flat in `data`, so we
 * pass the row itself shaped as a frame.
 */
function replayEvent(
  state: ReturnType<typeof createTranslatorState>,
  row: WorkflowEventRow,
): UIMessageChunk[] {
  const type = row.event_type;
  const data = row.data ?? {};
  // If the row still carries the nested envelope (data.event_type present),
  // feed it as a workflow_event frame; else feed as a direct (type, data) frame.
  if ((data as Record<string, unknown>).event_type && (data as Record<string, unknown>).data) {
    return sseEventToChunks(state, "workflow_event", { event_type: type, ...data });
  }
  return sseEventToChunks(state, type, data);
}

export function eventsToUIMessages(
  messages: ChatMessageRow[],
  events: WorkflowEventRow[],
  artifacts: ArtifactRow[] = [],
): LegalUIMessage[] {
  const sortedMsgs = [...messages].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const sortedEvents = [...events].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const artifactsByMessage = groupArtifacts(artifacts);

  // Build ordered [assistantMessage, windowStart, windowEnd) buckets.
  const assistantMsgs = sortedMsgs.filter((m) => m.role === "assistant");
  const result: LegalUIMessage[] = [];

  for (const msg of sortedMsgs) {
    if (msg.role === "user") {
      result.push({
        id: msg.id,
        role: "user",
        parts: [
          { type: "text", text: msg.content },
          ...attachmentPartsFromMetadata(msg.metadata),
        ],
      });
      continue;
    }
    if (msg.role === "system") {
      result.push({ id: msg.id, role: "system", parts: [{ type: "text", text: msg.content }] });
      continue;
    }

    // assistant: gather its event window.
    const myIdx = assistantMsgs.indexOf(msg);
    const nextAssistant = assistantMsgs[myIdx + 1];
    const windowEnd = nextAssistant ? nextAssistant.created_at : "￿";
    // window starts at this assistant message's own created_at (events are
    // logged during/after it) — but plan/route events can precede the assistant
    // row by milliseconds, so start from the preceding user message instead.
    const precedingUser = [...sortedMsgs]
      .filter((m) => m.role === "user" && m.created_at <= msg.created_at)
      .at(-1);
    const windowStart = precedingUser ? precedingUser.created_at : "";

    const windowEvents = sortedEvents.filter(
      (e) => e.created_at >= windowStart && e.created_at < windowEnd,
    );

    const state = createTranslatorState();
    state.assistantId = msg.id;
    state.sawStart = true; // rehydrated: no need to emit a start
    const chunks: UIMessageChunk[] = [];
    for (const e of windowEvents) chunks.push(...replayEvent(state, e));
    const parts = foldChunksToParts(chunks);

    // The persisted final answer text is authoritative — deltas are not stored
    // individually, so replayed events yield no text part. Inject it.
    if (msg.content && !parts.some((p) => p.type === "text")) {
      // place text after reasoning/plan but before/with tools is fine; append.
      parts.push({ type: "text", text: msg.content });
    } else if (msg.content) {
      const textPart = parts.find((p) => p.type === "text") as
        | Extract<MessagePart, { type: "text" }>
        | undefined;
      if (textPart && !textPart.text) textPart.text = msg.content;
    }

    // Reasoning is transient; reconstruct a collapsed stub from the enriched
    // provider_turn_finished if present (post backend-fix).
    const meta = msg.metadata ?? {};

    // Attach artifacts belonging to this message.
    for (const a of artifactsByMessage.get(msg.id) ?? []) {
      parts.push({
        type: "data-artifact",
        id: a.id,
        data: { id: a.id, filename: a.filename, url: a.url, origin: a.origin, messageId: a.message_id },
      });
    }

    result.push({
      id: msg.id,
      role: "assistant",
      parts,
      metadata: {
        runId: meta.run_id as string | undefined,
        provider: meta.provider as string | undefined,
        model: meta.model as string | undefined,
        createdAt: msg.created_at,
        durationMs: meta.duration_ms as number | undefined,
      },
    });
  }

  return result;
}

function groupArtifacts(artifacts: ArtifactRow[]): Map<string, ArtifactRow[]> {
  const m = new Map<string, ArtifactRow[]>();
  for (const a of artifacts) {
    const key = a.message_id ?? "";
    if (!key) continue;
    const arr = m.get(key) ?? [];
    arr.push(a);
    m.set(key, arr);
  }
  return m;
}
