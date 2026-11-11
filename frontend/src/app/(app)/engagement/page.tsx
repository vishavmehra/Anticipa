import type { Metadata } from "next";
import { ModulePage } from "@/components/module-page";

export const metadata: Metadata = { title: "Engagement" };

export default function EngagementPage() {
  return (
    <ModulePage
      title="Engagement"
      description="Outreach to residents and their responses, tracked until the care is completed."
      endpoint="GET /api/v1/care-gaps/{case_id}/communications"
      willShow={[
        "Outreach events and resident responses, every message labelled Simulated.",
        "Consent state and the escalation of out-of-scope questions to a clinician.",
      ]}
    />
  );
}
