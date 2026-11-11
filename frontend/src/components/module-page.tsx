import type { ReactNode } from "react";
import { PageHeader, Unavailable } from "./ui";

/**
 * Honest placeholder for a module whose API endpoint is not in the current build.
 * It states what is missing and what the page will show; it never renders sample figures.
 */
export function ModulePage({
  title,
  description,
  endpoint,
  willShow,
}: {
  title: string;
  description: string;
  /** Contract path the page will read, e.g. "GET /api/v1/residents". */
  endpoint: string;
  willShow: readonly ReactNode[];
}) {
  return (
    <>
      <PageHeader title={title} description={description} />
      <Unavailable endpoint={endpoint} willShow={willShow} />
    </>
  );
}
