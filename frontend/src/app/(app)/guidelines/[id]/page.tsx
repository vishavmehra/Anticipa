import type { Metadata } from "next";
import { GuidelineDetailView } from "./guideline-detail";

export const metadata: Metadata = { title: "Guideline" };

export default async function GuidelinePage({ params }: PageProps<"/guidelines/[id]">) {
  const { id } = await params;
  return <GuidelineDetailView id={id} />;
}
