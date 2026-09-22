import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { UIMessage } from "ai";
import { Info, Loader2, Menu, MessageSquarePlus, Send, Square } from "lucide-react";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
} from "@/components/ai-elements/conversation";
import { Message, MessageContent } from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
} from "@/components/ai-elements/prompt-input";
import { RenderMessageParts } from "@/components/parts/renderMessageParts";
import { MessageActions } from "@/components/parts/MessageActions";
import { AuditItemRows, SourceRows } from "@/components/parts/CitationRows";
import { splitSources, parseSourcesTable } from "@/components/parts/SourcesList";
import { rewriteSandboxHref } from "@/lib/artifacts";
import { Sidebar } from "@/components/layout/Sidebar";
import { Composer } from "@/components/controls/Composer";
import { SettingsDialog } from "@/components/controls/SettingsDialog";
import { client } from "@/api/client";
import { useChatSessions, useActiveChat } from "@/hooks/useChatSessions";
import { displayToolName, isSourceRetrievalTool } from "@/lib/toolLabels";
import type { MessagePart } from "@/lib/foldChunks";
import type { AuditData, UsageData } from "@/lib/dataParts";
import { formatDuration } from "@/lib/utils";
import { useTheme } from "@/hooks/useTheme";
import { personaCopy, DISCLAIMER, TASK_CHIPS } from "@/persona/persona";
import type { ChatSettings, ChatSummary, RuntimeOptions } from "@/types/chat";

const DEFAULT_SETTINGS: ChatSettings = {
  provider: "openai",
  model: "gpt-6-sol",
  reasoning_effort: "medium",
  enable_web_search: true,
  enable_web_fetch: true,
};

export default function App() {
  const { theme, setTheme, persona, setPersona } = useTheme();
  const [runtime, setRuntime] = useState<RuntimeOptions | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [settings, setSettings] = useState<ChatSettings>(DEFAULT_SETTINGS);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Composer prefill seed (n forces re-apply even for the same text).
  const [prefill, setPrefill] = useState<{ text: string; n: number }>({ text: "", n: 0 });
  const settingsRef = useRef(settings);
  settingsRef.current = settings;

  const copy = useMemo(() => personaCopy(persona), [persona]);

  const sessions = useChatSessions({
    getSettings: () => settingsRef.current,
    getSkillHint: () => settingsRef.current.skill_hint ?? null,
    onSideChannel: (_chatId, name) => {
      if (name === "chat_updated") client.listChats().then((r) => setChats(r.chats ?? []));
    },
  });
  const { activeId } = sessions;
  const chat = useActiveChat(sessions.activeChat);

  // Bootstrap runtime options + chat list.
  useEffect(() => {
    client.runtimeOptions().then((r) => {
      setRuntime(r);
      // Backend defaults (e.g. gpt-6-sol) win over the hardcoded frontend
      // fallback; this runs once on mount before any user change.
      setSettings((s) => ({ ...DEFAULT_SETTINGS, ...s, ...r.defaults }));
    });
    client.listChats().then((r) => setChats(r.chats ?? []));
  }, []);

  // When the viewed chat finishes streaming, refresh the sidebar so the
  // server-assigned title (and any newly created chat) appears.
  const prevStatusRef = useRef<string>("");
  useEffect(() => {
    if (prevStatusRef.current && prevStatusRef.current !== "ready" && chat.status === "ready") {
      client.listChats().then((r) => setChats(r.chats ?? []));
    }
    prevStatusRef.current = chat.status;
  }, [chat.status]);

  const openChat = useCallback(
    async (id: string) => {
      setDrawerOpen(false);
      const detail = await client.getChat(id).catch(() => null);
      if (detail?.chat.settings) setSettings((s) => ({ ...s, ...detail.chat.settings }));
      await sessions.open(id);
    },
    [sessions],
  );

  const newChat = useCallback(async () => {
    const id = await sessions.create();
    setChats((c) => [{ id, title: "新对话", created_at: "", updated_at: "" }, ...c.filter((x) => x.id !== id)]);
    setDrawerOpen(false);
  }, [sessions]);

  const busy = chat.status === "streaming" || chat.status === "submitted";

  const handleSend = useCallback(
    async (text: string, attachmentPaths: string[]) => {
      // Registering the Chat instance is synchronous inside create(), so the
      // viewed instance is already correct before we sendMessage — no race.
      let target = sessions.activeChat;
      if (!target) {
        const id = await sessions.create();
        setChats((c) => [{ id, title: text.slice(0, 40), created_at: "", updated_at: "" }, ...c]);
        target = sessions.getOrCreate(id);
      }
      const parts: MessagePart[] = [
        { type: "text", text },
        ...attachmentPaths.map((p) => ({ type: "data-file", id: p, data: { path: p } }) as MessagePart),
      ];
      target.sendMessage({ role: "user", parts } as unknown as UIMessage);
    },
    [sessions],
  );

  const activeChat = chats.find((c) => c.id === activeId) ?? null;
  const hasMessages = chat.messages.length > 0;

  const lastAssistant = [...chat.messages].reverse().find((m) => m.role === "assistant");

  // Sidebar Context ring: fill = last turn's prompt tokens over the current
  // model's context window (percent + ring only, no cost — per decision).
  const usage = (
    (lastAssistant?.parts as unknown as MessagePart[] | undefined)?.find(
      (p) => p.type === "data-usage",
    ) as { data: UsageData } | undefined
  )?.data;
  const maxTokens =
    runtime?.context_windows?.[settings.model] ?? runtime?.default_context_window ?? 0;
  const usedTokens = usage?.contextTokens ?? 0;

  return (
    <div
      className={
        inspectorOpen
          ? "grid h-dvh grid-cols-1 md:grid-cols-[288px_minmax(0,1fr)_360px]"
          : "grid h-dvh grid-cols-1 md:grid-cols-[288px_minmax(0,1fr)]"
      }
    >
      <Sidebar
        chats={chats}
        activeId={activeId}
        onOpen={openChat}
        onNew={newChat}
        onRename={(id, title) => client.patchChat(id, { title }).then(() => client.listChats().then((r) => setChats(r.chats ?? [])))}
        onDelete={(id) => client.deleteChat(id).then(() => setChats((c) => c.filter((x) => x.id !== id)))}
        copy={copy}
        onOpenSettings={() => setSettingsOpen(true)}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        usedTokens={usedTokens}
        maxTokens={maxTokens}
      />

      <main className="flex h-dvh min-h-0 min-w-0 flex-col overflow-hidden bg-background">
        <header className="flex h-[52px] items-center gap-3 border-b px-4">
          <button className="md:hidden" onClick={() => setDrawerOpen(true)} aria-label="Open chats">
            <Menu className="size-5" />
          </button>
          <span className="truncate font-semibold text-sm">{activeChat?.title ?? "新对话"}</span>
          <span className="ml-auto rounded-full border px-2.5 py-1 text-xs text-muted-foreground">
            ⛨ {DISCLAIMER}
          </span>
          <button
            className={`rounded-md p-1.5 transition-colors ${inspectorOpen ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent"}`}
            onClick={() => setInspectorOpen((v) => !v)}
            aria-label="运行详情 / Run details"
            title="运行详情 / Run details"
          >
            <Info className="size-4" />
          </button>
        </header>

        <Conversation className="min-h-0 flex-1">
          <ConversationContent className="mx-auto w-full max-w-3xl px-4 py-6">
            {!hasMessages ? (
              <WelcomeScreen copy={copy} onPick={(prompt, skill) => {
                // Prefill the composer (do NOT auto-send) — the chips are
                // inspiration; the user edits before sending.
                if (skill) setSettings((s) => ({ ...s, skill_hint: skill }));
                setPrefill({ text: prompt, n: prefill.n + 1 });
              }} />
            ) : (
              chat.messages.map((m, idx) => {
                const isLast = idx === chat.messages.length - 1;
                return (
                  <Message key={m.id} from={m.role}>
                    <MessageContent>
                      {m.role === "assistant" ? (
                        <>
                          <RenderMessageParts
                            parts={m.parts as unknown as MessagePart[]}
                            isStreaming={busy && isLast}
                          />
                          {!busy || !isLast ? (
                            <MessageActions
                              chatId={activeId}
                              messageId={m.id}
                              parts={m.parts as unknown as MessagePart[]}
                              onRegenerate={isLast ? () => chat.regenerate() : undefined}
                            />
                          ) : null}
                        </>
                      ) : (
                        <UserBubble parts={m.parts as unknown as MessagePart[]} />
                      )}
                    </MessageContent>
                  </Message>
                );
              })
            )}
            {/* Pre-first-event placeholder: the gap between send and the first
                backend event (a full planning LLM call). Without this the user
                stares at nothing after sending. */}
            {busy && chat.messages[chat.messages.length - 1]?.role === "user" ? (
              <Message from="assistant">
                <MessageContent>
                  <div className="flex items-center gap-2 rounded-lg bg-tool-surface px-3 py-2 font-mono text-xs text-content-secondary">
                    <Loader2 className="size-3.5 animate-spin text-primary" />
                    <span>连接中，正在分析问题… / Connecting…</span>
                  </div>
                </MessageContent>
              </Message>
            ) : null}
          </ConversationContent>
        </Conversation>

        <div className="mx-auto w-full max-w-3xl shrink-0 px-4 pb-4">
          <Composer
            runtime={runtime}
            settings={settings}
            setSettings={setSettings}
            busy={busy}
            onSend={handleSend}
            onStop={() => chat.stop()}
            placeholder={copy.placeholder}
            chatId={activeId}
            prefill={prefill}
          />
        </div>
      </main>

      {inspectorOpen && (
        <Inspector
          parts={(lastAssistant?.parts as unknown as MessagePart[]) ?? []}
          onClose={() => setInspectorOpen(false)}
        />
      )}

      {drawerOpen && (
        <div className="fixed inset-0 z-20 bg-black/40 md:hidden" onClick={() => setDrawerOpen(false)} />
      )}

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        runtime={runtime}
        settings={settings}
        setSettings={setSettings}
        theme={theme}
        setTheme={setTheme}
        persona={persona}
        setPersona={setPersona}
      />
    </div>
  );
}

type InspectorTab = "run" | "sources" | "files";

const INSPECTOR_TABS: { id: InspectorTab; label: string }[] = [
  { id: "run", label: "运行 Run" },
  { id: "sources", label: "来源 Sources" },
  { id: "files", label: "文件 Files" },
];

function Inspector({ parts, onClose }: { parts: MessagePart[]; onClose: () => void }) {
  const [tab, setTab] = useState<InspectorTab>("run");

  const tools = parts.filter((p) => p.type.startsWith("tool-")) as Array<{
    type: string;
    state: string;
    input?: unknown;
    output?: unknown;
  }>;
  const sourceTools = parts.filter(
    (p) => p.type.startsWith("tool-") && isSourceRetrievalTool(p.type.replace(/^tool-/, "")),
  );
  const artifacts = parts.filter((p) => p.type === "data-artifact");

  const usage = (parts.find((p) => p.type === "data-usage") as { data: UsageData } | undefined)?.data;
  const audit = (parts.find((p) => p.type === "data-audit") as { data: AuditData } | undefined)?.data;
  const auditItems = audit?.items ?? [];

  // Parsed prose sources (资料来源 table) — the full citation list for 来源 tab.
  const answerText = parts
    .filter((p) => p.type === "text")
    .map((p) => (p as { text: string }).text)
    .join("");
  const proseSources = (() => {
    const { sources } = splitSources(answerText);
    return sources ? parseSourcesTable(sources) : null;
  })();

  const turnMeta = [
    usage?.durationMs != null ? `用时 ${formatDuration(usage.durationMs)}` : null,
    usage?.model ?? null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <aside className="hidden h-dvh min-h-0 min-w-0 flex-col overflow-hidden border-l bg-panel md:flex">
      <div className="flex h-10 shrink-0 items-center border-b px-2">
        {INSPECTOR_TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`relative flex h-full items-center px-3.5 text-[13px] transition-colors ${
              tab === t.id
                ? "font-semibold text-foreground after:absolute after:inset-x-2.5 after:-bottom-px after:h-0.5 after:bg-primary"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {t.label}
          </button>
        ))}
        <button
          className="ml-auto self-center pr-2 text-muted-foreground hover:text-foreground"
          onClick={onClose}
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4 text-sm">
        {turnMeta ? <div className="font-mono text-[11.5px] text-content-faint">{turnMeta}</div> : null}

        {tab === "run" ? (
          <>
            <div className="grid grid-cols-3 gap-2">
              <Stat label="工具调用" value={tools.length} />
              <Stat label="来源" value={sourceTools.length} />
              <Stat label="文件" value={artifacts.length} />
            </div>
            <div>
              <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                工具轨迹 / Tool trace
              </div>
              {tools.length === 0 ? (
                <p className="text-xs text-content-faint">本轮暂无工具调用 / No tool calls this turn</p>
              ) : (
                <ul className="flex flex-col gap-1.5">
                  {tools.map((t, i) => (
                    <li
                      key={i}
                      className="flex items-center gap-2 rounded-md bg-tool-surface px-2.5 py-1.5 font-mono text-xs text-content-secondary"
                    >
                      <span
                        className={
                          t.state === "output-available"
                            ? "text-[var(--ok)]"
                            : t.state === "output-error"
                              ? "text-[var(--dnf)]"
                              : "text-primary"
                        }
                      >
                        {t.state === "output-available" ? "✓" : t.state === "output-error" ? "✕" : "◐"}
                      </span>
                      {displayToolNameForType(t.type)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        ) : null}

        {tab === "sources" ? (
          auditItems.length || proseSources?.length ? (
            <div className="flex flex-col gap-3">
              {auditItems.length ? (
                <div>
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    核验发现 / Flagged
                  </div>
                  <AuditItemRows items={auditItems} />
                </div>
              ) : null}
              {proseSources?.length ? (
                <div>
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    引证 / Citations
                  </div>
                  <SourceRows rows={proseSources} />
                </div>
              ) : null}
            </div>
          ) : (
            <p className="text-xs text-content-faint">本轮暂无引证 / No citations this turn</p>
          )
        ) : null}

        {tab === "files" ? (
          artifacts.length ? (
            <ul className="flex flex-col gap-1.5">
              {artifacts.map((p, i) => {
                const a = (p as { data: { filename: string; url: string } }).data;
                return (
                  <li key={i}>
                    <a
                      href={rewriteSandboxHref(a.url)}
                      download
                      className="text-xs text-primary hover:underline"
                    >
                      {a.filename}
                    </a>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-xs text-content-faint">本轮暂无生成文件 / No files this turn</p>
          )
        ) : null}
      </div>
    </aside>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border bg-card p-2.5">
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}

function displayToolNameForType(type: string): string {
  return displayToolName(type.replace(/^tool-/, ""), "complete");
}

function UserBubble({ parts }: { parts: MessagePart[] }) {
  const text = parts.filter((p) => p.type === "text").map((p) => (p as { text: string }).text).join("");
  const files = parts.filter((p) => p.type === "data-file") as Array<{ data: { path: string } }>;
  return (
    <div>
      <div className="whitespace-pre-wrap">{text}</div>
      {files.map((f, i) => (
        <span key={i} className="mt-1 inline-block rounded-md bg-accent px-2 py-0.5 text-xs text-muted-foreground">
          📎 {f.data.path.split("/").pop()}
        </span>
      ))}
    </div>
  );
}

function WelcomeScreen({
  copy,
  onPick,
}: {
  copy: ReturnType<typeof personaCopy>;
  /** Prefill the composer (does NOT auto-send) so the user can edit first. */
  onPick: (prompt: string, skill?: string) => void;
}) {
  return (
    <div className="mx-auto mt-16 max-w-2xl text-center">
      <div className="mx-auto mb-4 flex size-11 items-center justify-center rounded-xl bg-primary text-xl text-primary-foreground">
        {copy.avatar}
      </div>
      <h2 className="font-serif text-2xl font-semibold">{copy.welcomeTitle}</h2>
      <p className="mb-1.5 mt-1.5 text-xs text-muted-foreground">{copy.welcomeCaption}</p>
      <div className="mb-5 flex items-center justify-center gap-2 text-[13px] text-content-secondary">
        <span className="inline-block h-px w-6 bg-border" />
        今日灵感 · 可从这些开始，但不止于此 / Inspiration — start here, not limited to
        <span className="inline-block h-px w-6 bg-border" />
      </div>
      <div className="grid grid-cols-1 gap-3 text-left sm:grid-cols-2">
        {TASK_CHIPS.map((c) => (
          <button
            key={c.title}
            onClick={() => onPick(c.prompt, c.skill)}
            className="group rounded-2xl border bg-panel p-4 text-left transition-colors hover:border-primary/50"
            title="点按填入输入框，可在发送前编辑"
          >
            <div className="text-lg">{c.icon}</div>
            <div className="mt-1 font-semibold text-sm">{c.title}</div>
            <div className="mt-0.5 text-xs text-muted-foreground">{c.sub}</div>
            <div className="mt-2 text-[11px] text-content-faint opacity-0 transition-opacity group-hover:opacity-100">
              ↳ 填入输入框，可编辑后发送
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
