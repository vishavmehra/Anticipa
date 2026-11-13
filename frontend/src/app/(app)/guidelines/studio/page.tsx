import type { Metadata } from "next";
import { Suspense } from "react";
import { GuidelineStudio } from "@/components/studio/studio";

export const metadata: Metadata = { title: "Guideline Studio" };

export default function GuidelineStudioPage() {
  return (
    <Suspense fallback={<div className="skeleton h-32" role="status" aria-label="Loading Guideline Studio" />}>
      <GuidelineStudio />
    </Suspense>
  );
}
