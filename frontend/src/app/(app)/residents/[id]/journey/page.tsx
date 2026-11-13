import type { Metadata } from "next";
import { ResidentJourney } from "@/components/features";

export const metadata: Metadata = { title: "Resident journey" };

export default async function JourneyPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ResidentJourney id={id} />;
}
