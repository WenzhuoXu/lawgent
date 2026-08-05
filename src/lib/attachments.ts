/**
 * Attachment helpers ported/typed from main.jsx (basenameOf/guessMime/
 * categorizeAttachment/attachmentFromPath/formatFileSize) plus a UIMessage
 * file-part builder for rehydration.
 */

import type { MessagePart } from "@/lib/foldChunks";

export function basenameOf(path = ""): string {
  const cleaned = String(path).split(/[?#]/)[0];
  const idx = Math.max(cleaned.lastIndexOf("/"), cleaned.lastIndexOf("\\"));
  return idx >= 0 ? cleaned.slice(idx + 1) : cleaned;
}

const MIME_BY_EXT: Record<string, string> = {
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".csv": "text/csv",
  ".txt": "text/plain",
  ".md": "text/markdown",
  ".json": "application/json",
};

export function guessMimeFromName(name = ""): string {
  const ext = (name.match(/\.[^.]+$/)?.[0] || "").toLowerCase();
  return MIME_BY_EXT[ext] || "application/octet-stream";
}

export function categorizeAttachment(name: string, mime: string): string {
  if (mime.startsWith("image/")) return "image";
  if (mime === "application/pdf") return "PDF";
  if (name.endsWith(".docx")) return "Word";
  if (name.endsWith(".xlsx")) return "Excel";
  if (name.endsWith(".pptx")) return "PowerPoint";
  return "file";
}

export function formatFileSize(bytes?: number): string {
  if (!bytes || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** Build UIMessage file parts from a user message's metadata.attachments. */
export function attachmentPartsFromMetadata(
  metadata: Record<string, unknown> | undefined,
): MessagePart[] {
  const paths = ((metadata?.attachments as string[]) ?? []).filter(Boolean);
  return paths.map((path) => {
    const name = basenameOf(path);
    const mime = guessMimeFromName(name);
    return {
      type: "data-file",
      id: path,
      data: { path, filename: name, mediaType: mime, category: categorizeAttachment(name, mime) },
    } as MessagePart;
  });
}
