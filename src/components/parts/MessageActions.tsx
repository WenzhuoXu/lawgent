/**
 * Per-assistant-message action bar: copy, export DOCX/PDF (binds the server
 * export endpoint), regenerate, and a duration chip. Export on a DO-NOT-FILE
 * turn opens a watermark-gating confirm dialog.
 */

import { useState } from "react";
import { Clock, Copy, Download, RefreshCw } from "lucide-react";
import { formatDuration } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { client } from "@/api/client";
import type { MessagePart } from "@/lib/foldChunks";
import type { AuditData, UsageData } from "@/lib/dataParts";

export function MessageActions({
  chatId,
  messageId,
  parts,
  onRegenerate,
  onArtifact,
}: {
  chatId: string | null;
  messageId: string;
  parts: MessagePart[];
  onRegenerate?: () => void;
  onArtifact?: (a: { id: string; name: string; url: string }) => void;
}) {
  const [confirmFmt, setConfirmFmt] = useState<"pdf" | "docx" | null>(null);
  const audit = (parts.find((p) => p.type === "data-audit") as { data: AuditData } | undefined)?.data;
  const doNotFile = !!audit?.doNotFile;
  const usage = (parts.find((p) => p.type === "data-usage") as { data: UsageData } | undefined)?.data;
  const durationLabel = formatDuration(usage?.durationMs);

  const text = parts.filter((p) => p.type === "text").map((p) => (p as { text: string }).text).join("");

  // Watermark copy MUST match the confirm dialog's promise so what the user
  // approves is exactly what the exported file carries.
  const WATERMARK = "草稿 · 引证未经核验 / DRAFT — CITATIONS UNVERIFIED";

  const doExport = async (fmt: "pdf" | "docx") => {
    if (!chatId || !messageId) return;
    const artifact = await client
      .exportMessage(chatId, messageId, fmt, doNotFile ? WATERMARK : undefined)
      .catch(() => null);
    if (artifact) onArtifact?.(artifact);
  };

  const requestExport = (fmt: "pdf" | "docx") => {
    if (doNotFile) setConfirmFmt(fmt);
    else doExport(fmt);
  };

  return (
    <div className="mt-1 flex items-center gap-1 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100">
      <button
        className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-accent"
        onClick={() => navigator.clipboard?.writeText(text)}
      >
        <Copy className="size-3.5" /> 复制
      </button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-accent">
            <Download className="size-3.5" /> 导出
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onClick={() => requestExport("docx")}>导出为 DOCX</DropdownMenuItem>
          <DropdownMenuItem onClick={() => requestExport("pdf")}>导出为 PDF</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {onRegenerate ? (
        <button
          className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-accent"
          onClick={onRegenerate}
        >
          <RefreshCw className="size-3.5" /> 重新生成
        </button>
      ) : null}
      {durationLabel ? (
        <span className="ml-1 flex items-center gap-1 text-xs text-content-faint" title="本轮用时 / Turn duration">
          <Clock className="size-3.5" /> {durationLabel}
        </span>
      ) : null}

      <Dialog open={confirmFmt != null} onOpenChange={(o) => !o && setConfirmFmt(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>⛔ 该文稿未通过引证核验</DialogTitle>
            <DialogDescription>
              导出文件将加注「草稿 — 引证未经核验 / DRAFT — CITATIONS UNVERIFIED」水印。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <button
              className="rounded-md border border-dnf px-3 py-1.5 text-sm text-[var(--dnf)]"
              onClick={() => {
                if (confirmFmt) doExport(confirmFmt);
                setConfirmFmt(null);
              }}
            >
              仍然导出（加水印）
            </button>
            <button
              className="rounded-md bg-accent px-3 py-1.5 text-sm"
              onClick={() => setConfirmFmt(null)}
            >
              取消
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
