/**
 * Custom AI SDK ChatTransport that speaks the Legal Helper Python SSE protocol.
 * Translates our SSE events into UIMessageChunks via the pure translator in
 * api/sse.ts. Does NOT change the backend wire protocol.
 *
 * Body contract (verified against server.py): POST the LAST user message +
 * settings; the backend rebuilds history from SQLite, so we never resend the
 * whole transcript.
 */

import type { ChatTransport, UIMessage, UIMessageChunk } from "ai";
import {
  createTranslatorState,
  parseSSEBuffer,
  sseEventToChunks,
} from "@/api/sse";
import type { ChatSettings } from "@/types/chat";

type TransportDeps = {
  getSettings: () => ChatSettings;
  getSkillHint?: () => string | null;
  /** notified when the server assigns/echoes the persisted assistant message id */
  onServerAssistantId?: (chatId: string, id: string) => void;
  /** notified for chat-level side-channel events (chat_updated etc.) */
  onSideChannel?: (chatId: string, eventName: string, data: unknown) => void;
};

function extractText(message: UIMessage): string {
  return (message.parts ?? [])
    .filter((p) => p.type === "text")
    .map((p) => (p as { text: string }).text)
    .join("");
}

function extractAttachmentPaths(message: UIMessage): string[] {
  const paths: string[] = [];
  for (const p of message.parts ?? []) {
    // our composer stores the server upload path on a file/data-file part
    if (p.type === "file" && (p as any).url) paths.push((p as any).url);
    if (p.type === "data-file" && (p as any).data?.path) paths.push((p as any).data.path);
  }
  return paths;
}

export function createLegalHelperTransport(deps: TransportDeps): ChatTransport<UIMessage> {
  const streamTurn = async (
    chatId: string,
    body: Record<string, unknown>,
    abortSignal: AbortSignal | undefined,
  ): Promise<ReadableStream<UIMessageChunk>> => {
    const resp = await fetch(`/api/chats/${chatId}/messages/stream`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: abortSignal,
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`stream failed: ${resp.status} ${await resp.text().catch(() => "")}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    const state = createTranslatorState();

    // Cancel the server run when the client aborts (stop()).
    abortSignal?.addEventListener("abort", () => {
      reader.cancel().catch(() => {});
      fetch(`/api/chats/${chatId}/runs/cancel`, { method: "POST" }).catch(() => {});
    });

    // Eager pump: drive the reader in a loop from start() and enqueue chunks as
    // they arrive. This is the robust pattern — it does not depend on the
    // consumer pulling, which avoids any back-pressure/timing fragility.
    return new ReadableStream<UIMessageChunk>({
      async start(controller) {
        let buffer = "";
        const emit = (name: string, data: unknown) => {
          if (name === "message_saved") {
            const d = data as { role?: string; id?: string };
            if (d.role === "assistant" && d.id) deps.onServerAssistantId?.(chatId, d.id);
          }
          handleSideChannel(chatId, name, data, deps);
          for (const c of sseEventToChunks(state, name, data)) controller.enqueue(c);
        };
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            buffer = parseSSEBuffer(buffer, emit);
          }
          // flush any trailing frame
          parseSSEBuffer(buffer + "\n\n", emit);
          if (!state.finished) controller.enqueue({ type: "finish" });
          controller.close();
        } catch (err) {
          if ((err as Error)?.name === "AbortError") {
            controller.close();
            return;
          }
          controller.error(err);
        }
      },
      cancel() {
        reader.cancel().catch(() => {});
      },
    });
  };

  return {
    async sendMessages({ chatId, messages, abortSignal }) {
      const last = messages[messages.length - 1];
      const body = {
        message: last ? extractText(last) : "",
        attachments: last ? extractAttachmentPaths(last) : [],
        skill_hint: deps.getSkillHint?.() ?? null,
        settings: deps.getSettings(),
      };
      return streamTurn(chatId, body, abortSignal);
    },

    async reconnectToStream({ chatId }) {
      // Find an active run for this chat, then attach to the GET reconnect SSE.
      let runId: string | undefined;
      try {
        const detail = await fetch(`/api/chats/${chatId}`).then((r) => r.json());
        runId = detail?.active_runs?.[0]?.id;
      } catch {
        return null;
      }
      if (!runId) return null;
      const resp = await fetch(`/api/chats/${chatId}/runs/${runId}/stream`);
      if (!resp.ok || !resp.body) return null;

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      const state = createTranslatorState();
      return new ReadableStream<UIMessageChunk>({
        async start(controller) {
          let buffer = "";
          const emit = (name: string, data: unknown) => {
            for (const c of sseEventToChunks(state, name, data)) controller.enqueue(c);
          };
          try {
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              buffer = parseSSEBuffer(buffer, emit);
            }
            parseSSEBuffer(buffer + "\n\n", emit);
            if (!state.finished) controller.enqueue({ type: "finish" });
            controller.close();
          } catch (err) {
            controller.error(err);
          }
        },
        cancel() {
          reader.cancel().catch(() => {});
        },
      });
    },
  };
}

const SIDE_CHANNEL = new Set(["chat_updated", "memory_updated", "artifact"]);

function handleSideChannel(
  chatId: string,
  name: string,
  data: unknown,
  deps: TransportDeps,
) {
  if (SIDE_CHANNEL.has(name)) deps.onSideChannel?.(chatId, name, data);
}
