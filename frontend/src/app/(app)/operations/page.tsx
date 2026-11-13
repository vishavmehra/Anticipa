import type { Metadata } from "next";
import { OperationsCapacity } from "@/components/features";

export const metadata: Metadata = { title: "Operations and capacity" };

export default function OperationsPage() {
  return <OperationsCapacity />;
}
