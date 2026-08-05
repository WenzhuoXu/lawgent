import { useEffect, useRef, useState } from "react";
import { Globe, Paperclip, Send, Sparkles, Square } from "lucide-react";
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputTextarea,
  PromptInputTools,
} from "@/components/ai-elements/prompt-input";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { client } from "@/api/client";
import { cn } from "@/lib/utils";
import type { ChatSettings, RuntimeOptions } from "@/types/chat";

export function Composer({
  runtime,
  settings,
  setSettings,
  busy,
  onSend,
  onStop,
  placeholder,
  chatId,
  prefill,
}: {
  runtime: RuntimeOptions | null;
  settings: ChatSettings;
  setSettings: (fn: (s: ChatSettings) => ChatSettings) => void;
  busy: boolean;
  onSend: (text: string, attachmentPaths: string[]) => void;
  onStop: () => void;
  placeholder: string;
  chatId: string | null;
  prefill?: { text: string; n: number };
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Array<{ path: string; name: string }>>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // Apply a prefill seed (from welcome inspiration chips): fill the box and
  // focus it so the user can edit before sending. Never auto-sends.
  const lastPrefill = useRef(0);
  useEffect(() => {
    if (prefill && prefill.n !== lastPrefill.current) {
      lastPrefill.current = prefill.n;
      setText(prefill.text);
      rootRef.current?.querySelector("textarea")?.focus();
    }
  }, [prefill]);

  const skills = runtime?.skills ?? [];

  const submit = () => {
    const t = text.trim();
    if (!t || busy) return;
    onSend(t, attachments.map((a) => a.path));
    setText("");
    setAttachments([]);
  };

  const upload = async (files: FileList | null) => {
    if (!files || !chatId) return;
    for (const file of Array.from(files)) {
      try {
        const res = await client.uploadToChat(chatId, file);
        setAttachments((a) => [...a, { path: res.path, name: res.filename }]);
      } catch {
        /* ignore */
      }
    }
  };

  return (
    <div ref={rootRef}>
    <PromptInput onSubmit={() => submit()}>
      <PromptInputBody>
        {attachments.length > 0 ? (
          <div className="flex flex-wrap gap-2 px-1 pt-1">
            {attachments.map((a) => (
              <span key={a.path} className="rounded-md bg-accent px-2 py-0.5 text-xs text-muted-foreground">
                📎 {a.name}
                <button className="ml-1" onClick={() => setAttachments((x) => x.filter((y) => y.path !== a.path))}>
                  ✕
                </button>
              </span>
            ))}
          </div>
        ) : null}
        <PromptInputTextarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={placeholder}
          disabled={busy}
        />
      </PromptInputBody>

      <PromptInputFooter>
        <PromptInputTools>
          {/* Attach */}
          <input
            ref={fileRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => upload(e.target.files)}
          />
          <button
            type="button"
            className="flex items-center gap-1 rounded-full px-2.5 py-1 text-sm text-content-secondary hover:bg-accent"
            onClick={() => fileRef.current?.click()}
            disabled={busy || !chatId}
            title={chatId ? "添加附件" : "发送首条消息后可添加附件"}
          >
            <Paperclip className="size-4" /> 附件
          </button>

          {/* Skills */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className={cn(
                  "flex items-center gap-1 rounded-full px-2.5 py-1 text-sm hover:bg-accent",
                  settings.skill_hint ? "text-primary" : "text-content-secondary",
                )}
                disabled={busy}
              >
                <Sparkles className="size-4" /> {settings.skill_hint ?? "技能"}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="max-h-80 overflow-y-auto">
              <DropdownMenuLabel>选择技能 / Skill</DropdownMenuLabel>
              <DropdownMenuItem onClick={() => setSettings((s) => ({ ...s, skill_hint: null }))}>
                自动（无指定）
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              {skills.map((sk) => (
                <DropdownMenuItem key={sk.name} onClick={() => setSettings((s) => ({ ...s, skill_hint: sk.name }))}>
                  <span className="font-mono text-xs">{sk.name}</span>
                  {sk.description ? <span className="ml-2 truncate text-xs text-muted-foreground">{sk.description}</span> : null}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          {/* Web tools */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="flex items-center gap-1 rounded-full border px-2.5 py-1 text-sm text-content-secondary hover:bg-accent"
                disabled={busy}
              >
                <Globe className="size-4" />
                {settings.enable_web_search || settings.enable_web_fetch ? "网络" : "关"}
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuCheckboxItem
                checked={!!settings.enable_web_search}
                onCheckedChange={(v) => setSettings((s) => ({ ...s, enable_web_search: !!v }))}
              >
                网络搜索 / Web search
              </DropdownMenuCheckboxItem>
              <DropdownMenuCheckboxItem
                checked={!!settings.enable_web_fetch}
                onCheckedChange={(v) => setSettings((s) => ({ ...s, enable_web_fetch: !!v }))}
              >
                网页抓取 / Web fetch
              </DropdownMenuCheckboxItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </PromptInputTools>

        <div className="flex items-center gap-2">
          {/* Model badge — full selection lives in Settings. */}
          <span
            className="hidden rounded-full px-2.5 py-1 text-xs text-content-faint sm:inline"
            title="在设置中切换模型 / Change model in Settings"
          >
            {settings.model}
          </span>

          {busy ? (
            <button
              type="button"
              onClick={onStop}
              className="flex size-8 items-center justify-center rounded-full bg-destructive text-destructive-foreground"
              aria-label="Stop"
            >
              <Square className="size-3.5" />
            </button>
          ) : (
            <button
              type="submit"
              className="flex size-8 items-center justify-center rounded-full bg-primary text-primary-foreground disabled:opacity-40"
              disabled={!text.trim()}
              aria-label="Send"
            >
              <Send className="size-4" />
            </button>
          )}
        </div>
      </PromptInputFooter>
    </PromptInput>
    </div>
  );
}
