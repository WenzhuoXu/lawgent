/**
 * Multi-chat registry. Holds one framework-agnostic `Chat` instance per opened
 * chat in a ref map; the viewed chat binds to the view via useChat({ chat }).
 * Background chats keep their instance alive and continue streaming off-screen
 * (the transport ReadableStream is still consumed by that instance's loop),
 * replicating the old busyChatIds / isViewingChat behavior for free.
 */

import { useCallback, useRef, useState } from "react";
import { Chat, useChat } from "@ai-sdk/react";
import type { UIMessage } from "ai";
import { client } from "@/api/client";
import { createLegalHelperTransport } from "@/api/transport";
import { eventsToUIMessages } from "@/lib/eventsToUIMessages";
import type { ChatSettings } from "@/types/chat";

type Deps = {
  getSettings: () => ChatSettings;
  getSkillHint: () => string | null;
  onSideChannel: (chatId: string, name: string, data: unknown) => void;
};

export function useChatSessions(deps: Deps) {
  const depsRef = useRef(deps);
  depsRef.current = deps;

  const transportRef = useRef(
    createLegalHelperTransport({
      getSettings: () => depsRef.current.getSettings(),
      getSkillHint: () => depsRef.current.getSkillHint(),
      onServerAssistantId: () => {},
      onSideChannel: (chatId, name, data) => depsRef.current.onSideChannel(chatId, name, data),
    }),
  );

  const registry = useRef<Map<string, Chat<UIMessage>>>(new Map());
  const [activeId, setActiveId] = useState<string | null>(null);
  // bump to force a re-render when a background chat's busy status changes
  const [, forceTick] = useState(0);

  const getOrCreate = useCallback((id: string, messages: UIMessage[] = []) => {
    let chat = registry.current.get(id);
    if (!chat) {
      chat = new Chat<UIMessage>({
        id,
        messages,
        transport: transportRef.current,
      });
      registry.current.set(id, chat);
    }
    return chat;
  }, []);

  /** Open a chat: load persisted history into a Chat instance, THEN view it.
   *
   * Order matters: we must hydrate the instance with messages BEFORE it becomes
   * the active/viewed chat, otherwise the render that follows setActiveId would
   * create an empty instance first and the loaded messages would be dropped. */
  const open = useCallback(
    async (id: string) => {
      if (!registry.current.has(id)) {
        try {
          const detail = await client.getChat(id);
          const msgs = eventsToUIMessages(detail.messages, detail.events, detail.artifacts);
          registry.current.set(
            id,
            new Chat<UIMessage>({ id, messages: msgs as unknown as UIMessage[], transport: transportRef.current }),
          );
        } catch {
          // fall back to an empty instance so the chat still opens
          getOrCreate(id, []);
        }
      }
      setActiveId(id);
      forceTick((n) => n + 1);
    },
    [getOrCreate],
  );

  /** Create a brand-new chat, register an empty instance, and view it. */
  const create = useCallback(async (): Promise<string> => {
    const created = await client.createChat(undefined, depsRef.current.getSettings());
    getOrCreate(created.id, []);
    setActiveId(created.id);
    return created.id;
  }, [getOrCreate]);

  const activeChat = activeId ? registry.current.get(activeId) ?? getOrCreate(activeId) : undefined;

  return {
    activeId,
    setActiveId,
    activeChat,
    open,
    create,
    getOrCreate,
    registry,
    /** true if ANY registered chat is currently streaming */
    anyBusy: () =>
      [...registry.current.values()].some(
        (c) => c.status === "streaming" || c.status === "submitted",
      ),
  };
}

/** Bind the currently-viewed Chat instance into React. */
export function useActiveChat(chat: Chat<UIMessage> | undefined) {
  // useChat requires a chat; when none is active we pass a throwaway.
  const fallback = useRef<Chat<UIMessage> | null>(null);
  if (!fallback.current) fallback.current = new Chat<UIMessage>({ id: "__idle__", messages: [] });
  // resume: on (re)bind, if the server reports an active run for this chat, the
  // transport's reconnectToStream re-attaches and replays it — this replaces
  // the old 1.5s polling fallback.
  // throttle: coalesce React re-renders to ~30ms (~33fps). The backend emits
  // every token/tool/agent delta immediately (server.py:987), so without this
  // useChat's default batching makes updates arrive in visible chunks. A small
  // throttle yields a smooth, steady reveal without flooding React.
  return useChat<UIMessage>({ chat: chat ?? fallback.current, resume: true, throttle: 30 });
}
