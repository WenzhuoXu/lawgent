import { useState } from "react";
import { MessageSquarePlus, Search, Settings, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Context, ContextTrigger } from "@/components/ai-elements/context";
import type { ChatSummary } from "@/types/chat";
import type { personaCopy } from "@/persona/persona";

type Copy = ReturnType<typeof personaCopy>;

export function Sidebar({
  chats,
  activeId,
  onOpen,
  onNew,
  onRename,
  onDelete,
  copy,
  onOpenSettings,
  open,
  onClose,
  usedTokens,
  maxTokens,
}: {
  chats: ChatSummary[];
  activeId: string | null;
  onOpen: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  copy: Copy;
  onOpenSettings: () => void;
  open: boolean;
  onClose: () => void;
  /** live context-window fill for the sidebar ring (percent only, no cost) */
  usedTokens?: number;
  maxTokens?: number;
}) {
  const [query, setQuery] = useState("");
  const filtered = chats.filter((c) => c.title.toLowerCase().includes(query.toLowerCase()));

  return (
    <aside
      className={cn(
        "z-30 flex h-dvh flex-col border-r bg-panel",
        "max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:w-72 max-md:transition-transform",
        open ? "max-md:translate-x-0" : "max-md:-translate-x-full",
      )}
    >
      <div className="flex items-center gap-2 px-4 pb-2.5 pt-4 font-semibold">
        <span className="flex size-6 items-center justify-center rounded-lg bg-primary text-sm text-primary-foreground">
          {copy.avatar}
        </span>
        {copy.brand}
        {copy.brandSub ? <span className="text-xs font-normal text-muted-foreground">{copy.brandSub}</span> : null}
      </div>

      <button
        onClick={onNew}
        className="mx-3 mb-2.5 flex items-center gap-2 rounded-xl border bg-card px-3 py-2 text-sm font-medium hover:border-primary/40"
      >
        <MessageSquarePlus className="size-4" /> 新对话
      </button>

      <div className="mx-3 mb-3 flex items-center gap-2 rounded-xl bg-accent px-3 py-1.5 text-sm">
        <Search className="size-4 text-muted-foreground" />
        <input
          className="w-full bg-transparent outline-none placeholder:text-muted-foreground"
          placeholder="搜索对话…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      <div className="px-4 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        历史对话
      </div>
      <nav className="flex-1 overflow-y-auto px-2">
        {filtered.map((c) => (
          <div
            key={c.id}
            className={cn(
              "group mx-1 my-0.5 flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm",
              c.id === activeId ? "bg-accent font-medium" : "text-content-secondary hover:bg-accent/60",
            )}
          >
            <button className="min-w-0 flex-1 truncate text-left" onClick={() => onOpen(c.id)}>
              {c.title || "未命名对话"}
            </button>
            <button
              className="opacity-0 transition-opacity group-hover:opacity-100"
              onClick={() => {
                const t = window.prompt("重命名对话", c.title);
                if (t != null) onRename(c.id, t);
              }}
              aria-label="Rename"
            >
              ✎
            </button>
            <button
              className="opacity-0 transition-opacity group-hover:opacity-100"
              onClick={() => onDelete(c.id)}
              aria-label="Delete"
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        ))}
      </nav>

      <div className="border-t p-2">
        {maxTokens && maxTokens > 0 ? (
          <div className="mb-1 flex items-center justify-between rounded-lg px-2.5 py-1.5">
            <span className="text-[11px] text-muted-foreground">上下文 / Context</span>
            {/* Percent + ring only — per-session cost stays in the monthly
                CostCenter, so we render no ContextContent hover-card footer. */}
            <Context usedTokens={usedTokens ?? 0} maxTokens={maxTokens}>
              <ContextTrigger className="h-auto gap-1.5 px-1.5 py-1" />
            </Context>
          </div>
        ) : null}
        <button
          onClick={onOpenSettings}
          className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-sm text-content-secondary hover:bg-accent"
        >
          <Settings className="size-4" /> 设置 / Settings
        </button>
      </div>
    </aside>
  );
}
