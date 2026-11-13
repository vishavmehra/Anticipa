"use client";

import { Suspense } from "react";
import { notFound, useSearchParams } from "next/navigation";
import { CaseConversation } from "@/components/conversation";

// Test host for <CaseConversation />, outside the app shell so it can be exercised alone (e2e/features-conversation.spec.ts).
// Development only: 404s in production builds. ?case=<id> picks the case, ?edit=1 turns the message box on (in the app
// the permission comes from GET /auth/me).
function Host() {
  const q = useSearchParams();
  return (
    <main id="main" className="mx-auto max-w-4xl space-y-6 px-4 py-6">
      <h1 className="font-display text-2xl font-bold">Conversation component test host</h1>
      <CaseConversation caseId={q.get("case") ?? "00000000-0000-4000-8000-000000000000"} canSend={q.get("edit") === "1" ? true : undefined} />
    </main>
  );
}

export default function ConversationHostPage() {
  if (process.env.NODE_ENV === "production") notFound();
  return (
    <Suspense>
      <Host />
    </Suspense>
  );
}
