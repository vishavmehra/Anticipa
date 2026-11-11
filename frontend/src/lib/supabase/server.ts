import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { supabaseEnv } from "./env";

/**
 * Server client for Server Components / Route Handlers. Create one per request; never share.
 * Cookie writes are only possible in Route Handlers/Server Actions, so a failed `setAll` here is
 * expected and safe: proxy.ts already refreshes the session on every navigation.
 */
export async function createClient() {
  const { url, key } = supabaseEnv();
  const cookieStore = await cookies();
  return createServerClient(url, key, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) => cookieStore.set(name, value, options));
        } catch {
          /* called from a Server Component; proxy.ts handles the refresh */
        }
      },
    },
  });
}
