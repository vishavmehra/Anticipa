import type { Metadata } from "next";
import { EvidenceGuidelines } from "@/components/features";

export const metadata: Metadata = { title: "Evidence and guidelines" };

export default function EvidenceReviewPage() {
  return <EvidenceGuidelines />;
}
