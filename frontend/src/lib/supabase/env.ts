/**
 * Public Supabase settings only (URL + publishable key; ADR-036). Property access must stay literal
 * (`process.env.NEXT_PUBLIC_*`) so Next.js can inline the values into the browser bundle.
 * A service-role or secret key must never be referenced anywhere in frontend/.
 */
export function supabaseEnv(): { url: string; key: string } {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) {
    throw new Error(
      "Supabase is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY (see frontend/.env.example).",
    );
  }
  return { url, key };
}
