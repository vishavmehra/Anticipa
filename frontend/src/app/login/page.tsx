"use client";

import { isAuthRetryableFetchError } from "@supabase/supabase-js";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { createClient } from "@/lib/supabase/client";
import { btnPrimary, inputClass } from "@/components/ui";

/** Only same-site absolute paths; blocks open redirects such as //evil.example or /\evil. */
function safeNext(): string {
  const n = new URLSearchParams(window.location.search).get("next");
  return n && n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/\\") ? n : "/";
}

export default function LoginPage() {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setPending(true);
    setError(null);
    try {
      const { error: authError } = await createClient().auth.signInWithPassword({
        email: String(form.get("email") ?? "").trim(),
        password: String(form.get("password") ?? ""),
      });
      if (authError) {
        setError(
          isAuthRetryableFetchError(authError)
            ? "The sign-in service could not be reached. Check your connection and try again."
            : authError.message,
        );
        setPending(false);
        return;
      }
      router.replace(safeNext());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
      setPending(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-[calc(100dvh-var(--banner-h))] max-w-md flex-col justify-center px-4 py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Sign in to Anticipa</h1>
      <p className="mt-1 text-ink-muted">
        Preventive-care coordination console. This build runs on synthetic residents only. Accounts are created by an
        administrator.
      </p>
      <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-4 rounded-md border border-line bg-surface p-5">
        {error && (
          <div role="alert" className="rounded-md border border-danger bg-danger-bg px-4 py-3 text-danger">
            <p className="font-semibold">Could not sign in</p>
            <p className="mt-0.5">{error}</p>
          </div>
        )}
        <div>
          <label htmlFor="email" className="mb-1 block font-medium">
            Email
          </label>
          <input id="email" name="email" type="email" autoComplete="username" required className={inputClass} />
        </div>
        <div>
          <label htmlFor="password" className="mb-1 block font-medium">
            Password
          </label>
          <input id="password" name="password" type="password" autoComplete="current-password" required className={inputClass} />
        </div>
        <button type="submit" disabled={pending} className={btnPrimary}>
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
