// Shapes of GET/POST /api/v1/residents/{id}/consents (backend/anticipa/consent/routes.py, ADR-070).

export type ConsentScope = "outreach" | "data_use_ai_processing";
export type ConsentStatus = "permit" | "deny";
export type ConsentChannel = "sms" | "whatsapp" | "phone_call" | "email";

export interface ConsentRecord {
  id: string;
  resident_id: string;
  scope: ConsentScope;
  status: ConsentStatus;
  channel: ConsentChannel | null; // outreach only; null means every channel
  language: string | null;
  source: "resident_request" | "coordinator_entry" | "synthetic_seed";
  effective_from: string;
  effective_to: string | null;
  recorded_by: string | null;
  note: string | null;
  synthetic_status: string;
  created_at: string;
  in_force: boolean; // the newest currently effective record of its scope and channel
  fhir: Record<string, unknown>; // FHIR R4 Consent shape of this record
}

export interface ConsentView {
  resident_id: string;
  effective: {
    outreach: { permitted_channels: ConsentChannel[]; denied_channels: ConsentChannel[]; blocked: boolean };
    data_use_ai_processing: ConsentStatus;
  };
  records: ConsentRecord[];
  default_note: string;
  notice: string;
}

export interface ConsentInput {
  scope: ConsentScope;
  status: ConsentStatus;
  channel?: ConsentChannel | null;
  source: "resident_request" | "coordinator_entry";
  note?: string | null;
}
