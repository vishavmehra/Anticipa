import type { Metadata } from "next";
import { PageHeader } from "@/components/ui";
import { ProcessingMonitor } from "@/components/features";

export const metadata: Metadata = { title: "Live monitor" };

export default function MonitorPage() {
  return (
    <>
      <PageHeader title="Live monitor" description="The resident pipeline, background jobs, AI spend and notes as they are right now. Figures refresh by themselves." />
      <ProcessingMonitor />
    </>
  );
}
