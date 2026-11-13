import type { Metadata } from "next";
import { PopulationJourney } from "@/components/features";

export const metadata: Metadata = { title: "Care journey" };

export default function JourneyPage() {
  return <PopulationJourney />;
}
