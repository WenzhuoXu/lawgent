/**
 * Normalizes raw SSE frames into a flat `{ type, payload, agent, runId }` shape,
 * un-nesting the `workflow_event` envelope. Shared by the live translator and
 * the rehydration builder so both see identical normalized events.
 *
 * Envelope shape verified against captured fixtures:
 *   event: workflow_event
 *   data: { event_type, agent_name, run_id, data: { event_type, agent, run_id, data: {...payload} } }
 * i.e. the real payload is at `.data.data` of the frame.
 *
 * Persisted events (from GET /api/chats/{id}) share the outer envelope shape:
 *   { id, chat_id, created_at, event_type, agent_name, run_id, data: { ..., data: {...payload} } }
 */

export type NormalizedEvent = {
  /** canonical event type (inner workflow event type, or the top-level SSE name) */
  type: string;
  /** the innermost payload object */
  payload: Record<string, unknown>;
  agent?: string;
  runId?: string;
  createdAt?: string;
};

const WORKFLOW_ENVELOPE = "workflow_event";

/**
 * Given a top-level SSE (eventName, data) OR a persisted event row, return the
 * normalized event. Handles both the direct events (tool_call, delta, …) and
 * the workflow_event envelope.
 */
export function normalizeEvent(
  eventName: string,
  data: unknown,
): NormalizedEvent {
  const d = (data ?? {}) as Record<string, unknown>;

  // workflow_event envelope: unwrap to the inner type + payload.
  if (eventName === WORKFLOW_ENVELOPE || (d.event_type && d.data && typeof d.data === "object")) {
    const inner = d.data as Record<string, unknown>;
    const type = (d.event_type as string) || (inner.event_type as string) || eventName;
    // Innermost payload is inner.data when present, else inner itself.
    const payload = (inner.data && typeof inner.data === "object"
      ? inner.data
      : inner) as Record<string, unknown>;
    return {
      type,
      payload: payload ?? {},
      agent: (inner.agent as string) ?? (d.agent_name as string) ?? undefined,
      runId: (inner.run_id as string) ?? (d.run_id as string) ?? undefined,
      createdAt: (d.created_at as string) ?? (inner.ts as string) ?? undefined,
    };
  }

  // Direct event: the frame data IS the payload.
  return {
    type: eventName,
    payload: d,
    agent: (d.agent as string) ?? undefined,
    runId: (d.run_id as string) ?? undefined,
    createdAt: (d.created_at as string) ?? (d.ts as string) ?? undefined,
  };
}

/** Normalize a persisted event row (GET /api/chats/{id} → events[]). */
export function normalizePersistedEvent(row: Record<string, unknown>): NormalizedEvent {
  const type = (row.event_type as string) || "workflow_event";
  const outer = (row.data ?? {}) as Record<string, unknown>;
  // Persisted rows for enveloped events keep the nested {..., data:{...}}; direct
  // rows (workflow_plan, citation_audit, tool_call) store the payload flat in data.
  const payload = (outer.data && typeof outer.data === "object"
    ? (outer.data as Record<string, unknown>)
    : outer) as Record<string, unknown>;
  return {
    type,
    payload: payload ?? {},
    agent: (row.agent_name as string) ?? (outer.agent as string) ?? undefined,
    runId: (row.run_id as string) ?? (outer.run_id as string) ?? undefined,
    createdAt: (row.created_at as string) ?? undefined,
  };
}
