/**
 * Rebuilds a user message's attachment parts from its persisted
 * `metadata.attachments`, in the same `data-file` shape the composer sends.
 */

import type { MessagePart } from "@/lib/foldChunks";

export function attachmentPartsFromMetadata(
  metadata: Record<string, unknown> | undefined,
): MessagePart[] {
  const paths = ((metadata?.attachments as string[]) ?? []).filter(Boolean);
  return paths.map((path) => ({ type: "data-file", id: path, data: { path } }) as MessagePart);
}
