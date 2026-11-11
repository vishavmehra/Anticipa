import type { Metadata } from "next";
import { ResidentDetailView } from "./resident-detail";

export const metadata: Metadata = { title: "Resident" };

export default async function ResidentPage({ params }: PageProps<"/residents/[id]">) {
  const { id } = await params;
  return <ResidentDetailView id={id} />;
}
