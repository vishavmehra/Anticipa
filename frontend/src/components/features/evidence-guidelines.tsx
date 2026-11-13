"use client";

// Evidence and guidelines page: tabs for the evidence extraction view (reference layout), the review queue (accept or
// reject facts, kept from the earlier page) and a link tab to the guideline library. Route: /evidence-review.

import { useRouter } from "next/navigation";
import { useState } from "react";
import { PageHeader } from "@/components/ui";
import { EvidenceExtraction } from "./evidence-extraction";
import { PillTabs, TabPanel, type PillTab } from "./kit";
import { ReviewQueue } from "./review-queue";

type Tab = "extraction" | "queue" | "guidelines";
const TABS: PillTab<Tab>[] = [
  { key: "extraction", label: "Evidence extraction", icon: "file" },
  { key: "queue", label: "Review queue", icon: "checklist" },
  { key: "guidelines", label: "Guidelines", icon: "scale" },
];

export function EvidenceGuidelines() {
  const [tab, setTab] = useState<Tab>("extraction");
  const router = useRouter();
  return (
    <>
      <PageHeader
        title="Evidence and guidelines"
        description="Turn clinical records into structured evidence and actionable preventive care."
        actions={<PillTabs tabs={TABS} active={tab} onChange={(k) => (k === "guidelines" ? router.push("/guidelines") : setTab(k))} label="Evidence views" panelId="evidence-panel" />}
      />
      <TabPanel id="evidence-panel" label={TABS.find((t) => t.key === tab)!.label}>
        {tab === "extraction" ? <EvidenceExtraction /> : <ReviewQueue />}
      </TabPanel>
    </>
  );
}
