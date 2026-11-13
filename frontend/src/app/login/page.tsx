"use client";

import { isAuthRetryableFetchError } from "@supabase/supabase-js";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { Brand } from "@/components/brand";
import BackgroundPaths from "@/components/ui/background-paths";
import DotField from "@/components/ui/dot-field";
import { Icon, btnPrimary, btnSecondary, inputClass } from "@/components/ui";
import { createClient } from "@/lib/supabase/client";

/** Only same-site absolute paths; blocks open redirects such as //evil.example or /\evil. */
function safeNext(): string {
  const n = new URLSearchParams(window.location.search).get("next");
  return n && n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/\\") ? n : "/";
}

function GoogleMark() {
  return (
    <svg viewBox="0 0 24 24" className="size-5 shrink-0" aria-hidden="true">
      <path fill="#4285F4" d="M23.52 12.27c0-.85-.08-1.67-.22-2.45H12v4.64h6.46a5.52 5.52 0 0 1-2.4 3.62v3h3.88c2.27-2.09 3.58-5.17 3.58-8.81z" />
      <path fill="#34A853" d="M12 24c3.24 0 5.96-1.07 7.94-2.91l-3.88-3a7.2 7.2 0 0 1-10.7-3.78H1.34v3.09A12 12 0 0 0 12 24z" />
      <path fill="#FBBC05" d="M5.36 14.31a7.2 7.2 0 0 1 0-4.62V6.6H1.34a12 12 0 0 0 0 10.8l4.02-3.09z" />
      <path fill="#EA4335" d="M12 4.77c1.76 0 3.34.61 4.59 1.8l3.44-3.44A11.5 11.5 0 0 0 12 0 12 12 0 0 0 1.34 6.6l4.02 3.09A7.2 7.2 0 0 1 12 4.77z" />
    </svg>
  );
}

const POINTS = [
  ["target", "Every fact traced to the exact quote it came from"],
  ["scale", "Eligibility decided by approved, versioned rules, never by AI"],
  ["shield", "Every action audited, every role limited to what it needs"],
] as const;

function LoginView() {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, setPending] = useState(false);
  const [googlePending, setGooglePending] = useState(false);
  const [error, setError] = useState<string | null>(params.get("error") ? "Google sign-in did not complete. Try again, or sign in with email." : null);

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

  async function onGoogle() {
    setGooglePending(true);
    setError(null);
    try {
      const { error: oauthError } = await createClient().auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: `${window.location.origin}/auth/callback` },
      });
      if (oauthError) {
        setError(oauthError.message);
        setGooglePending(false);
      }
      // Success redirects the browser to Google; nothing more to do here.
    } catch (err) {
      setError(err instanceof Error ? err.message : "Google sign-in failed.");
      setGooglePending(false);
    }
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-[1.05fr_1fr]">
      <DotField className="on-navy hidden bg-navy text-[#b9a2f7] lg:flex" spacing={18} seed={11}>
        <div className="relative z-10 mx-10 max-w-lg rounded-card bg-navy/85 p-9 backdrop-blur-sm">
          <Brand tone="dark" />
          <h2 className="mt-10 font-display text-[2.2rem] leading-[1.15] font-bold tracking-tight text-white">Find missing preventive care, then help people complete it.</h2>
          <p className="mt-4 text-[0.9375rem] leading-relaxed text-navy-text">
            Anticipa reads records and clinical notes, applies approved guidelines, plans outreach around real clinic capacity and follows every case until the care is done.
          </p>
          <ul className="mt-8 space-y-4">
            {POINTS.map(([icon, text]) => (
              <li key={text} className="flex items-start gap-3 text-[0.9375rem] text-white">
                <span className="icon-tile size-9">
                  <Icon name={icon} className="size-4" />
                </span>
                <span className="pt-1">{text}</span>
              </li>
            ))}
          </ul>
        </div>
      </DotField>

      <BackgroundPaths className="min-h-dvh bg-canvas text-brand" count={36} speed={0.8}>
        <main className="relative z-10 w-full max-w-md px-4 py-10">
          <div className="mb-6 lg:hidden">
            <Brand tone="light" />
          </div>
          <div className="glass-strong p-7 text-ink shadow-pop sm:p-8">
            <h1 className="font-display text-3xl font-bold tracking-tight">Sign in to Anticipa</h1>
            <p className="mt-1.5 text-ink-muted">Preventive care coordination console. Judges: continue with Google for read only access.</p>

            {error && (
              <div role="alert" className="mt-5 flex gap-2.5 rounded-control border border-danger-line bg-danger-bg px-4 py-3 text-danger">
                <Icon name="warning" className="mt-0.5 size-4" />
                <div>
                  <p className="font-semibold">Could not sign in</p>
                  <p className="mt-0.5">{error}</p>
                </div>
              </div>
            )}

            <button type="button" onClick={onGoogle} disabled={googlePending} className={`${btnSecondary} mt-6 w-full`}>
              <GoogleMark />
              {googlePending ? "Redirecting to Google…" : "Continue with Google"}
            </button>

            <div className="my-5 flex items-center gap-3 text-xs text-ink-muted" role="separator" aria-label="or">
              <span className="h-px flex-1 bg-line" />
              or sign in with email
              <span className="h-px flex-1 bg-line" />
            </div>

            <form onSubmit={onSubmit} className="flex flex-col gap-4">
              <div>
                <label htmlFor="email" className="mb-1.5 block font-medium">
                  Email
                </label>
                <input id="email" name="email" type="email" autoComplete="username" required className={inputClass} />
              </div>
              <div>
                <label htmlFor="password" className="mb-1.5 block font-medium">
                  Password
                </label>
                <input id="password" name="password" type="password" autoComplete="current-password" required className={inputClass} />
              </div>
              <button type="submit" disabled={pending} className={`${btnPrimary} w-full`}>
                {pending ? "Signing in…" : "Sign in"}
              </button>
            </form>
            <p className="mt-5 text-xs text-ink-muted">Accounts are created by an administrator. Access is limited to the role assigned to your account.</p>
          </div>
        </main>
      </BackgroundPaths>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginView />
    </Suspense>
  );
}
