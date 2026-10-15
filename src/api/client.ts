/** Typed fetch wrappers over the Legal Helper REST API. */

import type { ChatDetail, ChatSettings, ChatSummary, RuntimeOptions, UsageSummary } from "@/types/chat";

async function api<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(path, options);
  if (!resp.ok) throw new Error(await resp.text());
  if (resp.status === 204) return null as T;
  return (await resp.json()) as T;
}

function jsonBody(data: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(data),
  };
}

export const client = {
  runtimeOptions: () => api<RuntimeOptions>("/api/runtime-options"),
  usage: (month?: string) => api<UsageSummary>(`/api/usage${month ? `?month=${month}` : ""}`),

  listChats: async (): Promise<{ chats: ChatSummary[] }> => {
    const r = await api<ChatSummary[] | { chats: ChatSummary[] }>("/api/chats");
    return { chats: Array.isArray(r) ? r : (r.chats ?? []) };
  },
  createChat: (title?: string, settings?: ChatSettings) =>
    api<ChatSummary>("/api/chats", jsonBody({ title, settings })),
  getChat: (id: string) => api<ChatDetail>(`/api/chats/${id}`),
  patchChat: (id: string, patch: Partial<{ title: string; archived: boolean; settings: ChatSettings }>) =>
    api<ChatSummary>(`/api/chats/${id}`, { ...jsonBody(patch), method: "PATCH" }),
  deleteChat: (id: string) => api(`/api/chats/${id}`, { method: "DELETE" }),

  uploadToChat: async (chatId: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api<{ id: string; chat_id: string; filename: string; path: string }>(
      `/api/chats/${chatId}/upload`,
      { method: "POST", body: form },
    );
  },

  exportMessage: (
    chatId: string,
    messageId: string,
    format: "pdf" | "docx",
    watermark?: string,
  ) =>
    api<{ id: string; name: string; url: string }>(
      `/api/chats/${chatId}/messages/${messageId}/export`,
      jsonBody(watermark ? { format, watermark } : { format }),
    ),
};
