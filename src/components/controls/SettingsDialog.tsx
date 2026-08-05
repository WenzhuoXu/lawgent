import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { client } from "@/api/client";
import { cn } from "@/lib/utils";
import type { PersonaId, ThemeId } from "@/hooks/useTheme";
import type { ChatSettings, ProviderId, RuntimeOptions, UsageSummary } from "@/types/chat";

const PROVIDER_LABEL: Record<ProviderId, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
};

const THEME_OPTIONS: Array<{ id: ThemeId; label: string }> = [
  { id: "system", label: "跟随系统" },
  { id: "paper", label: "浅色 Paper" },
  { id: "ink", label: "深色 Ink" },
];

export function SettingsDialog({
  open,
  onClose,
  runtime,
  settings,
  setSettings,
  theme,
  setTheme,
  persona,
  setPersona,
}: {
  open: boolean;
  onClose: () => void;
  runtime: RuntimeOptions | null;
  settings: ChatSettings;
  setSettings: (fn: (s: ChatSettings) => ChatSettings) => void;
  theme: ThemeId;
  setTheme: (t: ThemeId) => void;
  persona: PersonaId;
  setPersona: (p: PersonaId) => void;
}) {
  const providers = runtime?.providers ?? ["openai", "anthropic"];
  const models = runtime?.models[settings.provider] ?? [settings.model];
  const efforts = runtime?.reasoning_efforts ?? ["none", "low", "medium", "high", "xhigh"];

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? undefined : onClose())}>
      <DialogContent className="max-h-[85dvh] gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="border-b px-5 py-4">
          <DialogTitle>设置 / Settings</DialogTitle>
          <DialogDescription>模型、外观与用量 / Model, appearance & usage</DialogDescription>
        </DialogHeader>

        <div className="flex max-h-[calc(85dvh-70px)] flex-col gap-6 overflow-y-auto px-5 py-5">
          {/* Model / provider / reasoning */}
          <Section title="模型 / Model">
            <Field label="服务商 / Provider">
              <div className="flex flex-wrap gap-1.5">
                {providers.map((prov) => (
                  <SegButton
                    key={prov}
                    active={prov === settings.provider}
                    onClick={() =>
                      setSettings((s) => ({
                        ...s,
                        provider: prov as ProviderId,
                        model: runtime?.models[prov as ProviderId]?.[0] ?? s.model,
                      }))
                    }
                  >
                    {PROVIDER_LABEL[prov as ProviderId] ?? prov}
                  </SegButton>
                ))}
              </div>
            </Field>

            <Field label="模型 / Model">
              <select
                className="w-full rounded-md border bg-background px-3 py-2 text-sm outline-none focus-visible:border-ring"
                value={settings.model}
                onChange={(e) => setSettings((s) => ({ ...s, model: e.target.value }))}
              >
                {models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>

            {settings.provider === "openai" ? (
              <Field label="推理强度 / Reasoning">
                <div className="flex flex-wrap gap-1.5">
                  {efforts.map((r) => (
                    <SegButton
                      key={r}
                      active={r === settings.reasoning_effort}
                      onClick={() => setSettings((s) => ({ ...s, reasoning_effort: r }))}
                    >
                      {r}
                    </SegButton>
                  ))}
                </div>
              </Field>
            ) : null}
          </Section>

          {/* Appearance */}
          <Section title="外观 / Appearance">
            <Field label="主题 / Theme">
              <div className="flex flex-wrap gap-1.5">
                {THEME_OPTIONS.map((t) => (
                  <SegButton key={t.id} active={t.id === theme} onClick={() => setTheme(t.id)}>
                    {t.label}
                  </SegButton>
                ))}
              </div>
            </Field>
            <Field label="语气 / Persona">
              <div className="flex flex-wrap gap-1.5">
                <SegButton active={persona === "professional"} onClick={() => setPersona("professional")}>
                  专业
                </SegButton>
                <SegButton active={persona === "xiugou"} onClick={() => setPersona("xiugou")}>
                  🐶 修勾
                </SegButton>
              </div>
            </Field>
          </Section>

          {/* Cost center */}
          <Section title="用量与费用 / Usage & cost">
            <CostCenter open={open} />
          </Section>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <div className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">{title}</div>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs text-content-secondary">{label}</span>
      {children}
    </div>
  );
}

function SegButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-full border px-3 py-1 text-sm transition-colors",
        active
          ? "border-primary bg-primary text-primary-foreground"
          : "text-content-secondary hover:bg-accent",
      )}
    >
      {children}
    </button>
  );
}

function fmtInt(n: number): string {
  return n.toLocaleString("en-US");
}

function fmtUsd(n: number): string {
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function CostCenter({ open }: { open: boolean }) {
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [month, setMonth] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(false);
    client
      .usage(month)
      .then((u) => {
        if (!cancelled) setUsage(u);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, month]);

  if (loading && !usage) {
    return (
      <div className="flex items-center gap-2 rounded-lg border bg-card px-3 py-4 text-xs text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" /> 加载用量中… / Loading usage…
      </div>
    );
  }

  if (error || !usage) {
    return (
      <p className="rounded-lg border bg-card px-3 py-4 text-xs text-muted-foreground">
        无法加载用量 / Unable to load usage.
      </p>
    );
  }

  const months = usage.months ?? [usage.month];

  return (
    <div className="flex flex-col gap-3">
      {months.length > 1 ? (
        <select
          className="w-fit rounded-md border bg-background px-2.5 py-1.5 text-xs outline-none focus-visible:border-ring"
          value={usage.month}
          onChange={(e) => setMonth(e.target.value)}
        >
          {months.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      ) : (
        <div className="text-xs text-muted-foreground">{usage.month}</div>
      )}

      <div className="grid grid-cols-3 gap-2">
        <StatTile label="预估费用 / Cost" value={fmtUsd(usage.estimated_cost_usd)} />
        <StatTile label="总 tokens" value={fmtInt(usage.totals.total_tokens)} />
        <StatTile label="请求数 / Requests" value={fmtInt(usage.totals.requests)} />
      </div>

      {usage.by_model.length > 0 ? (
        <div className="overflow-hidden rounded-lg border">
          <table className="w-full text-left text-xs">
            <thead className="bg-accent/50 text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="px-2.5 py-1.5 font-medium">模型 / Model</th>
                <th className="px-2.5 py-1.5 text-right font-medium">Tokens</th>
                <th className="px-2.5 py-1.5 text-right font-medium">费用</th>
              </tr>
            </thead>
            <tbody>
              {usage.by_model.map((b) => (
                <tr key={`${b.provider}:${b.model}`} className="border-t">
                  <td className="px-2.5 py-1.5">
                    <div className="font-mono">{b.model}</div>
                    <div className="text-[10px] text-content-faint">{b.provider}</div>
                  </td>
                  <td className="px-2.5 py-1.5 text-right tabular-nums">
                    {fmtInt(b.input_tokens + b.output_tokens)}
                  </td>
                  <td className="px-2.5 py-1.5 text-right tabular-nums">{fmtUsd(b.estimated_cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-xs text-content-faint">本月暂无用量记录 / No usage recorded this month.</p>
      )}

      {usage.note ? <p className="text-[10px] text-content-faint">{usage.note}</p> : null}
    </div>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border bg-card p-2.5">
      <div className="truncate text-base font-semibold tabular-nums">{value}</div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}
