import type { Metadata } from "next";
import { CareGapDetailView } from "./care-gap-detail";

export const metadata: Metadata = { title: "Care gap" };

export default async function CareGapPage({ params }: PageProps<"/care-gaps/[id]">) {
  const { id } = await params;
  return <CareGapDetailView id={id} />;
}
