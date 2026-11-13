import { ok, type ApiHandler } from "./helpers";

// Route mocks for GET /care-gaps/{id}/conversation and POST /care-gaps/{id}/conversation/messages, shared by the specs that mount
// <CaseConversation /> (journey ACT panel, care gap detail page). Invented test text, shapes from backend/anticipa/conversation.

const message = (id: string, over: Record<string, unknown>) => ({
  id, role: "resident", body: "", language: "en", direction: "ltr", intent: null, source: "resident_input", escalation_state: null, escalation_reason: null, proposal: null,
  model_id: null, created_at: "2026-11-13T09:00:00Z", synthetic_status: "simulated", simulated: true, marker: null, reply_sent: null, ...over,
});

export const conversationThread = (caseId: string) => ({
  care_gap_case_id: caseId,
  case_status: "planned",
  resident_language: "en",
  escalated: false,
  consent: { denied_channels: [], outreach_blocked: false, ai_processing_denied: false },
  messages: [
    message("aaaaaaaa-0000-4000-8000-0000000000a1", { body: "When is my appointment and where?", intent: "location_time", reply_sent: true }),
    message("aaaaaaaa-0000-4000-8000-0000000000a2", {
      role: "assistant", body: "Your appointment is on Friday 20 November 2026 at 10:15 at Test Clinic. Please arrive a little early.", source: "template", intent: "location_time", marker: "[SIMULATED]",
      created_at: "2026-11-13T09:00:01Z",
    }),
  ],
  simulated: true,
  notice: "Simulated conversation: no message was sent to a real person and nothing was booked.",
});

/** Handlers for one case. `posted` receives the body of every POST so a spec can assert what was sent. */
export function conversationHandlers(caseId: string, posted: unknown[] = []): Record<string, ApiHandler> {
  return {
    [`GET /care-gaps/${caseId}/conversation`]: () => ({ body: ok(conversationThread(caseId)) }),
    [`POST /care-gaps/${caseId}/conversation/messages`]: (req) => {
      const sent = req.postDataJSON() as { text: string; language?: string };
      posted.push(sent);
      return {
        body: ok({
          resident_message: message("aaaaaaaa-0000-4000-8000-0000000000b1", { body: sent.text, language: sent.language ?? "en", intent: "scheduling", reply_sent: true, created_at: "2026-11-13T09:05:00Z" }),
          reply: message("aaaaaaaa-0000-4000-8000-0000000000b2", {
            role: "assistant", body: "Your appointment is on Friday 20 November 2026 at 10:15 at Test Clinic.", source: "template", intent: "scheduling", marker: "[SIMULATED]", created_at: "2026-11-13T09:05:01Z",
          }),
          suppressed: false, suppressed_reason: null, channel: "sms", escalation: null, opt_out: null, case_status: "planned", cost_usd: 0.0001, latency_ms: 900, simulated: true, notice: "Simulated.",
        }),
      };
    },
  };
}
