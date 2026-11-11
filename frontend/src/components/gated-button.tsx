"use client";

import type { ReactNode } from "react";
import { useCan } from "./app-shell";

/**
 * Action button driven by a permission from GET /auth/me. Without the permission it stays in the page,
 * announced as unavailable (aria-disabled keeps it focusable so screen-reader users reach the reason)
 * and explains itself with a "Read-only access" tooltip. The backend still rejects the call regardless.
 */
export function GatedButton({
  permission,
  className,
  onClick,
  disabled,
  children,
}: {
  /** One permission, or several of which holding ANY enables the action. */
  permission: string | readonly string[];
  className: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  const can = useCan();
  const allowed = ([] as string[]).concat(permission).some(can);
  if (allowed) {
    return (
      <button type="button" className={className} onClick={onClick} disabled={disabled}>
        {children}
      </button>
    );
  }
  return (
    <button
      type="button"
      className={`${className} cursor-not-allowed opacity-50`}
      aria-disabled="true"
      title="Read-only access"
      onClick={(e) => e.preventDefault()}
    >
      {children}
      <span className="sr-only">: read-only access</span>
    </button>
  );
}
