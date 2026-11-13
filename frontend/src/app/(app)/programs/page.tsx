import type { Metadata } from "next";
import { PreventivePrograms } from "@/components/features";

export const metadata: Metadata = { title: "Preventive care programs" };

export default function ProgramsPage() {
  return <PreventivePrograms />;
}
