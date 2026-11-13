// Response shapes of GET /api/v1/care-gaps/{id}/conversation and POST /api/v1/care-gaps/{id}/conversation/messages
// (backend/anticipa/conversation/routes.py). Every message is simulated: nothing is sent to a real person.

export type ConversationLanguage = "en" | "hi" | "ar";
export type ConversationChannel = "sms" | "whatsapp" | "phone_call" | "email";

export interface ProposedSlot {
  ref: string;
  slot_id?: string;
  date: string;
  start: string;
  end: string;
  timezone: string;
  clinic: string | null;
  address: string | null;
  service: string | null;
  status: "proposed";
  requires_coordinator_confirmation: boolean;
}

export type EscalationReason = "emergency" | "medical_question" | "eligibility_dispute" | "clinician_flag" | "unsafe_reply";
export type EscalationState = "escalated" | "already_escalated" | "case_closed";

export interface ConversationMessage {
  id: string;
  role: "resident" | "assistant";
  body: string;
  language: ConversationLanguage;
  direction: "ltr" | "rtl";
  intent: string | null;
  source: "resident_input" | "template" | "model";
  escalation_state: EscalationState | null;
  escalation_reason: EscalationReason | null;
  proposal: ProposedSlot | null;
  model_id: string | null;
  created_at: string;
  synthetic_status: string;
  simulated: boolean;
  marker: string | null;
  /** Resident messages only: false when outreach on the channel was denied and no reply was sent. */
  reply_sent: boolean | null;
}

export interface ConversationThread {
  care_gap_case_id: string;
  case_status: string;
  resident_language: string | null;
  escalated: boolean;
  consent: { denied_channels: string[]; outreach_blocked: boolean; ai_processing_denied: boolean };
  messages: ConversationMessage[];
  simulated: boolean;
  notice: string;
}

export interface PostResult {
  resident_message: ConversationMessage;
  reply: ConversationMessage | null;
  suppressed: boolean;
  suppressed_reason: string | null;
  /** emergency_safety_reply: the urgent care reply was sent although outreach on the channel is denied. */
  consent_override: string | null;
  channel: ConversationChannel;
  escalation: { reason: EscalationReason; state: EscalationState; urgent: boolean } | null;
  opt_out: { detected: boolean; consent_recorded: boolean; consent_record_id: string | null } | null;
  case_status: string;
  cost_usd: number;
  latency_ms: number;
  simulated: boolean;
  notice: string;
}
