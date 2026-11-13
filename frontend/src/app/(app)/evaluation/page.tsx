import type { Metadata } from "next";
import { ProgramsImpact } from "@/components/features";

export const metadata: Metadata = { title: "Programs and impact" };

export default function EvaluationPage() {
  return <ProgramsImpact />;
}
