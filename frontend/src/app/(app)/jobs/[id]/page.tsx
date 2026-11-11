import type { Metadata } from "next";
import { JobDetail } from "./job-detail";

export const metadata: Metadata = { title: "Job" };

export default async function JobPage({ params }: PageProps<"/jobs/[id]">) {
  const { id } = await params;
  return <JobDetail id={id} />;
}
